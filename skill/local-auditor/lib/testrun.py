"""qwen-test: the ONE shell command a local agent is ever granted.

It runs the test command from config -- never one the model wrote -- in a
throwaway git worktree, so a test's side effects (caches, generated files, a
stray rm) never touch the user's checkout. The model may only choose WHICH tests
run: selectors are passed as argv (no shell), and anything shell-shaped or
option-shaped is refused.

The worktree protects the checkout, not the machine: running tests runs the
repository's code. See reference/limits.md.
"""
import contextlib
import filecmp
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_TIMEOUT = 0, 1, 2, 5
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_BYTES = 20_000
_SEL_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./:\[\]=,+ -]*$")
_EXPR_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .()\[\]-]*$")
# Lines worth putting FIRST in a trimmed excerpt. A test command that never
# reached the runner still fails, and its reason is in compiler output: rustc,
# gcc and clang print lowercase ": error:" / "error[E0425]" diagnostics, jest
# prints its diff as "Expected" / "Received" lines, and `go test` marks a failing
# test with "--- FAIL". Everything above is kept: this only ever widens the net.
_FAIL_LINE = re.compile(
    r"FAILED|ERROR|Error|assert|panicked|\bFAIL\b|not ok"
    r"|: error:|: fatal error:|error\[[A-Z][0-9A-Z]*\]"
    r"|\bExpected\b|\bReceived\b"
    r"|--- FAIL"
)
USAGE = """usage: qwen-test [SELECTOR...]
Runs the configured test command (QWEN_TEST_CMD) in a throwaway git worktree.
SELECTOR is a test id or path, or '-k EXPR'. Nothing else is accepted."""


def _check_path_shape(t):
    if t.startswith("/") or re.match(r"^[A-Za-z]:", t) or ".." in t.replace("\\", "/").split("/"):
        raise ValueError("refused selector %r: absolute paths and '..' are not allowed" % t)


def validate_selectors(tokens):
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t == "-k":
            if i + 1 >= len(tokens):
                raise ValueError("-k needs an expression")
            e = tokens[i + 1]
            if not _EXPR_OK.fullmatch(e):     # fullmatch: $ alone lets a trailing newline through
                raise ValueError("refused -k expression %r: only words, spaces and parentheses" % e)
            out += ["-k", e]
            i += 2
            continue
        if not _SEL_OK.fullmatch(t):
            raise ValueError("refused selector %r: not a plain test id or path "
                             "(no options, @files or shell characters; only -k EXPR)" % t)
        _check_path_shape(t)
        out.append(t)
        i += 1
    return out


def build_argv(cmd, selectors):
    base = shlex.split(cmd or "")
    if not base:
        raise ValueError("the test command (QWEN_TEST_CMD) is empty")
    return base + list(selectors)


def _git(repo, *args, check=True):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args[0], (p.stderr or "").strip()))
    return p


def toplevel(path):
    p = _git(path, "rev-parse", "--show-toplevel", check=False)
    if p.returncode != 0:
        raise RuntimeError("not inside a git repository: %s" % path)
    top = p.stdout.strip()
    if _git(top, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
        raise RuntimeError("the repository has no commits yet: %s" % top)
    return top


def worktree_root():
    explicit = os.environ.get("QWEN_TEST_WORKTREES")
    if explicit:
        return explicit
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(xdg, "qwen-agent", "worktrees")


def make_worktree(repo, dest, commit="HEAD"):
    _git(repo, "worktree", "add", "--detach", "-f", dest, commit)


def remove_worktree(repo, dest):
    root = os.path.realpath(worktree_root())
    real = os.path.realpath(dest)
    if real == root or not real.startswith(root.rstrip(os.sep) + os.sep):
        raise RuntimeError("refusing to remove %s: not inside %s" % (dest, root))
    _git(repo, "worktree", "remove", "--force", dest, check=False)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.rmtree(dest + ".lock", ignore_errors=True)       # left by older versions
    with contextlib.suppress(OSError):
        os.remove(dest + ".flock")


def prepare(repo):
    """A new persistent worktree at HEAD under worktree_root(); returns its path."""
    os.makedirs(worktree_root(), exist_ok=True)
    # realpath: the path becomes a permission rule (Write(//<worktree>/**)), and
    # Claude Code matches rules against the resolved path -- on macOS a temp dir
    # under /var is really /private/var, and the unresolved rule never matches.
    dest = os.path.realpath(tempfile.mkdtemp(prefix="wt-", dir=worktree_root()))
    os.rmdir(dest)
    make_worktree(repo, dest)
    return dest


def _tree_files(repo):
    out = _git(repo, "ls-files", "-z", "-co", "--exclude-standard").stdout
    return [p for p in out.split("\0") if p]


def sync_tree(src, dst):
    """Make the worktree's tracked files match the source's current files.

    Copies new/changed files over; deletes files that are tracked at the
    worktree's HEAD but gone from the source. Files only the worktree has and
    git does not track (an auditor's reproduction tests) survive. Symlinks are
    never copied or written through.
    """
    for rel in _tree_files(src):
        s, d = os.path.join(src, rel), os.path.join(dst, rel)
        if os.path.islink(s) or not os.path.isfile(s) or os.path.islink(d):
            continue
        if os.path.isfile(d) and filecmp.cmp(s, d, shallow=False):
            continue
        os.makedirs(os.path.dirname(d) or dst, exist_ok=True)
        shutil.copy2(s, d)
    out = _git(dst, "ls-files", "-z").stdout
    for rel in (p for p in out.split("\0") if p):
        if not os.path.lexists(os.path.join(src, rel)):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(dst, rel))


