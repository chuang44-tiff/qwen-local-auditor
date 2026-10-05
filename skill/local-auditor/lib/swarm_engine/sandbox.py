"""Throwaway copies of --target: where agents that can edit or run commands work.

The user's tree is never the working directory of an agent that can edit or run a
command. A target that is the top of a git work tree gets
`git clone --shared --no-checkout --template=<empty dir> -q <target> <path>`, then
`git remote remove origin` (so a push cannot reach the target) and
`git checkout -q --detach <HEAD>`: the sandbox is an independent repository that only
shares the target's objects, never a linked worktree. A worktree shares the target's
.git, so `git stash` / `git branch` run inside the sandbox created refs IN THE TARGET
and `git worktree add` ran the target's post-checkout hook against it. Anything else
is copied without .git, node_modules, .venv, __pycache__ and the test/lint caches
(.pytest_cache, .mypy_cache, .ruff_cache, .tox, .nox), then committed into a fresh
repo so a diff works the same way in both modes.

Every git command this module runs gets `-c core.hooksPath=<a path that does not
exist>` and the clone's template directory is empty, so no hook can ever fire.

The base commit -- the target's HEAD in clone mode, the "sandbox base" commit in copy
mode -- is written to `<path>.base` next to the sandbox (outside it), and diff() stages
with `git add -A` and diffs against that recorded base, so commits the agent made
inside the sandbox are still in the patch. A `.base` an agent replaced with a symlink
is treated as missing -- diff() falls back to HEAD rather than trusting the link's
contents. The copy-mode baseline is staged with
`git add -A --force` so edits to copied files the target's .gitignore ignores are in
the patch too; new files the agent creates in an ignored path stay out of it. After the
baseline exists (after the checkout in clone mode, after the commit in copy mode) create()
appends the usual build-artifact patterns (__pycache__, *.pyc, test/lint caches,
node_modules/, .venv/, ...) to `<path>/.git/info/exclude`: an agent that runs Python
leaves __pycache__/*.pyc behind, and those must never reach a patch -- a plain
`git add -A` skips them, while the --force-staged baseline and tracked-file edits stay.

Patches are byte-exact: git output is captured as bytes and diff() returns it decoded
with "surrogateescape", so encoding the string back restores the exact bytes; run_cmd
feeds a patch to `git apply -` as bytes.

Sandboxes live under <run>/sandboxes/; a sandbox and its .base file are removed with a
plain rmtree -- the target is never needed, since a sandbox no longer shares its .git
with the target -- and cleanup(run, target) does that for every leftover even when
target is None, and also sweeps the stray `<name>.base` / `<name>.out` files sitting
directly under <run>/sandboxes. When <run>/sandboxes is itself a symlink cleanup()
unlinks the link and removes nothing through it. Paths handed to
create()/run_cmd()/diff()/remove() are made absolute first -- by resolving the PARENT,
never the path itself, so a sandbox an agent replaced with a symlink is removed as a
link and create()/run_cmd() refuse, checking the parent BEFORE resolving it, to build
through one -- and a caller whose cwd is not the run directory can still pass relative
ones (the clone itself runs with the sandbox's parent as its cwd).

run_cmd runs `bash -c cmd` in a fresh sandbox with the command's output going to a
temporary file instead of a pipe (so killing the shell ends the wait even when a
grandchild lives on); the file's name comes from `tempfile.mkstemp` -- a random
`<name>.*.out` under the sandboxes root -- so an output link an agent pre-placed at the
old fixed `<name>.out` name is never written through. On timeout it kills the shell's
process group on POSIX -- a child
that called setsid has left that group and survives, but run_cmd no longer waits for it.
On Windows it tries `taskkill /F /T` under its own timeout and kills the shell itself.
"""
import contextlib
import hashlib
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

