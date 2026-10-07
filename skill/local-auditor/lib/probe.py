"""qwen-agent --probe: the throwaway sandbox a probe session runs in.

qwen-agent.sh stays a thin wrapper and calls this module:

  probe.py create --cwd DIR [--source DIR]   prints four lines: the probe run folder,
                                             the sandbox, the session's directory in it,
                                             and the source that was copied
  probe.py diff SANDBOX OUT                  writes the session's patch (bytes) to OUT
  probe.py remove RUN                        removes a probe run folder made by create
  probe.py check DIR                         prints the sandbox holding DIR -- but only when
                                             it really is one create() made; this is what
                                             qwen-agent --probe-here asks before fencing

The source is --source when given (qwen-agent's --test-repo), else the top of the git
work tree holding --cwd, else --cwd itself (copied). The sandbox is
lib/swarm_engine/sandbox.create(source, ..., include_dirty=True): an independent clone
(or copy) carrying the user's uncommitted and untracked files, so the session sees the
tree as the user sees it, while the user's tree is only ever read.

Probe run folders live under $QWEN_PROBE_DIR, default
$XDG_CACHE_HOME/qwen-agent/probes (else ~/.cache/qwen-agent/probes), one
<UTC stamp>-<random> folder per run holding sandboxes/tree. A probe directory inside the
source is refused: the sandbox would copy itself.

Nothing here acts on a folder it did not create. create() drops `.qwen-probe-run`
(holding the word "qwen-probe") into every run folder; remove RUN is refused unless RUN
is a non-empty path to an existing directory carrying that marker, and diff SANDBOX is
refused (before any git command runs) unless SANDBOX has the `.base` file
sandbox.create() writes next to it and the run folder two levels up carries the run
marker, and unless neither SANDBOX nor its `sandboxes/` parent is a symlink -- checked
on the path itself before resolving anything, so a sandbox an agent replaced with a
link elsewhere is refused instead of having git run through the link. A -C whose
directory is not part of the copy -- inside an ignored folder such as build/, or
inside .git -- is refused with the half-made run folder removed. check DIR runs the
same guard over DIR's git top level and demands facts those marker files cannot be
faked into covering: the `.base` content names a commit that exists IN that sandbox, the
sandbox's parent directory is named `sandboxes`, and realpath(DIR) is the sandbox top or
inside it (compared component-wise, so sb2 is never read as inside sb) -- so a stray
`<repo>.base` beside the user's own checkout never earns the --probe-here fence (a full
shell there). The git calls this module makes itself (the top-level lookup and the
.base check) strip GIT_DIR, GIT_WORK_TREE, GIT_COMMON_DIR, GIT_INDEX_FILE,
GIT_OBJECT_DIRECTORY and GIT_ALTERNATE_OBJECT_DIRECTORIES from the environment, so an
inherited GIT_* cannot steer the gate to or off a sandbox. The clone in make() runs
through sandbox.create, which inherits the environment: its callers unset those
variables first. Every line printed to stdout goes out as UTF-8 bytes through
sys.stdout.buffer, on every platform: qwen-agent reads them in bash, and a console
codec that cannot encode the name -- cp1252 on a piped Windows stdout -- must not
raise on the last print and leak
the run folder; a name whose bytes are not UTF-8 re-encodes to those exact bytes. If
even that write fails, the half-made run folder is removed and the exit is 8. stderr
messages encode with the stderr codec and replace.

Exit codes: 0 ok, 2 refused (bad paths), 8 failed (git or the file system).
"""
import argparse
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.swarm_engine import sandbox  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_FAIL = 0, 2, 8
TREE = "tree"
RUN_MARKER = ".qwen-probe-run"        # create() drops it in every run folder: remove() and
RUN_MARKER_WORD = "qwen-probe"        # diff() refuse a folder that does not carry it.


class Refused(Exception):
    """A path combination probe mode will not work with (exit 2)."""


def probe_root():
    explicit = os.environ.get("QWEN_PROBE_DIR")
    if explicit:
        return pathlib.Path(explicit)
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return pathlib.Path(xdg) / "qwen-agent" / "probes"


def _norm(p):
    """The form two paths are compared in: realpath through normcase -- case-insensitive
    on Windows, where realpath alone would still let a differently-cased spelling differ."""
    return os.path.normcase(os.path.realpath(str(p)))