_NOISE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
               "node_modules", "target", ".gradle"}


def changed_files(src, wt):
    """Worktree files that are absent from, or differ from, the source tree.

    Test-run debris (caches, bytecode) is skipped even when the repo does not
    gitignore it, so '## REPRO FILES' lists what the auditor actually wrote.
    """
    out = []
    for rel in _tree_files(wt):
        parts = rel.replace("\\", "/").split("/")
        if _NOISE_DIRS.intersection(parts) or rel.endswith((".pyc", ".pyo")):
            continue
        w, s = os.path.join(wt, rel), os.path.join(src, rel)
        if not os.path.isfile(w):
            continue
        if not os.path.isfile(s) or not filecmp.cmp(w, s, shallow=False):
            out.append(rel.replace("\\", "/"))
    return sorted(out)


def _try_lock(fd):
    """Take an exclusive OS lock on fd without blocking; False if another process holds it.

    The kernel releases the lock when the holder exits, however it exits, so a dead
    holder never needs stealing (the mkdir+pid lock this replaces could not be stolen
    when its owner died before writing the pid, and two waiters could both steal it)."""
    if os.name == "posix":
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            return False
        return True
    import msvcrt
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock(fd):
    if os.name == "posix":
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def _lock(wt, timeout):
    # A file next to the worktree, never deleted while runs may use it: unlinking a
    # locked file would let the next taker lock a fresh inode alongside the holder.
    path, deadline = wt + ".flock", time.time() + min(timeout, 300)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        while not _try_lock(fd):
            if time.time() > deadline:
                raise RuntimeError("another qwen-test run holds %s" % path)
            time.sleep(0.2)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _child_env():
    env = dict(os.environ)
    for var, orig in (("PYTHONPATH", "QWEN_TEST_ORIG_PYTHONPATH"),
                      ("PYTHONUTF8", "QWEN_TEST_ORIG_PYTHONUTF8")):
        val = env.pop(orig, None)
        if val is None:
            continue
        if val == "__unset__":
            env.pop(var, None)
        else:
            env[var] = val
    return env


def _kill_group(p):
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGKILL)
    else:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                       capture_output=True, check=False)
        with contextlib.suppress(OSError):
            p.kill()


RUN_OUTPUT_CAP = 8 * 1024 * 1024    # bytes of test output kept in memory: head + tail


