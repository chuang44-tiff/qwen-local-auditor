"""The main session's side of asking an agent (spec 11.2): lib/notes.py's
ask and answer modes and the qwen-agent.sh --ask/--answer dispatch.

The CLI runs as a child, exactly the way test_notes.py drives --send: a watch
pointer under the test's own XDG_CACHE_HOME (tmp_path/cache -- the real watch
dir is never touched), an inbox made with notes.init_inbox and the agent's msg
dir made with msgdir.init_msgdir. The agent's answer is simulated by calling
msgdir.publish_reply from a thread after a short delay. The one deliberate
in-test pause is --wait 1; no test asserts it finished under 10 s (Global
Constraints), and the looks-once case runs under a 60 s subprocess timeout."""
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

from lib import msgdir, notes, watch
from test_cli import AGENT, BASH, posix
from test_notes import NOTES_PY, RUN_ID, env_for, live_agent, run_cli, write_pointer


@pytest.fixture
def note_env(tmp_path, monkeypatch):
    """One watch dir for both halves (test_notes.py's twin): the in-process lib and
    the CLI child resolve pointers, inboxes and msg dirs under this test's cache
    home -- never the developer's, never the real watch dir."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("QWEN_CONFIG", str(tmp_path / "no-config"))
    return cache, env_for(tmp_path)


def init_agent_msgdir():
    """The single-shot's msg dir beside its pointer: <watch_dir>/<id>.msg, made
    for real so publish_reply has its folders (the CLI resolves the same path
    through msg_dir_for -> msgdir.single_msgdir)."""
    m = msgdir.single_msgdir(watch.watch_dir(), RUN_ID)
    msgdir.init_msgdir(m)
    return m


def next_line(p, what):
    """One stdout line from a pipe-backed child; a child that exited before
    printing fails here with its rc in the message instead of hanging."""
    line = p.stdout.readline()
    assert line, "the child printed nothing (expected %s); rc %r" % (what, p.poll())
    return line.rstrip("\n")


@contextlib.contextmanager
def ask_child(env, tmp_path, args):
    """`python NOTES_PY ask ...` as a pipe-backed child; a 60 s watchdog kills
    any child still running when the test ends (generous: no wall-clock bound
    under 10 s)."""
    cmd = [sys.executable, str(NOTES_PY)] + list(args)
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                         cwd=str(tmp_path))

    def _kill():
        with contextlib.suppress(OSError):
            p.kill()

    timer = threading.Timer(60.0, _kill)
    timer.daemon = True
    timer.start()
    try:
        yield p
    finally:
        timer.cancel()
        _kill()
        p.wait(timeout=30)


def launcher_env(tmp_path, extra=None):
    """A launcher child env built from scratch (test_messaging_wiring's hygiene):
    no developer QWEN_*/CLAUDE_*/ANTHROPIC_* setting leaks in, and the watch home
    is this test's own cache dir."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
    env["QWEN_CONFIG"] = posix(tmp_path / "no-such-config")
    env["QWEN_NO_NOTICE"] = "1"
    env["QWEN_DEPTH"] = "shallow"
    env["QWEN_WATCH"] = "0"
    env["QWEN_WATCH_NOSERVE"] = "1"
    env["QA_SUPERVISED"] = "0"
    env["XDG_CACHE_HOME"] = posix(tmp_path / "cache")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


def launcher(tmp_path, env, *args, timeout=90):
    """qwen-agent itself, run as the main session would run it."""
    return subprocess.run([BASH, posix(AGENT), *[str(a) for a in args]], env=env,
                          capture_output=True, encoding="utf-8", errors="replace",
                          cwd=str(tmp_path), timeout=timeout)


ANSWER_LINE = r"answer from %s \(\d{2}:\d{2}:\d{2}\):"   # % the escaped address


# ================================================================ --ask

def test_ask_answered_prints_reply(note_env, tmp_path):
    _cache, env = note_env
    addr, inbox = live_agent(_cache)
    m = init_agent_msgdir()
    answer_text = "two rounds left; the failing check is the encoding one"
    with ask_child(env, tmp_path, ["ask", addr, "--wait", "20", "how far are you?"]) as p:
        first = next_line(p, "the asked line")
        assert first.startswith("asked ")
        nid = first[len("asked "):]
        assert notes.NOTE_ID_RE.fullmatch(nid), nid
        published = {}

        def answer():                              # the agent's reply tool, in a thread
            time.sleep(0.5)                        # a short delay; the child polls every 1 s
            published["ok"] = msgdir.publish_reply(m, nid, answer_text)

        th = threading.Thread(target=answer)
        th.start()
        out, err = p.communicate(timeout=30)
        th.join(timeout=30)
    assert published.get("ok") is True
    assert p.returncode == 0, err
    lines = out.splitlines()
    assert len(lines) == 2, repr(out)
    assert re.fullmatch(ANSWER_LINE % re.escape(addr), lines[0]), lines[0]
    assert lines[1] == answer_text
    body = json.loads((inbox / notes.NEW / ("%s.json" % nid)).read_text(encoding="utf-8"))
    assert body["kind"] == "ask"                   # the queued note is an ask
    assert body["from"] == "main"
    assert body["to"] == addr
    assert body["text"] == "how far are you?"


def test_ask_no_answer_is_10(note_env, tmp_path):
    _cache, env = note_env
    addr, _inbox = live_agent(_cache)
    init_agent_msgdir()
    res = run_cli(env, tmp_path, ["ask", addr, "--wait", "1", "progress?"], timeout=60)
    assert res.returncode == 10, res.stderr        # --wait 1 and nobody ever answers
    assert res.stdout.startswith("asked ")
    nid = res.stdout.split()[1]
    assert notes.NOTE_ID_RE.fullmatch(nid), nid
    assert res.stderr.strip() == "no answer yet: %s" % nid


def test_ask_ended_without_answer_is_11(note_env, tmp_path):
    _cache, env = note_env
    addr, inbox = live_agent(_cache)
    init_agent_msgdir()
    with ask_child(env, tmp_path, ["ask", addr, "--wait", "20", "progress?"]) as p:
        first = next_line(p, "the asked line")
        nid = first[len("asked "):]
        assert notes.NOTE_ID_RE.fullmatch(nid), nid
        notes.close_inbox(str(inbox), "run_ended")  # the agent ends mid-wait, unanswered
        _out, err = p.communicate(timeout=30)
    assert p.returncode == 11, err
    assert err.strip() == "the agent ended without answering"


def test_ask_bad_address_2_or_3(note_env, tmp_path):
    _cache, env = note_env
    res = run_cli(env, tmp_path, ["ask", "not-an-id", "hello"], timeout=60)
    assert res.returncode == 2, res.stderr          # malformed address: usage
    assert res.stdout == ""
    res = run_cli(env, tmp_path, ["ask", "aabbccddeeff", "hello"], timeout=60)
    assert res.returncode == 3, res.stderr          # 12 hex, no pointer: unavailable
    assert res.stdout == ""


# ================================================================ --answer

def test_answer_after_run_ended(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    m = init_agent_msgdir()
    nid = notes.queue_note(str(inbox), addr, "still stuck on the encoding?", kind="ask")
    nid_long = notes.queue_note(str(inbox), addr, "report everything", kind="ask")
    assert set(notes.close_inbox(str(inbox), "run_ended")) == {nid, nid_long}
    assert msgdir.publish_reply(m, nid, "no: one round left, then the fix") is True
    assert msgdir.publish_reply(m, nid_long, "x" * 4001) is True  # beyond the 4000 limit
    write_pointer(cache, RUN_ID, kind="agent", end=time.time())  # the run is over
    res = run_cli(env, tmp_path, ["answer", addr, nid], timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr   # no running/pid/closed check
    lines = res.stdout.splitlines()
    assert len(lines) == 2, repr(res.stdout)
    assert re.fullmatch(ANSWER_LINE % re.escape(addr), lines[0]), lines[0]
    assert lines[1] == "no: one round left, then the fix"
    # the same fetch over the ended run prints text over 4000 characters cut, with
    # the cut notice as its last line
    res = run_cli(env, tmp_path, ["answer", addr, nid_long], timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = res.stdout.splitlines()
    assert len(lines) == 3, repr(res.stdout)                  # header, cut text, notice
    assert re.fullmatch(ANSWER_LINE % re.escape(addr), lines[0]), lines[0]
    assert lines[1] == "x" * 4000
    assert lines[2] == "... (cut at 4000 characters)"


def test_answer_unknown_id_is_3(note_env, tmp_path):
    _cache, env = note_env
    addr, _inbox = live_agent(_cache)
    init_agent_msgdir()
    bogus = notes.new_note_id()                    # well-formed id, never queued
    res = run_cli(env, tmp_path, ["answer", addr, bogus], timeout=60)
    assert res.returncode == 3, res.stderr
    assert res.stdout == ""
    assert res.stderr.strip() == "no such ask: %s" % bogus


def test_answer_default_wait_looks_once(note_env, tmp_path):
    _cache, env = note_env
    addr, inbox = live_agent(_cache)
    init_agent_msgdir()
    nid = notes.queue_note(str(inbox), addr, "how far?", kind="ask")   # asked, unanswered
    # no --wait: ONE look, never the 600 s default -- the 60 s subprocess timeout is
    # the bound (no test may assert a wall clock under 10 s)
    res = run_cli(env, tmp_path, ["answer", addr, nid], timeout=60)
    assert res.returncode == 10, res.stdout + res.stderr
    assert res.stderr.strip() == "no answer yet: %s" % nid
    # an explicit 0 is the same single look; SECONDS is an int 0..3600 (unlike
    # --ask/--send, where --wait is 1..3600 and 0 is usage)
    res = run_cli(env, tmp_path, ["answer", addr, nid, "--wait", "0"], timeout=60)
    assert res.returncode == 10, res.stdout + res.stderr
    assert res.stderr.strip() == "no answer yet: %s" % nid
    assert run_cli(env, tmp_path, ["answer", addr, nid, "--wait", "3601"],
                   timeout=60).returncode == 2
    assert run_cli(env, tmp_path, ["answer", addr, nid, "--wait", "x"],
                   timeout=60).returncode == 2
    assert run_cli(env, tmp_path, ["answer", addr], timeout=60).returncode == 2
    assert run_cli(env, tmp_path, ["ask", addr, "--wait", "0", "t"],
                   timeout=60).returncode == 2


def test_sh_dispatch_and_help_list_ask_answer(tmp_path):
    env = launcher_env(tmp_path)
    h = launcher(tmp_path, env, "--help")
    assert h.returncode == 0, h.stderr
    assert re.search(r"^\s*--ask ADDRESS TEXT\s", h.stdout, re.M), "--ask not in --help"
    assert re.search(r"^\s*--answer ADDRESS ID\s", h.stdout, re.M), "--answer not in --help"
    r = launcher(tmp_path, env, "--ask")            # the takeover passes it verbatim
    assert r.returncode == 2, r.stdout + r.stderr   # notes.py: ADDRESS is required


def test_answer_strips_escape_sequences(tmp_path, monkeypatch, capsys):
    from lib import notes as _n
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    _n._print_reply("abc", {"t": 0, "text": "ok\x1b[31mred\x07\nline2"})
    joined = "\n".join(out)
    assert "\x1b" not in joined and "\x07" not in joined
    assert "line2" in joined
