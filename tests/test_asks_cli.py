"""The sender-side CLI for asks (stage 1.5, task 2): lib/asks.py's main with the
next-ask and amend modes, and the qwen-agent.sh --next-ask/--amend dispatch.

The module half drives asks.main in-process (conftest puts skill/local-auditor on
sys.path); the shell half runs the real qwen-agent.sh over a registry pointer and
a run folder built under XDG_CACHE_HOME=tmp_path/cache, so nothing reaches the
real watch registry. Listener threads join with generous timeouts (never under
10 s). Symlink cases are POSIX-only and skip on Windows."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from lib import asks
from test_cli import AGENT, BASH, posix

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="symlinks and signals are POSIX")

RUN_ID = "a1b2c3d4e5f6"          # a watch id as watch.ID_RE demands: 12 lowercase hex
JOIN_TIMEOUT = 30                # generous on purpose: no wall-clock bound under 10 s

VALID_TASK = ("- [ ] value is good -- check: cmd true\n"
              "- [ ] the notes are updated -- check: none\n")


# ----------------------------------------------------------------- fixtures and helpers

def _spawn_dead_pid():
    """A pid that is provably dead: spawn, reap, return its number."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    p.kill()
    p.wait(timeout=10)
    return p.pid


def wait_for(pred, timeout=30, message="condition never became true"):
    deadline = time.time() + timeout
    while not pred():
        if time.time() > deadline:
            raise AssertionError("%s (after %.0f s)" % (message, timeout))
        time.sleep(0.05)


def write_pointer(tmp_path, run_dir, monkeypatch, **over):
    """The registry pointer an until-done run leaves behind, under the test's own
    XDG_CACHE_HOME (so the real watch dir is never read or written)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    wd = tmp_path / "cache" / "qwen-agent" / "watch"
    wd.mkdir(parents=True, exist_ok=True)
    p = {"id": RUN_ID, "kind": "until-done", "title": "t", "pid": os.getpid(),
         "run_path": str(run_dir), "start": round(time.time(), 3), "end": None}
    p.update(over)
    (wd / ("%s.json" % RUN_ID)).write_text(json.dumps(p), encoding="utf-8")


def setup(tmp_path, monkeypatch, *, has_test_cmd=True, ended=False, dead_pid=False):
    """An initialised run folder plus its registry pointer: the ambient QWEN_STREAM
    and QWEN_ASK_LEASE of the developer's shell never decide what the test sees."""
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)
    run = tmp_path / "run"
    assert asks.init_run(str(run), has_test_cmd) is True
    over = {}
    if ended:
        over["end"] = round(time.time(), 3)
    if dead_pid:
        over["pid"] = _spawn_dead_pid()
    write_pointer(tmp_path, run, monkeypatch, **over)
    return str(run)


def lease_path(run):
    return os.path.join(run, asks.ASKS_DIR, asks.LEASE_FILE)


def log_records(run):
    path = os.path.join(run, asks.ASKS_DIR, asks.LOG_FILE)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    return [json.loads(ln) for ln in data.decode("utf-8").splitlines() if ln.strip()]


def pending_path(run):
    return os.path.join(run, asks.AMEND_DIR, asks.PENDING_FILE)


def start_listener(*args):
    """asks.main on a helper thread (the signal handlers stay the main thread's:
    asks._install_lease_cleanup installs nothing off it). The thread records its
    rc and its own stdout/stderr into the returned box."""
    box = {}

    def body():
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                box["rc"] = asks.main(list(args))
        finally:
            box["out"], box["err"] = out.getvalue(), err.getvalue()
    th = threading.Thread(target=body)
    th.start()
    return box, th


def _base_env(tmp_path, extra=None):
    """A launcher child env built from scratch (test_messaging_wiring's hygiene),
    so nothing of the developer's QWEN_*/CLAUDE_*/ANTHROPIC_* settings reaches a
    test run -- and QWEN_ASK_LEASE never leaks in either."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
    env["QWEN_CONFIG"] = posix(tmp_path / "no-such-config")
    env["QWEN_NO_NOTICE"] = "1"
    env["QWEN_DEPTH"] = "shallow"
    env["QWEN_WATCH"] = "0"
    env["QWEN_WATCH_NOSERVE"] = "1"
    env["QA_SUPERVISED"] = "0"
    env["XDG_CACHE_HOME"] = posix(tmp_path / "cache")
    env.update(extra or {})
    return env


def cli(tmp_path, env, *args, input=None, timeout=90):
    """qwen-agent's own asks CLI, run as the main session would run it."""
    return subprocess.run([BASH, posix(AGENT), *[str(a) for a in args]], env=env,
                          capture_output=True, encoding="utf-8", errors="replace",
                          cwd=str(tmp_path), input=input, timeout=timeout)


