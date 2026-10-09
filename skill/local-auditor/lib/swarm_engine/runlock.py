"""The run lock: RUN/.lock says a runner process owns the run folder right now.

A run (fresh or --resume) creates RUN/.lock with O_CREAT|O_EXCL, holding {"pid", "host",
"started"}, and removes it when it ends. Live = the file exists and either names another
host (that process cannot be checked from here: it counts as live) or names a pid of this
host that is still running. A lock whose pid is gone is stale: --resume (and
--record-verdict) remove it with one stderr line. Two runners racing for one folder are
settled by O_EXCL: exactly one creates the file.

Pid liveness: POSIX asks os.kill(pid, 0). Windows must NOT: os.kill there terminates the
process whatever the signal number, so it asks OpenProcess + GetExitCodeProcess through
ctypes instead.

render_lock is a separate, short-lived lock (RUN/.render.lock) that serializes rewrites
of a finished run's results; a holder that died leaves a file older than `stale`
seconds, which the next caller removes.
"""
import contextlib
import datetime
import json
import os
import pathlib
import socket
import time

LOCK, RENDER_LOCK, CLEAR_LOCK = ".lock", ".render.lock", ".lock.clear"
# a lock file that exists but does not parse yet is a writer between its O_EXCL create
# and its one write: live for this long, stale after it
UNREADABLE_GRACE = 10.0
_BINARY = getattr(os, "O_BINARY", 0)          # Windows: no newline translation
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class RunLive(Exception):
    """The run folder is held by a live runner (pid on host). With `lock_path` (the
    RUN/.lock it was refused over) the message names the remedy: a lock whose runner is
    gone is removed by --resume itself, but one left by a runner on another host -- or by
    a process this host cannot read -- has to be deleted by hand."""

    def __init__(self, pid, host, lock_path=None):
        self.pid, self.host, self.lock_path = pid, host, lock_path
        where = "" if host in (None, _host()) else " on host %s" % host
        msg = "run is live (pid %s%s)" % (pid if pid is not None else "?", where)
        if lock_path:
            msg += "; if no runner is running, delete %s" % lock_path
        super().__init__(msg)


def _host():
    return socket.gethostname()


def pid_alive(pid):
    """True when a process with this pid runs on this host."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid > 0x7FFFFFFF:
        return False          # no os.kill/OpenProcess can name it: junk, like 0 or -1
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            # access denied: the process exists but belongs to someone else
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            # an exited process lingers while any handle is open: its exit code says so
            return code.value == _STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                 # it exists; another user owns it
    except OSError:
        return False
    return True


def _stat(path):
    """os.stat, or None when the path is gone or unreadable."""
    try:
        return os.stat(str(path))
    except OSError:
        return None


def _age(path):
    st = _stat(path)
    return None if st is None else time.time() - st.st_mtime


def holder(run_dir):
    """(pid, host) of RUN/.lock, (None, None) while it is unreadable, None when absent."""
    path = pathlib.Path(run_dir) / LOCK
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return (None, None)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return (None, None)
    if not isinstance(data, dict):
        return (None, None)
    pid = data.get("pid")
    pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
    host = data.get("host") if isinstance(data.get("host"), str) else None
    return (pid, host)


def is_live(run_dir):
    """The lock exists and its owner may still be running (see the module docstring)."""
    h = holder(run_dir)
    if h is None:
        return False
    pid, host = h
    if pid is None:
        age = _age(pathlib.Path(run_dir) / LOCK)
        return age is not None and age < UNREADABLE_GRACE
    if host != _host():
        return True
    return pid_alive(pid)


def _open_excl(path):
    return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _BINARY, 0o644)


def _record():
    return json.dumps({"pid": os.getpid(), "host": _host(),
                       "started": datetime.datetime.now(datetime.timezone.utc)
                       .isoformat(timespec="seconds")}).encode("utf-8")


def acquire(run_dir):
    """Create RUN/.lock for this process, or raise RunLive naming its holder."""
    path = pathlib.Path(run_dir) / LOCK
    try:
        fd = _open_excl(path)
    except (FileExistsError, PermissionError):
        # Windows refuses the create with PermissionError while a deleted file lingers
        h = holder(run_dir) or (None, None)
        raise RunLive(h[0], h[1], path) from None
    try:
        os.write(fd, _record())
    finally:
        os.close(fd)


def release(run_dir):
    """Remove RUN/.lock when it is this process's own; leave anyone else's alone."""
    h = holder(run_dir)
    if h is not None and h[0] == os.getpid() and h[1] == _host():
        with contextlib.suppress(OSError):
            (pathlib.Path(run_dir) / LOCK).unlink()


@contextlib.contextmanager
def _exclusive(path, wait, stale):
    """Hold `path` (O_EXCL) for the with-block: wait up to `wait` seconds for another
    holder, remove a file older than `stale` seconds (its holder died), TimeoutError
    when the wait runs out."""
    path = pathlib.Path(path)
    deadline = time.time() + wait
    while True:
        try:
            fd = _open_excl(path)
            break
        except (FileExistsError, PermissionError):
            st = _stat(path)
            age = None if st is None else time.time() - st.st_mtime
            if age is not None and age > stale:
                # between judging this file stale and unlinking it, the first waiter
                # to break it may already have removed it and created a fresh one:
                # unlink only while the file here is still the one observed -- same
                # inode, same mtime -- so a second waiter never removes the first's
                # fresh file
                now = _stat(path)
                if now is not None and now.st_ino == st.st_ino \
                        and now.st_mtime_ns == st.st_mtime_ns:
                    with contextlib.suppress(OSError):
                        path.unlink()
                continue
            if time.time() >= deadline:
                raise TimeoutError("%s is held by another process (waited %gs)" % (path, wait))
            time.sleep(0.05)
    try:
        os.write(fd, _record())
    finally:
        os.close(fd)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def clear_stale(run_dir, err):
    """Remove RUN/.lock when its pid is gone (or it stayed unreadable past the grace),
    saying so through err(); True when a lock was removed. The check and the removal
    run under RUN/.lock.clear, so two callers cannot both judge one lock stale and the
    second delete the lock the first has just taken."""
    run_dir = pathlib.Path(run_dir)
    if holder(run_dir) is None:
        return False
    with _exclusive(run_dir / CLEAR_LOCK, wait=10.0, stale=30.0):
        h = holder(run_dir)
        if h is None or is_live(run_dir):
            return False
        with contextlib.suppress(FileNotFoundError):
            (run_dir / LOCK).unlink()
    err("removed stale lock (pid %s)" % (h[0] if h[0] is not None else "?"))
    return True


def render_lock(run_dir, wait=30.0, stale=300.0):
    """Context manager on RUN/.render.lock: one re-render of a run's results at a time."""
    return _exclusive(pathlib.Path(run_dir) / RENDER_LOCK, wait, stale)