def _same(a, b):
    """True when the two paths name the same directory: equal once normalised, or
    samefile-equal while both exist (macOS' normcase is a no-op, so there a spelling that
    differs only in case survives the string compare while both name one directory)."""
    if _norm(a) == _norm(b):
        return True
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def _inside(child, parent):
    """True when the real path child is parent or below it; both sides compared as
    normcase(realpath(...)), with the samefile answer accepted for equal-but-differently-
    spelled existing paths."""
    c, p = _norm(child), _norm(parent)
    if c == p or c.startswith(p.rstrip(os.sep) + os.sep):
        return True
    return _same(child, parent)


GIT_ENV_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def _git_env():
    """The environment for every git subprocess this module runs: os.environ minus the
    GIT_* variables that point git at a repository other than the one named on its own
    command line. GIT_WORK_TREE=<a sandbox> is alone enough to make `git -C <plain repo>
    rev-parse --show-toplevel` answer with that sandbox -- and the --probe-here gate is
    exactly a rev-parse whose answer decides whether a session gets a full shell. The
    caller's environment must not be able to steer it."""
    env = dict(os.environ)
    for name in GIT_ENV_VARS:
        env.pop(name, None)
    return env


def git_toplevel(cwd):
    """The top of the git work tree holding cwd when it has a commit, else None. Decoded
    with surrogateescape: a path whose bytes are not UTF-8 must survive as itself."""
    try:
        top = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                             capture_output=True, encoding="utf-8", errors="surrogateescape",
                             env=_git_env())
        if top.returncode != 0:
            return None
        head = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--verify", "-q", "HEAD"],
                              capture_output=True, env=_git_env())
    except OSError:
        return None
    if head.returncode != 0:
        return None
    return pathlib.Path(top.stdout.strip())


def _has_run_marker(run):
    """True when `run` carries the marker create() writes into every probe run folder.
    Bytes, not text: the folder's name may be undecodable and so may anything an agent
    left at the marker's name; neither gets followed or trusted beyond the word."""
    try:
        return RUN_MARKER_WORD.encode("utf-8") in (pathlib.Path(run) / RUN_MARKER).read_bytes()
    except OSError:
        return False                       # missing, unreadable or a directory


def _is_probe_sandbox(sb):
    """True when `sb` is the sandbox create() made: neither `sb` nor its sandboxes/
    parent is a symlink (checked with is_symlink() on the two paths themselves, BEFORE
    anything is resolved or read -- an agent that replaced its sandbox with a link
    elsewhere must be refused outright, not have git run through the link on the strength
    of the real .base still sitting beside the link), the `.base` file sandbox.create()
    writes sits next to it, and the run folder holding sandboxes/<name> (two levels up)
    carries the run marker. Only these facts decide -- no git command is run to find
    out."""
    sb = pathlib.Path(sb)
    return (not sb.is_symlink() and not sb.parent.is_symlink()
            and (sb.parent / (sb.name + sandbox.BASE_SUFFIX)).is_file()
            and _has_run_marker(sb.parent.parent))


def make(source, cwd, run):
    """Build the sandbox of `source` under `run` (as run/sandboxes/tree) and return
    (sandbox, session_dir): session_dir is the sandbox's copy of `cwd`, which must be
    `source` or inside it AND actually have a copy in the sandbox -- a -C inside an
    ignored folder such as build/ (never copied) or inside .git has none, and printing
    a session dir that does not exist is worse than refusing."""
    source, cwd = pathlib.Path(source), pathlib.Path(cwd)
    if not source.is_dir():
        raise Refused("not a directory: %s" % source)
    if not _inside(cwd, source):
        raise Refused("the -C directory %s is not inside %s" % (cwd, source))
    rs, rc = os.path.realpath(str(source)), os.path.realpath(str(cwd))
    if ".git" in pathlib.PurePath(rc).parts:      # checked on the -C path itself: when -C
        raise Refused("%s is not part of the copy of %s: nothing under .git is copied"
                      % (cwd, source))            # IS a .git, the fallback source is that
    rel = os.curdir if _same(cwd, source) else os.path.relpath(rc, rs)   # .git itself
    sb = sandbox.create(source, pathlib.Path(run) / "sandboxes" / TREE, include_dirty=True)
    here = sb if rel == os.curdir else sb / rel
    if not here.is_dir():
        raise Refused("%s is not part of the copy of %s (an ignored directory is not "
                      "copied into the sandbox)" % (cwd, source))
    return sb, here