SKIP = (".git", "node_modules", ".venv", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox")
BUILD_IGNORE = ("__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/", ".mypy_cache/",
                ".ruff_cache/", ".tox/", ".nox/", "node_modules/", ".venv/", ".coverage",
                "*.egg-info/")        # create() appends these to <sandbox>/.git/info/exclude
GIT_ID = ["-c", "user.name=qwen-swarm", "-c", "user.email=qwen-swarm@localhost",
          "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
TAIL = 4000
TEMPLATE = ".qwen-empty-template"
BASE_SUFFIX = ".base"           # the base-sha file create() writes beside a sandbox;
OUT_SUFFIX = ".out"             # run_cmd()'s temporary output file. cleanup() sweeps
STRAY_SUFFIXES = (BASE_SUFFIX, OUT_SUFFIX)     # both as stray leftovers, not just dirs.


def _text(data, errors="replace"):
    return data.decode("utf-8", errors)


def _git(args, cwd, check=True, timeout=300, stdin_data=None):
    """git with hooks switched off, run as `git -c core.hooksPath=<does-not-exist> ...`.
    stdout/stderr are bytes; failure messages replace-decode them."""
    nohooks = pathlib.Path(cwd).parent / ".qwen-no-hooks"
    cmd = ["git", "-c", "core.hooksPath=%s" % nohooks] + list(args)
    if stdin_data is None:
        p = subprocess.run(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout)
    else:
        p = subprocess.run(cmd, cwd=str(cwd), input=stdin_data, capture_output=True,
                           timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed: %s"
                           % (" ".join(args), _text(p.stderr or p.stdout).strip()))
    return p


def is_git_root(target):
    """True when target is the top directory of a git work tree with at least one commit."""
    try:
        top = _git(["rev-parse", "--show-toplevel"], target, check=False)
        head = _git(["rev-parse", "--verify", "-q", "HEAD"], target, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if top.returncode != 0 or head.returncode != 0:
        return False
    try:
        return os.path.samefile(_text(top.stdout).strip(), str(target))
    except OSError:
        return False


def _walk(target):
    for root, dirs, files in os.walk(str(target)):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for f in sorted(files):
            yield pathlib.Path(root) / f


def fingerprint(target):
    """git:<HEAD> for a git target; copy:<hash of every copied file's path, size and
    mtime> otherwise. Part of the cache key of read/sandbox units and of run_cmd."""
    target = pathlib.Path(target)
    if is_git_root(target):
        return "git:" + _text(_git(["rev-parse", "HEAD"], target).stdout).strip()
    h = hashlib.sha256()
    for p in _walk(target):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(("%s\0%d\0%d\n" % (p.relative_to(target).as_posix(), st.st_size,
                                    st.st_mtime_ns)).encode("utf-8"))
    return "copy:" + h.hexdigest()


def is_dirty(target):
    """A git target with uncommitted changes (they are NOT in any sandbox)."""
    if not is_git_root(pathlib.Path(target)):
        return False
    return bool(_text(_git(["status", "--porcelain"], target).stdout).strip())


def _base_file(path):
    """Where the sandbox's base commit sha lives: next to it, never inside it."""
    path = pathlib.Path(path)
    return path.parent / (path.name + BASE_SUFFIX)


def _exclude_build_artifacts(path):
    """Append BUILD_IGNORE to <sandbox>/.git/info/exclude (creating .git/info) so the
    artifacts an agent's own commands leave in the sandbox -- a Python run's
    __pycache__/*.pyc, test/lint caches -- never reach the patch: diff() stages with a
    plain `git add -A`, which skips ignored files. Written only after the baseline (post
    checkout / post commit): the baseline is staged with --force anyway, and copied or
    checked-out files are meant to stay in the patch -- this stops NEW artifacts."""
    info = pathlib.Path(path) / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    with open(info / "exclude", "a", encoding="utf-8") as fh:
        fh.write("".join(p + "\n" for p in BUILD_IGNORE))


def _pin_eol(path):
    """Record core.autocrlf=false in the sandbox repo's OWN config, before its working
    tree is materialised, so every git command run inside the sandbox -- this module's
    checkout as well as an agent's unpinned `git stash` / `git apply` -- sees the same
    line endings whatever the user's global core.autocrlf/core.eol say. GIT_ID's `-c`
    flags cover only this module's own commands: with the pin missing from the config,
    a global core.autocrlf=true made the unpinned checkout below convert the checked-out
    files, git still called them clean, and every later diff/apply mismatched --
    "patch does not apply". core.eol is pinned to lf as well: a target whose
    .gitattributes says `text=auto` would otherwise still be checked out with the user's
    core.eol (crlf) even with autocrlf off."""
    _git(["config", "core.autocrlf", "false"], path)
    _git(["config", "core.eol", "lf"], path)


def create(target, path):
    """A fresh sandbox of target at path (any old one there is removed first). Both paths
    become absolute: the clone runs with the sandbox's parent as its cwd, so a relative
    target or path would otherwise be read relative to the wrong directory. A parent that
    is itself a symlink is refused outright -- the sandbox would be built THROUGH it, in
    a directory this run does not own."""
    target = pathlib.Path(target).resolve()
    path = pathlib.Path(path)
    if path.parent.is_symlink():
        # Checked before the mkdir and the resolve: both would already be acting on the
        # directory the link points at.
        raise RuntimeError("cannot build a sandbox at %s: %s is a symlink"
                           % (path, path.parent))
    path.parent.mkdir(parents=True, exist_ok=True)             # so the parent resolves
    path = path.parent.resolve() / path.name                   # path may not exist yet
    remove(target, path)
    if path.is_symlink() or path.exists():
        # remove() could not clear the path -- it is a link it could not unlink or a plain
        # file it will not rmtree: building here would go through or over it, so refuse.
        raise RuntimeError("cannot build a sandbox at %s: something exists there" % path)
    if is_git_root(target):
        sha = _text(_git(["rev-parse", "HEAD"], target).stdout).strip()
        template = path.parent / TEMPLATE
        template.mkdir(parents=True, exist_ok=True)          # shared by all sandboxes here
        _git(["clone", "--shared", "--no-checkout", "--template=%s" % template, "-q",
              str(target), str(path)], path.parent)
        _git(["remote", "remove", "origin"], path)           # a push cannot reach the target
        _pin_eol(path)                                       # before the checkout writes the
        _git(["checkout", "-q", "--detach", sha], path)       # tree: byte-exact whatever the
                                                               # user's global EOL settings
        _exclude_build_artifacts(path)                       # after the checkout: only NEW
        _base_file(path).write_text(sha + "\n", encoding="utf-8")
        return path
    shutil.copytree(str(target), str(path), symlinks=True, ignore=shutil.ignore_patterns(*SKIP))
    _git(["init", "-q"], path)
    _pin_eol(path)                                           # the config pin outlives create():
    # every later git in the sandbox, pinned or not, is immune to the user's global EOL
    _git(GIT_ID + ["add", "-A", "--force"], path)            # ignored-but-copied files too
    _git(GIT_ID + ["commit", "-q", "--allow-empty", "--no-verify", "-m", "sandbox base"], path)
    _exclude_build_artifacts(path)                           # after the baseline commit: the
    # --force stage keeps copied files in every patch; only new artifacts are excluded
    sha = _text(_git(["rev-parse", "HEAD"], path).stdout).strip()
    _base_file(path).write_text(sha + "\n", encoding="utf-8")
    return path


def diff(path):
    """Everything the sandbox changed since its recorded base, new files included, as a
    patch. The base is the .base file (falling back to HEAD), not the sandbox's current
    HEAD, so an agent that committed is still in the patch. The returned string re-encodes
    to the exact patch bytes with .encode("utf-8", "surrogateescape")."""
    path = pathlib.Path(path)
    path = path.parent.resolve() / path.name   # parent only: never follow a link at `path`,
    base = "HEAD"                              # so its .base stays the one beside the link
    bf = _base_file(path)
    # A .base replaced with a symlink is treated as missing: the link's text is not a
    # sha to diff against, and HEAD is that same base in both create() modes.
    if not bf.is_symlink():
        with contextlib.suppress(OSError):
            base = _text(bf.read_bytes()).strip() or "HEAD"
    _git(GIT_ID + ["add", "-A"], path)
    out = _git(GIT_ID + ["diff", "--cached", "--binary", "--no-color", "--no-ext-diff", base],
               path).stdout
    return out.decode("utf-8", "surrogateescape")


def _rmtree(path):
    """shutil.rmtree that also clears read-only files (git objects on Windows). Only
    called on a path that exists and is not itself a link: shutil.rmtree removes the
    symlinks inside a tree without following them, which is what we want."""
    def onerror(func, p, _exc):
        with contextlib.suppress(OSError):
            os.chmod(p, stat.S_IWRITE)
            func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(str(path), onexc=onerror)
    else:
        shutil.rmtree(str(path), onerror=onerror)


def remove(target, path):
    """Remove one sandbox and its .base file; never fails (a leftover is removed by the
    next run). The sandbox shares no .git with the target, so the target is not needed.
    A relative path is read against the current working directory, not the run's. Only
    the parent is resolved, so a sandbox an agent replaced with a symlink to somewhere
    else is unlinked, never followed: rmtree would delete the link's destination."""
    p = pathlib.Path(path)
    p = p.parent.resolve() / p.name                            # never resolve `path` itself
    if p.is_symlink():
        with contextlib.suppress(OSError):
            p.unlink()                                         # the link only, never its target
    elif p.exists():
        with contextlib.suppress(OSError):
            _rmtree(p)
    with contextlib.suppress(OSError):             # the .base file: a link or a file is
        _base_file(p).unlink()                     # unlinked, never followed


def cleanup(run, target):
    """Remove every sandbox under <run>/sandboxes (leftovers of a killed run, or the
    sandboxes of this run at exit); also sweeps create()'s helper directory there, plus
    the stray `.base` / `.out` files a killed run leaves behind. When <run>/sandboxes is
    itself a symlink only the link goes -- walking it would delete whatever it points
    at. The target is not needed: removing a sandbox never touches the target."""
    root = pathlib.Path(run) / "sandboxes"
    if root.is_symlink():
        with contextlib.suppress(OSError):
            root.unlink()                      # the link only: its contents are not ours
        return
    if not root.is_dir():
        return
    for d in sorted(root.iterdir()):
        if d.is_dir():
            remove(target, d)
        elif d.name.endswith(STRAY_SUFFIXES):
            with contextlib.suppress(OSError):
                d.unlink()


def bash_path():
    """The bash run_cmd uses: $QWEN_SWARM_BASH (qwen-swarm.sh sets it), else PATH's."""
    return os.environ.get("QWEN_SWARM_BASH") or shutil.which("bash") or "bash"


def _kill(p):
    """Kill the shell's process group on POSIX (start_new_session made it its own group);
    a child that called setsid has left that group and survives, but run_cmd has already
    stopped waiting. On Windows: taskkill /F /T under a timeout, then p.kill()."""
    if os.name == "posix":
        with contextlib.suppress(OSError):          # start_new_session made p its own pgid
            os.killpg(p.pid, signal.SIGKILL)
    else:
        # SubprocessError covers TimeoutExpired: a stuck taskkill is given up on at 30s.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with contextlib.suppress(OSError):
            p.kill()


def run_cmd(target, path, cmd, patch=None, timeout=600):
    """`bash -c cmd` in a fresh sandbox of target at path, `patch` (a str or the bytes of
    one) applied first. Returns {applied, rc, timed_out, output_tail}; the sandbox is
    always removed. Relative target/path are read against the caller's cwd."""
    target = pathlib.Path(target).resolve()
    path = pathlib.Path(path)
    if path.parent.is_symlink():
        # The create() check below would not see it: resolving the parent here would
        # swallow the link and hand create() the directory it points at.
        raise RuntimeError("cannot run a sandbox at %s: %s is a symlink"
                           % (path, path.parent))
    path = path.parent.resolve() / path.name         # path does not exist yet
    out_file = None
    try:
        create(target, path)
        if patch:
            data = patch.encode("utf-8", "surrogateescape") if isinstance(patch, str) else patch
            ap = _git(GIT_ID + ["apply", "--whitespace=nowarn", "-"], path, check=False,
                      stdin_data=data)
            if ap.returncode != 0:
                return {"applied": False, "rc": None, "timed_out": False,
                        "output_tail": _text(ap.stderr or ap.stdout)[-TAIL:]}
        # The output goes to a file, not a pipe: waiting must end once bash is killed,
        # even when a grandchild that outlives it still holds the output open. mkstemp
        # picks the name (a random `<name>.*.out`, still swept by suffix in cleanup()),
        # so an output link an agent pre-placed at a fixed `<name>.out` is never opened
        # for writing and nothing is written THROUGH it.
        fd, out_file = tempfile.mkstemp(prefix=path.name + ".", suffix=OUT_SUFFIX,
                                        dir=str(path.parent))
        with os.fdopen(fd, "wb") as fh:
            p = subprocess.Popen([bash_path(), "-c", cmd], cwd=str(path),
                                 stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                 **({"start_new_session": True} if os.name == "posix" else {}))
            timed_out = False
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill(p)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=10)
        return {"applied": True, "rc": p.returncode, "timed_out": timed_out,
                "output_tail": _text(pathlib.Path(out_file).read_bytes())[-TAIL:]}
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("run_cmd(%r) failed: %s" % (cmd, exc))
    finally:
        if out_file is not None:                     # the mkstemp file by its own name, so
            with contextlib.suppress(OSError):       # a stray `<name>.out` link is never
                os.unlink(out_file)                  # the thing deleted here
        remove(target, path)