# ========================================================= --next-ask, the module


def test_next_ask_returns_waiting_ask_and_claims(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    asks.write_lease(run, 9999)                      # a stale stamp never blocks us
    asked = asks.write_ask(run, 4, "the stuck round's body is never printed")
    assert asks.main(["next-ask", RUN_ID]) == 0
    cap = capsys.readouterr()
    expected = os.path.join(run, asks.ASKS_DIR, asks.CLAIMED, "4.md")
    assert cap.out == expected + "\n"              # the path, absolute, nothing else
    assert "never printed" not in cap.out          # the ask's text never reaches stdout
    assert os.path.isfile(expected)
    assert not os.path.exists(asked)               # moved by the claim, not copied
    lease = asks.read_lease(run)
    assert lease is not None and lease["pid"] == os.getpid()   # refreshed on the claim
    recs = log_records(run)
    assert [r["ev"] for r in recs] == ["claimed"]
    assert recs[0]["round"] == 4 and recs[0]["pid"] == os.getpid()


def test_next_ask_timeout_is_10(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    # a wait past the 590 margin below, so the until assertion can tell the
    # give-up's fresh stamp from the one written before the loop started
    assert asks.main(["next-ask", RUN_ID, "--wait", "12"]) == 10
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err.strip() == "no ask yet"
    # the give-up path refreshed the lease before leaving (spec 10.2 (4)): the
    # expiry is a fresh lease_seconds away, not the pre-loop stamp (which would
    # sit ~12 s into its life and fall under the bar)
    lease = asks.read_lease(run)
    assert lease is not None and lease["pid"] == os.getpid()
    assert lease["until"] >= time.time() + 590
    # SECONDS is an int 1..3600 (default 3600); --wait together with --release is 2
    assert asks.main(["next-ask", RUN_ID, "--wait", "0"]) == 2
    assert asks.main(["next-ask", RUN_ID, "--wait", "3601"]) == 2
    assert asks.main(["next-ask", RUN_ID, "--wait", "x"]) == 2
    assert asks.main(["next-ask", RUN_ID, "--wait"]) == 2
    assert asks.main(["next-ask", RUN_ID, "--wait", "1", "--release"]) == 2
    assert asks.main(["next-ask", RUN_ID, "--release", "--wait", "1"]) == 2
    capsys.readouterr()


def test_next_ask_exits_11_when_ended_appears(tmp_path, monkeypatch):
    run = setup(tmp_path, monkeypatch)
    box, th = start_listener("next-ask", RUN_ID, "--wait", "10")
    wait_for(lambda: os.path.lexists(lease_path(run)),
             message="the listener never stamped a lease")
    open(asks.ended_path(run), "a").close()                    # the loop ends mid-wait
    th.join(timeout=JOIN_TIMEOUT)
    assert not th.is_alive()
    assert box["rc"] == 11
    assert box["err"].strip() == "run ended"
    # the lease that names this pid went with the exit
    assert not os.path.lexists(lease_path(run))


def test_next_ask_ended_before_start_writes_no_lease(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch, ended=True)             # the pointer says end
    asked = asks.write_ask(run, 1, "never claimed")
    assert asks.main(["next-ask", RUN_ID]) == 11
    cap = capsys.readouterr()
    assert cap.out == ""
    assert not os.path.lexists(lease_path(run))                # no lease was stamped
    assert os.path.isfile(asked)                               # nothing was claimed
    assert os.listdir(os.path.join(run, asks.ASKS_DIR, asks.CLAIMED)) == []
    # RUN/asks/.ended reads as ended the same way, before anything is written
    write_pointer(tmp_path, run, monkeypatch)                  # end cleared
    open(asks.ended_path(run), "a").close()
    assert asks.main(["next-ask", RUN_ID]) == 11
    capsys.readouterr()
    assert not os.path.lexists(lease_path(run))
    assert os.path.isfile(asked)


def test_next_ask_dead_pid_is_11(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch, dead_pid=True)
    asked = asks.write_ask(run, 1, "never claimed")
    assert asks.main(["next-ask", RUN_ID]) == 11               # dead pid = ended run
    capsys.readouterr()
    assert not os.path.lexists(lease_path(run))
    assert os.path.isfile(asked)
    assert os.listdir(os.path.join(run, asks.ASKS_DIR, asks.CLAIMED)) == []


@posix_only
def test_next_ask_before_asks_dir_is_3(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    shutil.rmtree(os.path.join(run, asks.ASKS_DIR))
    assert asks.main(["next-ask", RUN_ID]) == 3                # no asks/ to claim from
    assert asks.read_lease(run) is None                        # nothing was stamped
    # a symlinked asks/ is refused the same way, and nothing lands behind the link
    run2 = tmp_path / "run2"
    run2.mkdir()
    outside = tmp_path / "outside"
    os.symlink(str(outside), str(run2 / asks.ASKS_DIR))
    write_pointer(tmp_path, run2, monkeypatch)
    assert asks.main(["next-ask", RUN_ID]) == 3
    capsys.readouterr()
    assert not outside.exists()


def test_next_ask_unknown_and_wrong_kind_is_3(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    pointer = tmp_path / "cache" / "qwen-agent" / "watch" / ("%s.json" % RUN_ID)
    pointer.unlink()                                           # no pointer at all
    assert asks.main(["next-ask", RUN_ID]) == 3
    write_pointer(tmp_path, run, monkeypatch, kind="agent")    # the wrong kind
    assert asks.main(["next-ask", RUN_ID]) == 3
    write_pointer(tmp_path, run, monkeypatch, run_path="")     # no run folder
    assert asks.main(["next-ask", RUN_ID]) == 3
    capsys.readouterr()


def test_next_ask_bad_id_is_2(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch)
    for bad in ("nothex123456", "ABCDEF123456", "a" * 11, "a" * 13,
                "%s/alpha" % RUN_ID, RUN_ID * 2, "x" * 1000, "abcdef12345 "):
        assert asks.main(["next-ask", bad]) == 2, bad          # 2 is USAGE, not 3
    assert asks.main(["next-ask"]) == 2                        # the ID is required
    assert asks.main(["next-ask", RUN_ID, RUN_ID]) == 2        # exactly one positional
    assert asks.main([]) == 2                                  # no mode at all
    assert asks.main(["bogus"]) == 2
    capsys.readouterr()


def test_two_listeners_claim_exclusively(tmp_path, monkeypatch):
    run = setup(tmp_path, monkeypatch)
    asks.write_ask(run, 2, "exactly one answerer gets this")
    a, ta = start_listener("next-ask", RUN_ID, "--wait", "2")
    b, tb = start_listener("next-ask", RUN_ID, "--wait", "2")
    ta.join(timeout=JOIN_TIMEOUT)
    tb.join(timeout=JOIN_TIMEOUT)
    assert not ta.is_alive() and not tb.is_alive()
    # the rename-claim is exclusive: exactly one 0, the other waits out its 10
    assert sorted([a["rc"], b["rc"]]) == [0, 10]
    claimed = os.listdir(os.path.join(run, asks.ASKS_DIR, asks.CLAIMED))
    assert claimed == ["2.md"]                                 # claimed once, not twice
    assert os.listdir(os.path.join(run, asks.ASKS_DIR, asks.NEW)) == []
    assert [r["ev"] for r in log_records(run)] == ["claimed"]  # logged once


def test_release_deletes_lease(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    asks.write_lease(run, 4242)                                # another pid's stamp
    assert asks.main(["next-ask", RUN_ID, "--release"]) == 0   # any pid's lease goes
    assert asks.read_lease(run) is None
    assert asks.main(["next-ask", RUN_ID, "--release"]) == 0   # no lease: still 0
    open(asks.ended_path(run), "a").close()                     # the run has ended...
    asks.write_lease(run, 777)
    assert asks.main(["next-ask", RUN_ID, "--release"]) == 0   # ...and it still works
    capsys.readouterr()
    assert asks.read_lease(run) is None


def test_listener_exits_10_after_release(tmp_path, monkeypatch):
    run = setup(tmp_path, monkeypatch)
    box, th = start_listener("next-ask", RUN_ID, "--wait", "10")
    wait_for(lambda: os.path.lexists(lease_path(run)),
             message="the listener never stamped a lease")
    assert asks.main(["next-ask", RUN_ID, "--release"]) == 0
    th.join(timeout=JOIN_TIMEOUT)
    assert not th.is_alive()
    assert box["rc"] == 10
    assert box["err"].strip() == "no ask yet"
    # the vanished path exits without re-stamping the lease it lost
    assert not os.path.lexists(lease_path(run))


def test_listener_refreshes_lease(tmp_path, monkeypatch):
    run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(asks, "REFRESH", 0.01)     # read at call time, so patchable
    box, th = start_listener("next-ask", RUN_ID, "--wait", "4")
    wait_for(lambda: asks.read_lease(run) is not None,
             message="the listener never stamped a lease")
    t1 = asks.read_lease(run)["t"]
    wait_for(lambda: (asks.read_lease(run) or {}).get("t", 0) > t1,
             message="the lease was never refreshed while waiting")
    th.join(timeout=JOIN_TIMEOUT)
    assert not th.is_alive()
    assert box["rc"] == 10                         # no ask: the wait ran out
    lease = asks.read_lease(run)                   # the give-up refresh left it ours
    assert lease is not None and lease["pid"] == os.getpid() and lease["t"] > t1


# ============================================================ --amend, the module


def test_amend_valid_is_queued(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch, has_test_cmd=True)
    task = tmp_path / "task.md"
    task.write_bytes(VALID_TASK.encode("utf-8"))      # bytes: no \r\n on Windows
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 0
    cap = capsys.readouterr()
    assert cap.out == "amend queued\n"
    assert cap.err == ""
    with open(pending_path(run), "rb") as fh:
        assert fh.read().decode("utf-8") == VALID_TASK   # the bytes, verbatim
    # a -f path with spaces is none of our business to mangle (edge: argv with gaps)
    spaced = tmp_path / "task with spaces.md"
    spaced.write_bytes(VALID_TASK.encode("utf-8"))
    assert asks.main(["amend", RUN_ID, "-f", str(spaced)]) == 0
    with open(pending_path(run), "rb") as fh:
        assert fh.read().decode("utf-8") == VALID_TASK
    capsys.readouterr()


def test_amend_invalid_is_2_and_writes_nothing(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    task = tmp_path / "task.md"
    task.write_text("- [ ] thing -- check: bogus\n", encoding="utf-8")
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "unknown check kind" in cap.err           # the parser's own message
    assert not os.path.lexists(pending_path(run))    # a parse error writes nothing


def test_amend_empty_checklist_is_2(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    task = tmp_path / "task.md"
    task.write_text("# Goal\n\nprose only, no checklist anywhere\n", encoding="utf-8")
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "no checklist items" in cap.err           # an empty checklist is a parse error
    assert not os.path.lexists(pending_path(run))


def test_amend_test_item_without_cmd_is_2(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch, has_test_cmd=False)
    task = tmp_path / "task.md"
    task.write_text("- [ ] cover it -- check: test pytest -q t.py\n", encoding="utf-8")
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "test command" in cap.err
    assert not os.path.lexists(pending_path(run))
    # the refusal fires when meta SAYS false -- an absent meta says nothing, and
    # nothing else refuses (the same file reaches the loop, which judges it anyway)
    os.remove(os.path.join(run, asks.AMEND_DIR, asks.META_FILE))
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 0
    assert os.path.lexists(pending_path(run))
    capsys.readouterr()


def test_amend_too_large_is_2(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    task = tmp_path / "task.md"
    task.write_bytes(b"x" * (asks.AMEND_MAX + 1))    # over the cap: usage 2
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "limit" in cap.err
    assert not os.path.lexists(pending_path(run))
    # the boundary itself is fine: "over AMEND_MAX" means > AMEND_MAX, and an
    # exactly-AMEND_MAX body is queued like any other (the "#" line is a comment
    # parse skips; the two added characters here are the "#" and the newline)
    pad = asks.AMEND_MAX - len(VALID_TASK.encode("utf-8")) - 2
    edge = tmp_path / "edge.md"
    edge.write_bytes((VALID_TASK + "#" + "x" * pad + "\n").encode("utf-8"))
    assert len(edge.read_bytes()) == asks.AMEND_MAX
    assert asks.main(["amend", RUN_ID, "-f", str(edge)]) == 0
    with open(pending_path(run), "rb") as fh:
        assert fh.read() == edge.read_bytes()
    capsys.readouterr()


def test_amend_ended_or_closed_is_3(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch, ended=True)   # the pointer says end
    task = tmp_path / "task.md"
    task.write_text(VALID_TASK, encoding="utf-8")
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 3
    assert not os.path.lexists(pending_path(run))
    write_pointer(tmp_path, run, monkeypatch)        # live again; now the inbox closes
    inbox = os.path.join(run, asks.INBOX_DIR)
    os.makedirs(inbox, exist_ok=True)
    open(os.path.join(inbox, asks.CLOSED_SENTINEL), "a").close()
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 3
    assert not os.path.lexists(pending_path(run))
    os.unlink(os.path.join(inbox, asks.CLOSED_SENTINEL))
    shutil.rmtree(os.path.join(run, asks.AMEND_DIR))  # no amend folder to queue into
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 3
    capsys.readouterr()
    assert not os.path.lexists(pending_path(run))


def test_amend_two_f_or_positional_is_2(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)
    f1 = tmp_path / "one.md"
    f1.write_text(VALID_TASK, encoding="utf-8")
    f2 = tmp_path / "two.md"
    f2.write_text(VALID_TASK, encoding="utf-8")
    assert asks.main(["amend", RUN_ID, "-f", str(f1), "-f", str(f2)]) == 2  # once only
    assert asks.main(["amend", RUN_ID, "here is text instead", "-f", str(f1)]) == 2
    assert asks.main(["amend", RUN_ID]) == 2                                # no -f
    assert asks.main(["amend", RUN_ID, "-f"]) == 2                          # -f operand
    assert asks.main(["amend", RUN_ID, "-f", str(f1), "extra"]) == 2        # stray tail
    assert asks.main([]) == 2                                               # no mode
    capsys.readouterr()
    assert not os.path.lexists(pending_path(run))


def test_amend_stdin(tmp_path, monkeypatch, capsys):
    run = setup(tmp_path, monkeypatch)

    def _pipe(data):                                    # what a piped stdin is: bytes
        return type("Stdin", (), {"buffer": io.BytesIO(data)})()

    monkeypatch.setattr(sys, "stdin", _pipe(VALID_TASK.encode("utf-8")))
    assert asks.main(["amend", RUN_ID, "-f", "-"]) == 0
    cap = capsys.readouterr()
    assert cap.out == "amend queued\n"
    with open(pending_path(run), "rb") as fh:
        assert fh.read().decode("utf-8") == VALID_TASK
    # an empty stdin parses to no checklist: usage 2, the queued file untouched
    monkeypatch.setattr(sys, "stdin", _pipe(b""))
    assert asks.main(["amend", RUN_ID, "-f", "-"]) == 2
    with open(pending_path(run), "rb") as fh:
        assert fh.read().decode("utf-8") == VALID_TASK
    # a lone non-UTF-8 byte is decoded leniently (the notes.py -f rule): the
    # replacement char rides into the queued file, the bytes are never refused
    monkeypatch.setattr(sys, "stdin", _pipe(VALID_TASK.encode("utf-8") + b"# caf\xe9 x\n"))
    assert asks.main(["amend", RUN_ID, "-f", "-"]) == 0
    cap = capsys.readouterr()
    assert cap.out == "amend queued\n"
    with open(pending_path(run), "rb") as fh:
        assert fh.read().decode("utf-8") == VALID_TASK + "# caf� x\n"


def test_stream_off_both_commands_are_3(tmp_path, monkeypatch, capsys):
    # QWEN_STREAM=0 made the loop build no asks/ and no amend/ layout (P5): both
    # commands find the run addressed but nothing of it on disk to talk to: 3.
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)
    monkeypatch.setenv("QWEN_STREAM", "0")
    run = tmp_path / "run"
    assert asks.init_run(str(run), True) is False
    assert not run.exists()
    write_pointer(tmp_path, run, monkeypatch)
    assert asks.main(["next-ask", RUN_ID]) == 3
    task = tmp_path / "task.md"
    task.write_text(VALID_TASK, encoding="utf-8")
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 3
    cap = capsys.readouterr()
    assert cap.out == ""
    assert not run.exists()                               # neither command built one


# ========================================================== the shell dispatch


def test_sh_next_ask_dispatch(tmp_path, monkeypatch):
    run = setup(tmp_path, monkeypatch)
    asks.write_ask(run, 3, "the round is stuck the same way twice")
    env = _base_env(tmp_path)
    r = cli(tmp_path, env, "--next-ask", RUN_ID)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == os.path.join(run, asks.ASKS_DIR, asks.CLAIMED, "3.md")
    assert "stuck the same way" not in r.stdout           # never the ask's text
    assert os.path.isfile(os.path.join(run, asks.ASKS_DIR, asks.CLAIMED, "3.md"))
    # no ask left: the wait runs out through the shell too -> 10, `no ask yet`
    r = cli(tmp_path, env, "--next-ask", RUN_ID, "--wait", "1")
    assert r.returncode == 10, r.stdout + r.stderr
    assert "no ask yet" in r.stderr
    # the grammar is asks.py's, verbatim after the mode (K9): a bad id is 2,
    # --wait with --release is 2, --release itself exits 0
    assert cli(tmp_path, env, "--next-ask", "not-an-id").returncode == 2
    assert cli(tmp_path, env, "--next-ask", RUN_ID,
               "--wait", "1", "--release").returncode == 2
    assert cli(tmp_path, env, "--next-ask", RUN_ID, "--release").returncode == 0


def test_sh_amend_dispatch_stdin(tmp_path, monkeypatch):
    run = setup(tmp_path, monkeypatch)
    env = _base_env(tmp_path)
    r = cli(tmp_path, env, "--amend", RUN_ID, "-f", "-", input=VALID_TASK)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "amend queued"
    with open(pending_path(run), "rb") as fh:          # a text-mode pipe on Windows
        got = fh.read().decode("utf-8")                # turns \n into \r\n; the loop
    assert got.replace("\r\n", "\n") == VALID_TASK     # normalises it on apply
    # -f FILE reaches the dispatch too (native_path converts only on Windows)
    task = tmp_path / "task.md"
    task.write_text(VALID_TASK, encoding="utf-8")
    assert cli(tmp_path, env, "--amend", RUN_ID, "-f", posix(task)).returncode == 0
    # no -f at all is usage 2
    assert cli(tmp_path, env, "--amend", RUN_ID).returncode == 2


def test_sh_help_lists_next_ask_and_amend(tmp_path):
    env = _base_env(tmp_path)
    r = cli(tmp_path, env, "--help")
    assert r.returncode == 0, r.stderr
    assert "--next-ask" in r.stdout
    assert "--amend" in r.stdout


def test_amend_queued_after_the_loop_closed_is_taken_back(tmp_path, monkeypatch, capsys):
    # The loop's end runs: close inbox -> move a leftover pending.md aside ->
    # report -> .ended. An --amend whose write lands after that move would be
    # read by nobody: it sees the closed inbox after writing, takes the file
    # back and exits 3 instead of claiming "amend queued".
    run = setup(tmp_path, monkeypatch)
    task = tmp_path / "task.md"
    task.write_text(VALID_TASK, encoding="utf-8")
    inbox = os.path.join(run, asks.INBOX_DIR)
    real_write = asks.safe_write

    def write_then_close(path, data, *a, **kw):
        real_write(path, data, *a, **kw)
        os.makedirs(inbox, exist_ok=True)
        open(os.path.join(inbox, asks.CLOSED_SENTINEL), "a").close()

    monkeypatch.setattr(asks, "safe_write", write_then_close)
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 3
    assert not os.path.lexists(pending_path(run))
    assert "amend queued" not in capsys.readouterr().out


def test_amend_taken_by_the_loop_before_close_is_queued(tmp_path, monkeypatch, capsys):
    # ...but when the loop already took the file (applied it, or moved it to
    # unapplied.md for the report) before the inbox closed, it IS queued: exit 0.
    run = setup(tmp_path, monkeypatch)
    task = tmp_path / "task.md"
    task.write_text(VALID_TASK, encoding="utf-8")
    inbox = os.path.join(run, asks.INBOX_DIR)
    real_write = asks.safe_write

    def write_then_loop_takes_it(path, data, *a, **kw):
        real_write(path, data, *a, **kw)
        assert asks.leftover_pending(run)            # the loop's end, moving it aside
        os.makedirs(inbox, exist_ok=True)
        open(os.path.join(inbox, asks.CLOSED_SENTINEL), "a").close()

    monkeypatch.setattr(asks, "safe_write", write_then_loop_takes_it)
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 0
    assert capsys.readouterr().out == "amend queued\n"
    assert os.path.exists(os.path.join(run, asks.AMEND_DIR, "unapplied.md"))


def test_amend_invalid_utf8_counts_its_decoded_size(tmp_path, monkeypatch, capsys):
    # invalid bytes decode to U+FFFD (3 bytes each): a file under the cap on disk
    # but over it once decoded is refused here, not queued for the loop to reject
    run = setup(tmp_path, monkeypatch)
    task = tmp_path / "task.md"
    task.write_bytes(VALID_TASK.encode("utf-8") + b"\xff" * (asks.AMEND_MAX // 2))
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 2
    assert not os.path.lexists(pending_path(run))
    capsys.readouterr()