def _purge(run):
    """The deletion half of remove(): the sandbox through sandbox.cleanup (never
    following a link an agent left in its place), the emptied folders, and create()'s
    marker last -- only once the sandbox is gone, so a folder whose deletion failed
    can still be removed by a later remove(). A folder holding anything else is left
    in place."""
    run = pathlib.Path(run)
    sandbox.cleanup(run, None)
    for d in (run / "sandboxes" / sandbox.TEMPLATE, run / "sandboxes"):
        try:
            d.rmdir()
        except OSError:
            pass
    if os.path.lexists(str(run / "sandboxes")):
        return
    for step in ((run / RUN_MARKER).unlink, run.rmdir):
        try:
            step()
        except OSError:
            pass


def create(cwd, source=None):
    """A new probe run folder with its sandbox; returns (run, sandbox, session_dir,
    source). The run folder carries the run marker from the first, so a failed build's
    half-made folder is as removable as a finished one -- and is removed here."""
    cwd = pathlib.Path(cwd)
    if not cwd.is_dir():
        raise Refused("not a directory: %s" % cwd)
    src = pathlib.Path(source) if source else (git_toplevel(cwd) or cwd)
    root = probe_root()
    if _inside(root, src):
        raise Refused("the probe directory %s is inside %s; set QWEN_PROBE_DIR elsewhere"
                      % (root, src))
    root.mkdir(parents=True, exist_ok=True)
    run = pathlib.Path(tempfile.mkdtemp(
        prefix=time.strftime("%Y%m%dT%H%M%SZ-", time.gmtime()), dir=str(root)))
    try:
        (run / RUN_MARKER).write_text(RUN_MARKER_WORD + "\n", encoding="utf-8")
        sb, here = make(src, cwd, run)
    except BaseException:
        _purge(run)
        raise
    return run, sb, here, src


def write_patch(sb, out):
    """The session's changes since the sandbox base, written to `out` as exact bytes.
    Returns the number of bytes written (0 = the session changed nothing). Refused
    before any git command runs unless `sb` is a sandbox create() made and is not
    reached through a symlink."""
    if not _is_probe_sandbox(sb):
        raise Refused("%s is not a qwen-probe sandbox as create() made it (it or its "
                      "sandboxes/ parent is a symlink, there is no .base marker beside "
                      "it, or there is no run marker above sandboxes/)" % sb)
    data = sandbox.diff(sb).encode("utf-8", "surrogateescape")
    pathlib.Path(out).write_bytes(data)
    return len(data)


BASE_SHA = re.compile(r"[0-9a-fA-F]{40,}")   # what create() writes in a .base: a full sha. Not
# merely "not empty": a 4-char prefix names a commit just as well as the whole sha does, so a
# truncated .base is not a base.


def check(dir_):
    """The sandbox holding `dir_`, refused unless it really is one create() made -- the
    --probe-here gate, which hands a session full Bash where it points. The `_is_probe_sandbox`
    guard alone is text anyone can copy: a plain repo with a hand-written `<toplevel>.base`
    beside it and a `.qwen-probe-run` two levels up would pass it. So on top of the guard the
    `.base` must name a commit that EXISTS in the sandbox (`git rev-parse --verify --quiet
    <sha>^{commit}`, run inside it), the sandbox's parent must be named `sandboxes`, as
    create() lays it out, and `dir_` itself -- resolved with realpath and compared
    component-wise, not by string prefix (/a/sb2 is not inside /a/sb) -- must be the sandbox
    top or inside it. Every git call here runs with the GIT_* steering variables stripped
    (_git_env), so an inherited GIT_WORK_TREE/GIT_DIR cannot make a plain repo's top level
    answer as a kept sandbox."""
    top = git_toplevel(dir_)
    if top is None:
        raise Refused("%s is not inside a git work tree with a commit" % dir_)
    if not _inside(dir_, top):                 # realpath, compared component-wise: sb2 is
        raise Refused("%s is not %s or inside it -- the top level git named does not hold "
                      "the directory asked about" % (dir_, top))         # never inside sb
    if not _is_probe_sandbox(top):
        raise Refused("%s is not inside a qwen-probe sandbox as create() made it (it or its "
                      "sandboxes/ parent is a symlink, there is no .base marker beside it, or "
                      "there is no run marker above sandboxes/)" % dir_)
    if top.parent.name != "sandboxes":
        raise Refused("%s does not sit under a directory named sandboxes, as create() lays "
                      "one out" % top)
    try:
        sha = (top.parent / (top.name + sandbox.BASE_SUFFIX)).read_text(
            encoding="utf-8", errors="surrogateescape").strip()
    except OSError:                      # missing or unreadable: not the file create() wrote
        raise Refused("cannot read the .base beside %s" % top)
    if not BASE_SHA.fullmatch(sha):
        raise Refused("the .base of %s is not a full commit sha: %r" % (top, sha[:20]))
    try:
        found = subprocess.run(["git", "-C", str(top), "rev-parse", "--verify", "--quiet",
                                "%s^{commit}" % sha], capture_output=True, env=_git_env())
    except OSError:                      # no git: nothing to check the .base against
        raise Refused("git is not available to check the .base of %s" % top)
    if found.returncode != 0:
        raise Refused("the .base of %s names %s, which is not a commit in it" % (top, sha))
    return top