def _read_capped(fh, cap=None):
    """The file's text, or its first quarter-cap and last three-quarter-cap with a marker."""
    cap = RUN_OUTPUT_CAP if cap is None else cap
    size = fh.seek(0, os.SEEK_END)
    fh.seek(0)
    if size <= cap:
        return fh.read().decode("utf-8", "replace")
    head = fh.read(cap // 4)
    fh.seek(size - (cap - cap // 4))
    tail = fh.read()
    return (head.decode("utf-8", "replace") + "\n... [%d bytes of output not kept] ...\n" % (size - cap)
            + tail.decode("utf-8", "replace"))


def _run(argv, cwd, timeout):
    """(returncode or None on timeout, combined output). Kills the whole process group.

    Output goes to a temporary file, not a pipe: a test suite that prints gigabytes is
    read back bounded (RUN_OUTPUT_CAP), and a grandchild that keeps the pipe open after
    a timeout can no longer stall the read."""
    kw = {"start_new_session": True} if os.name == "posix" else {}
    # In the worktree root (on disk, next to the worktrees), not $TMPDIR, which may be a
    # RAM-backed tmpfs: a runaway test can only fill the disk before its timeout.
    os.makedirs(worktree_root(), exist_ok=True)
    with tempfile.TemporaryFile(dir=worktree_root()) as out:
        try:
            p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, env=_child_env(), **kw)
        except OSError as exc:
            return 127, "cannot run the test command %r: %s" % (argv[0], exc)
        try:
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(p)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=5)
                rc = None
            return rc, _read_capped(out)
        finally:
            _kill_group(p)       # also on Ctrl-C/SIGTERM, and reaps stray grandchildren


def _trim(text, max_bytes):
    """Failure lines first, then head and tail; strictly within max_bytes."""
    b = text.encode("utf-8", "replace")
    if len(b) <= max_bytes:
        return text
    fails = [l[:300] for l in text.splitlines() if _FAIL_LINE.search(l)][:40]
    fail_part = ""
    if fails:
        fail_part = "\n".join(["FAILURE LINES:"] + fails + [""])
        fail_part = fail_part.encode("utf-8", "replace")[: max_bytes // 4].decode("utf-8", "ignore")
    marker = "\n... [%d bytes trimmed] ...\n" % len(b)
    budget = max(0, max_bytes - len(fail_part.encode()) - len(marker.encode()))
    head = b[: budget // 3].decode("utf-8", "ignore")
    tail = b[len(b) - (budget - len(head.encode())):].decode("utf-8", "ignore") if budget else ""
    out = fail_part + ("\n" if fail_part else "") + head + marker + tail
    return out.encode()[:max_bytes].decode("utf-8", "ignore")


def run_tests(selectors, *, cmd, source, worktree, timeout, max_bytes):
    sel = validate_selectors(selectors)
    argv = build_argv(cmd, sel)
    src = toplevel(source)
    own = worktree is None
    wt = prepare(src) if own else worktree
    try:
        with _lock(wt, timeout):
            sync_tree(src, wt)
            rc, out = _run(argv, wt, timeout)
    finally:
        if own:
            remove_worktree(src, wt)
    if rc is None:
        status = "TIMEOUT"
    elif rc == 0:
        status = "PASSED"
    elif rc == 127:
        status = "ERROR"
    else:
        status = "FAILED"
    first = ""
    if status != "PASSED":
        first = next((l.strip() for l in out.splitlines() if _FAIL_LINE.search(l)), "")
        if not first and status == "TIMEOUT":
            first = "no result within %ds" % timeout
    head = "TEST %s %s%s" % (" ".join(sel) or "ALL", status, (": " + first[:200]) if first else "")
    code = {"PASSED": EXIT_PASS, "TIMEOUT": EXIT_TIMEOUT}.get(status, EXIT_FAIL)
    return code, head + "\n" + _trim(out, max_bytes)


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        # --selectors (what qwen-test.sh passes): every later arg is a selector, so
        # the maintenance modes below are unreachable from the agent.
        selectors_only = argv[:1] == ["--selectors"]
        if selectors_only:
            argv = argv[1:]
        if not selectors_only and argv[:1] in (["-h"], ["--help"]):
            print(USAGE)
            return 0
        if not selectors_only and argv[:1] == ["--prepare"] and len(argv) == 2:
            print(prepare(toplevel(argv[1])))
            return 0
        if not selectors_only and argv[:1] == ["--cleanup"] and len(argv) == 3:
            remove_worktree(argv[1], argv[2])
            return 0
        if not selectors_only and argv[:1] == ["--changed"] and len(argv) == 3:
            print("\n".join(changed_files(argv[1], argv[2])))
            return 0
        cmd = (os.environ.get("QWEN_TEST_CMD") or "").strip()
        if not cmd:
            print("qwen-test: no test command configured (set QWEN_TEST_CMD in the config)",
                  file=sys.stderr)
            return EXIT_USAGE
        timeout = int(os.environ.get("QWEN_TEST_TIMEOUT") or DEFAULT_TIMEOUT)
        if timeout <= 0:
            raise ValueError("QWEN_TEST_TIMEOUT must be positive")
        code, text = run_tests(
            argv, cmd=cmd, source=os.environ.get("QWEN_TEST_SOURCE") or os.getcwd(),
            worktree=os.environ.get("QWEN_TEST_WORKTREE") or None,
            timeout=timeout,
            max_bytes=int(os.environ.get("QWEN_TEST_MAX_BYTES") or DEFAULT_MAX_BYTES))
        print(text)
        return code
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError) as exc:
        print("qwen-test: %s" % exc, file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
