import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from lib.swarm_engine import runlock


def dead_pid():
    """The pid of a process that has exited (and been reaped)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def write_lock(run, pid, host=None):
    run.mkdir(parents=True, exist_ok=True)
    (run / ".lock").write_text(json.dumps({"pid": pid, "host": host or socket.gethostname(),
                                           "started": "2026-10-08T00:00:00+00:00"}),
                               encoding="utf-8")


def test_acquire_writes_pid_host_started_and_a_second_acquire_is_refused(tmp_path):
    runlock.acquire(tmp_path)
    data = json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid() and data["host"] == socket.gethostname()
    assert data["started"].endswith("+00:00")
    with pytest.raises(runlock.RunLive) as e:
        runlock.acquire(tmp_path)
    assert e.value.pid == os.getpid() and e.value.host == socket.gethostname()
    assert str(e.value) == "run is live (pid %d); if no runner is running, delete %s" % (
        os.getpid(), tmp_path / ".lock")
    runlock.release(tmp_path)
    assert not (tmp_path / ".lock").exists()


def test_live_lock_message_names_the_remedy(tmp_path):
    # the message has to say what to do with a lock nobody can clear for you: --resume
    # removes a stale one itself, but a holder on another host leaves the file here
    runlock.acquire(tmp_path)
    with pytest.raises(runlock.RunLive) as e:
        runlock.acquire(tmp_path)
    remedy = "; if no runner is running, delete %s" % (tmp_path / ".lock")
    assert str(e.value).endswith(remedy)
    assert str(e.value.lock_path) == str(tmp_path / ".lock")
    runlock.release(tmp_path)
    # a holder read from somewhere else (no lock_path given) keeps the bare message
    assert str(runlock.RunLive(4321, "some-other-host")) == \
        "run is live (pid 4321 on host some-other-host)"
    assert str(runlock.RunLive(None, None)) == "run is live (pid ?)"


def test_release_leaves_another_processes_lock_alone(tmp_path):
    write_lock(tmp_path, dead_pid())
    runlock.release(tmp_path)
    assert (tmp_path / ".lock").exists()
    runlock.release(tmp_path / "nowhere")                 # no lock, no folder: no error


def test_is_live(tmp_path):
    assert not runlock.is_live(tmp_path)                  # no lock
    write_lock(tmp_path, os.getpid())
    assert runlock.is_live(tmp_path)                      # this very process
    write_lock(tmp_path, dead_pid())
    assert not runlock.is_live(tmp_path)                  # its pid is gone
    write_lock(tmp_path, dead_pid(), host="some-other-host")
    assert runlock.is_live(tmp_path)                      # cannot be checked: live
    assert "on host some-other-host" in str(runlock.RunLive(*runlock.holder(tmp_path)))
    (tmp_path / ".lock").write_text("", encoding="utf-8")
    assert runlock.is_live(tmp_path)                      # a writer mid-create: live ...
    old = time.time() - runlock.UNREADABLE_GRACE - 5
    os.utime(str(tmp_path / ".lock"), (old, old))
    assert not runlock.is_live(tmp_path)                  # ... until the grace ran out


def test_clear_stale_removes_only_a_dead_lock(tmp_path):
    said = []
    write_lock(tmp_path, os.getpid())
    assert runlock.clear_stale(tmp_path, said.append) is False
    assert (tmp_path / ".lock").exists() and said == []
    pid = dead_pid()
    write_lock(tmp_path, pid)
    assert runlock.clear_stale(tmp_path, said.append) is True
    assert not (tmp_path / ".lock").exists()
    assert said == ["removed stale lock (pid %d)" % pid]
    assert not (tmp_path / ".lock.clear").exists()
    assert runlock.clear_stale(tmp_path, said.append) is False      # nothing left to clear


def test_render_lock_serializes_and_breaks_a_stale_holder(tmp_path):
    order = []

    def second():
        with runlock.render_lock(tmp_path, wait=10):
            order.append("second")

    with runlock.render_lock(tmp_path):
        assert (tmp_path / ".render.lock").exists()
        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.3)
        order.append("first")
    t.join(10)
    assert order == ["first", "second"]
    assert not (tmp_path / ".render.lock").exists()
    (tmp_path / ".render.lock").write_text("{}", encoding="utf-8")   # a holder that died
    with pytest.raises(TimeoutError):
        with runlock.render_lock(tmp_path, wait=0.2):
            pass
    old = time.time() - 400
    os.utime(str(tmp_path / ".render.lock"), (old, old))
    with runlock.render_lock(tmp_path, wait=0.2):                   # older than 5 min: stale
        order.append("third")
    assert order[-1] == "third"


def test_stale_break_rechecks_before_unlink(tmp_path, monkeypatch):
    """Between judging the lock file stale and unlinking it, the first waiter to break
    that file may already have created a fresh one: re-check the file here is the one
    observed (same inode, same mtime) and never unlink a newer one."""
    path = tmp_path / ".lock.clear"
    path.write_text("{}", encoding="utf-8")
    old = time.time() - 400
    os.utime(str(path), (old, old))                      # the stale file of a dead waiter
    real_stat = runlock._stat
    replaced = []

    def stat_hook(p):
        st = real_stat(p)
        if st is not None and not replaced and str(p) == str(path):
            # right here, between this waiter's age check and its unlink, a first
            # waiter breaks the stale file and takes it with a fresh one
            path.unlink()
            fd = runlock._open_excl(path)                # O_EXCL create: a new inode
            os.write(fd, b"fresh")
            os.close(fd)
            replaced.append(st)
        return st

    monkeypatch.setattr(runlock, "_stat", stat_hook)
    with pytest.raises(TimeoutError):                    # the fresh file: waited out
        with runlock._exclusive(path, wait=0.2, stale=30.0):
            pass
    assert path.read_bytes() == b"fresh"                 # and it survived


@pytest.mark.skipif(os.name != "nt", reason="the Windows OpenProcess branch")
def test_pid_alive_on_windows():
    assert runlock.pid_alive(os.getpid())
    assert not runlock.pid_alive(dead_pid())


def test_pid_alive_rejects_junk():
    for pid in (None, 0, -1, True, "12", 10 ** 20, 2 ** 31):
        assert runlock.pid_alive(pid) is False
    assert runlock.pid_alive(os.getpid())