def _is_run_folder(text):
    return bool(text) and pathlib.Path(text).is_dir() and _has_run_marker(text)


def remove(run):
    """Remove a probe run folder made by create(); True when its sandbox is gone, False
    when nothing was touched or the sandbox could not be deleted (the deletions below
    swallow their own errors, so what is still on disk is the answer). A folder this
    module did not create is never touched: `run` must be a non-empty path to an
    existing directory carrying create()'s run marker. A folder holding anything else
    (after the sandbox is gone) is left in place."""
    text = str(run)
    if not _is_run_folder(text):
        return False
    _purge(text)
    return not os.path.lexists(os.path.join(text, "sandboxes"))


def _out(text):
    """One print to stdout, as raw UTF-8 bytes, on every platform: qwen-agent.sh reads
    these lines in bash, so the console's codec is not the contract. Encoding through a
    cp1252 text layer (a piped Windows stdout) would raise UnicodeEncodeError on the
    final print of create -- after the run folder is already made, so exit 1 and the
    folder leaks; surrogateescape on the text layer only covers non-UTF-8 bytes, not a
    codec that cannot spell CJK. A name whose bytes arrived as undecodable (via
    surrogateescape) re-encodes to the very bytes it came in as."""
    sys.stdout.buffer.write(text.encode("utf-8", "surrogateescape"))
    sys.stdout.buffer.flush()


def main(argv=None):
    # stderr keeps its own codec but replaces, so an error message about an undecodable
    # path can never raise on the way out; stdout is not used as text at all (_out).
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="probe.py")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("create")
    c.add_argument("--cwd", required=True)
    c.add_argument("--source")
    d = sub.add_parser("diff")
    d.add_argument("sandbox")
    d.add_argument("out")
    r = sub.add_parser("remove")
    r.add_argument("run")
    k = sub.add_parser("check")
    k.add_argument("dir")
    try:
        o = ap.parse_args(argv)
    except SystemExit as e:
        return e.code if e.code == 0 else EXIT_USAGE     # --help exits 0; usage errors 2
    if o.cmd is None:
        ap.print_usage(sys.stderr)
        return EXIT_USAGE
    try:
        if o.cmd == "create":
            run, sb, here, src = create(o.cwd, o.source)
            try:
                _out("%s\n%s\n%s\n%s\n" % (run, sb, here, src))
            except (OSError, ValueError):        # a console that took nothing: leave no
                _purge(run)                      # half-made run folder behind
                return EXIT_FAIL
        elif o.cmd == "diff":
            write_patch(o.sandbox, o.out)
        elif o.cmd == "check":
            _out("%s\n" % check(o.dir))
        elif not _is_run_folder(str(o.run)):
            print("%s is not a probe run folder made by create: removing nothing" % o.run,
                  file=sys.stderr)
            return EXIT_USAGE
        elif not remove(o.run):
            print("the sandbox in %s could not be removed completely" % o.run,
                  file=sys.stderr)
            return EXIT_FAIL
    except Refused as e:
        print(str(e), file=sys.stderr)
        return EXIT_USAGE
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        print(str(e), file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
