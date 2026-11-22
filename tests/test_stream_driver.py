"""lib/stream_driver.py, phase 1: the driver between qwen-agent.sh and claude.

Every test drives the real driver as a child process (sys.executable DRIVER ... --
sys.executable tests/fake_claude_stream.py) and steers the fake with its env knobs:
FAKE_STREAM_MODE, FAKE_MARKER_DIR (witnesses the test waits on), FAKE_GATE_DIR
(parking lots the test releases) and FAKE_LINES_FILE (exactly what arrived on the
fake's stdin). Notes enter through lib/notes.py's own queue_note into the test's
inbox; nothing is timed by sleeping -- every wait is a marker/pid poll with a
generous deadline, and every driver is killed in a finally.
"""
import contextlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

import pytest

from lib import notes, stream_driver

ROOT = pathlib.Path(__file__).resolve().parents[1]
DRIVER = ROOT / "skill" / "local-auditor" / "lib" / "stream_driver.py"
FAKE = ROOT / "tests" / "fake_claude_stream.py"
RUN_ID = "a1b2c3d4e5f6"        # a watch id as watch.ID_RE demands: 12 lowercase hex

RECORD_KEYS = {"type", "subtype", "is_error", "num_turns", "duration_ms", "result",
               "session_id", "usage", "total_cost_usd", "permission_denials",
               "stream", "notes_delivered", "note_ids"}


def pid_gone(pid):
    """True when the pid is provably dead (POSIX os.kill(pid, 0); only the POSIX-
    guarded tests call it, so the Windows semantics never come up)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class Run:
    """One driver launch under test: an inbox, the fake's env, and the Popen.

    The child env is built from scratch (every QWEN_*/CLAUDE_*/ANTHROPIC_* developer
    setting goes), so nothing outside the test reaches the driver or the fake. With
    start=False the Popen is deferred: a test that must plant files in new/ (or take
    the spawn-failure SENTINEL route) writes them first, then calls launch()."""

    def __init__(self, tmp_path, *, mode="ok", prompt="fix the bug", defer=False,
                 poll="100", cap=None, activity=False, lines=True, gated=True,
                 claude_argv=None, env_extra=None, start=True):
        self.tmp = pathlib.Path(tmp_path)
        self.tmp.mkdir(parents=True, exist_ok=True)     # Run(tmp_path/"sub") is legitimate
        self.inbox = self.tmp / "inbox"
        notes.init_inbox(str(self.inbox))
        self.raw = self.tmp / "raw.jsonl"
        self.prompt = prompt
        self.prompt_file = self.tmp / "prompt.txt"
        self.prompt_file.write_text(prompt, encoding="utf-8")
        self.marker_dir = self.tmp / "markers"
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        self.gate_dir = self.tmp / "gates"
        self.gate_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file = self.tmp / "fake.pid"
        self.lines_file = self.tmp / "lines.jsonl"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
        env["FAKE_STREAM_MODE"] = mode
        env["FAKE_MARKER_DIR"] = str(self.marker_dir)
        env["FAKE_PID_FILE"] = str(self.pid_file)
        env["FAKE_LINES_FILE"] = str(self.lines_file)          # always: the witness file
        env["QWEN_NOTE_POLL"] = poll
        env["PYTHONIOENCODING"] = "utf-8"
        if gated:
            env["FAKE_GATE_DIR"] = str(self.gate_dir)
        if cap is not None:
            env["QWEN_NOTE_MAX_PER_RUN"] = str(cap)
        if activity:
            env["QWEN_WATCH_ACTIVITY"] = str(self.tmp / "activity.jsonl")
        env.update(env_extra or {})
        self.env = env
        # Windows hang diagnosis: start the driver under faulthandler so a driver that
        # outlives wait()'s deadline prints every thread's stack to its stderr first
        # (surfaced in the "driver hung" message). POSIX launches are unchanged.
        boot = ([sys.executable, "-c",
                 "import faulthandler, runpy, sys; faulthandler.dump_traceback_later(20);"
                 " sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"]
                if os.name == "nt" else [sys.executable])
        argv = boot + [str(DRIVER), "--inbox", str(self.inbox),
                "--prompt-file", str(self.prompt_file), "--raw", str(self.raw)]
        if defer:
            argv.append("--defer-on-end")
        argv.append("--")
        argv += list([sys.executable, str(FAKE)] if claude_argv is None else claude_argv)
        self.argv = argv
        self.p = None
        self.rc = None
        self.stderr = ""
        if start:
            self.launch()

    def launch(self):
        """Start the driver; every test that deferred with start= must call this."""
        self.p = subprocess.Popen(self.argv, env=self.env, cwd=str(self.tmp),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, text=True,
                                  encoding="utf-8", errors="replace")
        return self

    # ------------------------------------------------------------- witnesses
    def _poll_until(self, pred, timeout, message):
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() >= deadline:
                raise AssertionError("%s (after %.0f s)" % (message, timeout))
            time.sleep(0.05)

    def marker(self, name, timeout=20):
        """Wait for the fake to reach a step: its atomic marker file in marker_dir."""
        self._poll_until(lambda: (self.marker_dir / name).exists(), timeout,
                         "no marker %s" % name)

    def gate(self, name):
        """Release one of the fake's parking lots."""
        (self.gate_dir / name).touch()

    def queue(self, text="please steer", addr=RUN_ID):
        """Queue one note into the inbox through the very library the driver reads."""
        return notes.queue_note(str(self.inbox), addr, text)

    def log(self):
        return notes.read_log(str(self.inbox))

    def received(self):
        """The parsed FAKE_LINES_FILE: one dict per user line the fake received."""
        if not self.lines_file.exists():
            return []
        out = []
        for raw in self.lines_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                out.append(json.loads(raw))
        return out

    def pids(self):
        return json.loads(self.pid_file.read_text(encoding="utf-8"))

    def wait_for(self, pred, timeout=20):
        self._poll_until(pred, timeout, "condition never became true")

    # ------------------------------------------------------------- lifecycle
    def _recorded_pids(self):
        try:
            data = json.loads(self.pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        return [v for v in (data.get("pid"), data.get("child_pid")) if isinstance(v, int)]

    def _kill_all(self):
        with contextlib.suppress(OSError):
            if self.p is not None and self.p.poll() is None:
                self.p.kill()
        for pid in self._recorded_pids():
            with contextlib.suppress(OSError):
                # Windows has no SIGKILL; there os.kill with SIGTERM calls
                # TerminateProcess -- the same hard kill.
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))

    def wait(self, timeout=25):
        """The driver's exit code; a driver that will not exit is killed and the test
        fails with 'driver hung' instead of hanging the suite."""
        try:
            self.rc = self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_all()
            with contextlib.suppress(Exception):
                self.p.wait(timeout=10)
            self.stderr = self._read_stderr()
            raise AssertionError("driver hung; its stderr:\n%s" % self.stderr[-6000:])
        self.stderr = self._read_stderr()
        return self.rc

    def _read_stderr(self):
        if self.p is None or self.p.stderr is None:
            return ""
        try:
            return self.p.stderr.read()
        except (OSError, ValueError):
            return ""

    def dead(self, pid, timeout=5):
        self.wait_for(lambda: pid_gone(pid), timeout)

    def record(self):
        """The single JSON line of $RAW."""
        lines = [ln for ln in self.raw.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1, "raw is not exactly one record: %r" % lines
        return json.loads(lines[0])

    def cleanup(self):
        self._kill_all()
        if self.p is not None:
            with contextlib.suppress(Exception):
                self.p.wait(timeout=10)
            with contextlib.suppress(OSError):
                self.p.stderr.close()


# ================================================================ notes in, one record out

def test_note_mid_turn_forwarded_echoed_logged_and_merged(tmp_path):
    run = Run(tmp_path, mode="ok", gated=True, poll="0.2", activity=True)
    try:
        run.marker("m_tool_waited")
        nid = run.queue("please steer")
        run.marker("m_note_echoed")                 # the parked fake drained + echoed it
        run.gate("g_tool")
        run.gate("g_result1")
        assert run.wait() == 0
        got = run.received()
        assert len(got) == 2                        # the prompt, then the note: nothing else
        assert got[0]["message"]["content"] == "fix the bug"
        assert set(got[1]) == {"type", "message"}   # no other note keys leak into the line
        assert set(got[1]["message"]) == {"role", "content"}
        assert re.fullmatch(r"\[Note from main at \d{2}:\d{2}:\d{2} local time\]\n"
                            r"please steer", got[1]["message"]["content"]), repr(got[1])
        recs = [r for r in run.log() if r["id"] == nid]
        assert [r["state"] for r in recs] == ["queued", "delivered"]
        for rec in recs:
            assert "reason" not in rec
            assert rec["to"] == RUN_ID and rec["from"] == "main"
            assert rec["len"] == len("please steer")
        log_bytes = (run.inbox / notes.LOG_FILE).read_bytes()
        act_bytes = (run.tmp / "activity.jsonl").read_bytes()
        assert b"please steer" not in log_bytes     # the text is never in the log...
        assert b"please steer" not in act_bytes     # ...nor in the activity line
        act = [json.loads(ln) for ln in act_bytes.decode("utf-8").splitlines() if ln.strip()]
        assert len(act) == 1
        assert {k: v for k, v in act[0].items() if k != "t"} == {
            "ev": "note", "id": nid, "from": "main", "len": len("please steer")}
        assert sorted(os.listdir(str(run.inbox / notes.DELIVERED))) == [nid + ".json"]
        assert os.listdir(str(run.inbox / notes.NEW)) == []
        rec = run.record()
        assert rec["type"] == "result" and rec["is_error"] is False
        assert rec["session_id"] == "fake-stream-session-1"
        assert rec["num_turns"] == 1 and rec["duration_ms"] == 41
        assert rec["usage"]["input_tokens"] == 100 and rec["usage"]["output_tokens"] == 10
        assert rec["usage"]["cache_read_input_tokens"] == 5
        assert rec["usage"]["cache_creation"] == {"ephemeral_5m_input_tokens": 7}
        assert rec["total_cost_usd"] == 0.01
        assert rec["stream"] is True
        assert rec["notes_delivered"] == 1 and rec["note_ids"] == [nid]
        assert len(rec["permission_denials"]) == 2
        assert set(rec) == RECORD_KEYS
    finally:
        run.cleanup()


def test_note_header_uses_the_queued_time(tmp_path):
    run = Run(tmp_path, mode="ok", gated=True, poll="0.2")
    try:
        run.marker("m_tool_waited")
        nid = run.queue("please steer")
        # the file as it sits in new/ BEFORE the driver's move: its t is the queued time
        body = json.loads((run.inbox / notes.NEW / ("%s.json" % nid)).read_text(
            encoding="utf-8"))
        run.marker("m_note_echoed")
        run.gate("g_tool")
        run.gate("g_result1")
        assert run.wait() == 0
        got = run.received()
        assert got[1]["message"]["content"] == (
            "[Note from main at %s local time]\nplease steer" % notes.fmt_clock(body["t"]))
    finally:
        run.cleanup()


def test_merge_two_turns_sums_and_dedups(tmp_path):
    run = Run(tmp_path, mode="ok", gated=True, poll="100")
    try:
        run.marker("m_tool1")
        run.gate("g_tool")
        run.marker("m_pre_result1")
        time.sleep(1.0)   # let any late wake of the prompt echo run its scan while the
                          # inbox is still empty: from here the only wake is result 1's
        nid = run.queue()                            # invisible to the 100 s poll
        run.gate("g_result1")                        # the DRAIN scan forwards it: turn 2
        assert run.wait() == 0
        rec = run.record()
        assert rec["num_turns"] == 2
        assert rec["duration_ms"] == 83              # 41 + 42, per turn, summed
        assert rec["usage"]["input_tokens"] == 300   # 100 + 200
        assert rec["usage"]["output_tokens"] == 30
        assert rec["usage"]["cache_read_input_tokens"] == 10
        assert rec["usage"]["cache_creation"] == {"ephemeral_5m_input_tokens": 7}
        assert rec["total_cost_usd"] == 0.02         # LAST: the cost is cumulative
        assert rec["notes_delivered"] == 1 and rec["note_ids"] == [nid]
        den = rec["permission_denials"]
        assert len(den) == 3                         # tu-1 de-duplicated, both Reads kept
        assert sum(1 for d in den if d.get("tool_use_id") == "tu-1") == 1
        assert sum(1 for d in den if d.get("tool_name") == "Read") == 2
        recs = [r for r in run.log() if r["id"] == nid]
        assert [r["state"] for r in recs] == ["queued", "delivered"]
        assert sorted(os.listdir(str(run.inbox / notes.DELIVERED))) == [nid + ".json"]
    finally:
        run.cleanup()


def test_noteless_run_adds_only_stream_keys(tmp_path):
    run = Run(tmp_path, mode="ok", gated=True, poll="100")
    try:
        run.marker("m_prompt_echoed")
        run.gate("g_tool")
        run.gate("g_result1")
        assert run.wait() == 0
        rec = run.record()
        assert rec["notes_delivered"] == 0 and rec["note_ids"] == []
        assert rec["num_turns"] == 1 and rec["total_cost_usd"] == 0.01
        assert set(rec) == RECORD_KEYS
        log_path = run.inbox / notes.LOG_FILE
        assert not log_path.exists() or log_path.read_bytes().strip() == b""
    finally:
        run.cleanup()


# ================================================================ the close rule (P1)

def test_close_mid_turn_note_keeps_one_turn(tmp_path):
    run = Run(tmp_path, mode="ok", gated=True, poll="0.2")
    try:
        run.marker("m_tool_waited")
        nid = run.queue("please steer")
        run.marker("m_note_echoed")                  # echoed by the parked gate: no turn
        run.gate("g_tool")
        run.gate("g_result1")
        assert run.wait() == 0                       # the driver closed stdin by itself
        rec = run.record()
        assert rec["num_turns"] == 1                 # exactly one result merged
        assert rec["notes_delivered"] == 1 and rec["note_ids"] == [nid]
        assert not (run.marker_dir / "m_result2").exists()   # never a second turn
    finally:
        run.cleanup()


def test_close_note_just_before_result_starts_turn_two(tmp_path):
    run = Run(tmp_path, mode="ok", gated=True, poll="100")
    try:
        run.marker("m_tool1")
        run.gate("g_tool")
        run.marker("m_pre_result1")
        time.sleep(1.0)                              # as in the merge test: no stale wake
        run.queue()                                  # queued just before result 1
        run.gate("g_result1")
        assert run.wait() == 0
        rec = run.record()                           # the driver forwarded instead of closing
        assert rec["num_turns"] == 2
        assert rec["total_cost_usd"] == 0.02
        assert (run.marker_dir / "m_result2").exists()
    finally:
        run.cleanup()


def test_close_no_hang_without_notes(tmp_path):
    run = Run(tmp_path, mode="ok", gated=False, poll="1")
    try:
        assert run.wait(timeout=15) == 0             # no gates, no notes: no hang
        assert run.record()["num_turns"] == 1
    finally:
        run.cleanup()


# ================================================================ crashes and exit codes

def test_crash_before_result_leaves_raw_empty(tmp_path):
    run = Run(tmp_path, mode="crash_before_result", gated=False, poll="100")
    try:
        assert run.wait() == 1                       # claude exits 1, it propagates
        assert run.raw.exists()
        assert run.raw.read_text(encoding="utf-8") == ""   # no result ever -> empty $RAW
        assert run.log() == []                        # no note touched: no log at all
    finally:
        run.cleanup()


def test_crash_mid_turn2_error_during_turn_and_crashed_note(tmp_path):
    run = Run(tmp_path, mode="crash_mid_turn2", gated=True, poll="100")
    try:
        run.marker("m_tool1")
        run.gate("g_tool")
        run.marker("m_pre_result1")
        time.sleep(1.0)                              # as in the merge test: no stale wake
        nid = run.queue()
        run.gate("g_result1")
        assert run.wait() == 1
        rec = run.record()
        assert rec["is_error"] is True
        assert rec["subtype"] == "error_during_turn"
        assert rec["num_turns"] == 1                 # only result 1 ever arrived
        assert rec["notes_delivered"] == 0 and rec["note_ids"] == []
        recs = [r for r in run.log() if r["id"] == nid]
        assert [r["state"] for r in recs] == ["queued", "undelivered"]
        assert recs[1]["reason"] == "crashed"
        assert sorted(os.listdir(str(run.inbox / notes.UNDELIVERED))) == [nid + ".json"]
        assert os.listdir(str(run.inbox / notes.NEW)) == []
    finally:
        run.cleanup()


@pytest.mark.parametrize("code", [126, 127])
def test_exit_code_zero_output_126_and_127(tmp_path, code):
    run = Run(tmp_path, gated=False, poll="1",
              claude_argv=[sys.executable, "-c", "import sys; sys.exit(%d)" % code])
    try:
        assert run.wait() == code                    # zero output: the retry codes pass through
        assert run.raw.exists()
        assert run.raw.read_text(encoding="utf-8") == ""
    finally:
        run.cleanup()


def test_exit_code_output_then_126_is_one(tmp_path):
    run = Run(tmp_path, mode="exit126_after_output", gated=False, poll="1")
    try:
        assert run.wait() == 1                       # it wrote output: no retry path
        assert run.raw.exists()
        assert run.raw.read_text(encoding="utf-8") == ""
    finally:
        run.cleanup()


def test_exit_code_other_propagates(tmp_path):
    run = Run(tmp_path, gated=False, poll="1",
              claude_argv=[sys.executable, "-c", "import sys; sys.exit(42)"])
    try:
        assert run.wait() == 42
        assert run.raw.exists()
        assert run.raw.read_text(encoding="utf-8") == ""
    finally:
        run.cleanup()
    run2 = Run(tmp_path / "crash1", mode="crash_before_result", gated=False, poll="1")
    try:
        assert run2.wait() == 1
    finally:
        run2.cleanup()


def test_spawn_missing_and_unexecutable(tmp_path):
    # (a) a name that cannot exist: 127, and $RAW is never touched (not even emptied)
    run = Run(tmp_path / "missing", gated=False, poll="1",
              claude_argv=["/no/such/dir/claude-xyz"], start=False)
    run.raw.write_text("SENTINEL", encoding="utf-8")
    try:
        run.launch()
        assert run.wait() == 127
        assert run.raw.read_text(encoding="utf-8") == "SENTINEL"
    finally:
        run.cleanup()
    # (b) POSIX only: a file that exists but is not executable: 126, $RAW untouched too
    if os.name == "posix":
        script = tmp_path / "noexec.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o644)
        run2 = Run(tmp_path / "noexec", gated=False, poll="1", claude_argv=[str(script)],
                   start=False)
        run2.raw.write_text("SENTINEL", encoding="utf-8")
        try:
            run2.launch()
            assert run2.wait() == 126
            assert run2.raw.read_text(encoding="utf-8") == "SENTINEL"
        finally:
            run2.cleanup()


# ================================================================ signals (POSIX group kills)

POSIX_ONLY = pytest.mark.skipif(os.name != "posix",
                                reason="POSIX signal and process-group semantics")


@POSIX_ONLY
def test_signal_sigterm_exits_143_kills_group(tmp_path):
    run = Run(tmp_path, gated=True, poll="100")
    try:
        run.marker("m_tool_waited")
        pid = run.pids()["pid"]
        t0 = time.monotonic()
        run.p.send_signal(signal.SIGTERM)
        assert run.wait(timeout=10) == 143
        assert time.monotonic() - t0 < 6             # far inside the 2 s budget + slack
        run.dead(pid)                                # the fake died with its driver
        assert run.raw.read_text(encoding="utf-8") == ""   # no result yet: empty $RAW
    finally:
        run.cleanup()


@POSIX_ONLY
def test_signal_ignoreterm_killed_within_two_seconds(tmp_path):
    run = Run(tmp_path, mode="ignoreterm", gated=True, poll="100")
    try:
        run.marker("m_prompt_echoed")
        pid = run.pids()["pid"]
        run.p.send_signal(signal.SIGTERM)            # the fake shrugs it off
        assert run.wait(timeout=10) == 143           # the driver escalated to SIGKILL
        run.dead(pid)
        assert run.raw.read_text(encoding="utf-8") == ""
    finally:
        run.cleanup()


@POSIX_ONLY
def test_signal_spawn_child_leaves_no_process(tmp_path):
    run = Run(tmp_path, mode="spawn_child", gated=True, poll="100")
    try:
        run.marker("m_tool_waited")
        pids = run.pids()
        assert isinstance(pids["child_pid"], int)    # the decoy is really there
        run.p.send_signal(signal.SIGTERM)
        assert run.wait(timeout=10) == 143
        run.dead(pids["pid"])                        # the group SIGKILL took BOTH:
        run.dead(pids["child_pid"])                  # the decoy ignores SIGTERM itself
        assert run.raw.read_text(encoding="utf-8") == ""
    finally:
        run.cleanup()


# ================================================================ privacy, deadlocks, validity

def test_privacy_thinking_and_assistant_never_written(tmp_path):
    run = Run(tmp_path, mode="ok", gated=True, poll="0.2", activity=True)
    try:
        run.marker("m_tool_waited")
        run.queue("please steer")
        run.marker("m_note_echoed")
        run.gate("g_tool")
        run.gate("g_result1")
        assert run.wait() == 0
        banned = (b"ZZTHINKZZ", b"ZZASSISTZZ")       # the fake leaks them in events only
        for path in run.tmp.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                for token in banned:
                    assert token not in data, "%s holds %r" % (path, token)
        for token in banned:
            assert token.decode("ascii") not in run.stderr
    finally:
        run.cleanup()


def test_deadlock_one_megabyte_prompt(tmp_path):
    big = "p" * 1048576                              # 1 MiB through the stdin pipe
    run = Run(tmp_path, mode="ok", gated=False, poll="1", prompt=big)
    try:
        assert run.wait(timeout=40) == 0
        assert run.received()[0]["message"]["content"] == big
        assert run.record()["num_turns"] == 1
    finally:
        run.cleanup()


def test_deadlock_five_megabyte_stdout_line(tmp_path):
    run = Run(tmp_path, mode="big_line", gated=False, poll="1")
    try:
        assert run.wait(timeout=40) == 0             # the reader drains, never stalls
        assert run.record()["num_turns"] == 1
    finally:
        run.cleanup()


def test_invalid_note_moved_with_reason(tmp_path):
    run = Run(tmp_path, gated=False, poll="1", start=False)
    try:
        new = run.inbox / notes.NEW
        (new / "1700000000000000001-deadbe.json").write_bytes(b"{oops")
        (new / "1700000000000000002-abcdef.json").write_text(
            json.dumps({"id": "WRONG", "t": 1.0, "from": "main", "to": RUN_ID,
                        "text": "id does not match the stem"}), encoding="utf-8")
        (new / "1700000000000000003-bbbbbb.json").write_text(
            json.dumps({"id": "1700000000000000003-bbbbbb", "t": 1.0, "from": "main",
                        "to": RUN_ID, "text": "   "}), encoding="utf-8")   # empty after strip
        nid = run.queue("valid note")
        run.launch()
        assert run.wait(timeout=40) == 0
        junk = ["1700000000000000001-deadbe", "1700000000000000002-abcdef",
                "1700000000000000003-bbbbbb"]
        assert sorted(os.listdir(str(run.inbox / notes.UNDELIVERED))) == sorted(
            stem + ".json" for stem in junk)
        by_id = {}
        for rec in run.log():
            by_id.setdefault(rec["id"], []).append(rec)
        for stem in junk:
            assert [r["state"] for r in by_id[stem]] == ["undelivered"], stem
            assert by_id[stem][0]["reason"] == "invalid_note", stem
        assert [r["state"] for r in by_id[nid]] == ["queued", "delivered"]
        assert sorted(os.listdir(str(run.inbox / notes.DELIVERED))) == [nid + ".json"]
    finally:
        run.cleanup()


@POSIX_ONLY
def test_invalid_symlink_note_never_forwarded(tmp_path):
    run = Run(tmp_path, gated=False, poll="1", start=False)
    try:
        outside = tmp_path / "outside"
        outside.mkdir()
        stem = "1700000000000000004-cccccc"
        target = outside / (stem + ".json")
        target.write_text(json.dumps({"id": stem, "t": 1.0, "from": "main", "to": RUN_ID,
                                      "text": "a valid note somewhere else"}),
                          encoding="utf-8")
        link = run.inbox / notes.NEW / (stem + ".json")
        os.symlink(str(target), str(link))           # list_new skips links, touches none
        run.launch()
        assert run.wait(timeout=40) == 0
        assert os.path.islink(str(link))             # the link stands...
        assert target.exists()                       # ...and so does its target
        assert [r for r in run.log() if r["id"] == stem] == []   # never named in the log
        assert not (run.inbox / notes.UNDELIVERED / (stem + ".json")).exists()
    finally:
        run.cleanup()


# ================================================================ the per-run cap

def test_cap_20_is_the_default():
    assert stream_driver.DEFAULT_NOTE_MAX_PER_RUN == 20
    base = {"turn_in_flight": False, "pending": False, "unacked": False, "new": 0}
    assert stream_driver.close_policy(base) is True
    for key, truthy in (("turn_in_flight", True), ("pending", True),
                        ("unacked", True), ("new", 1)):
        flipped = dict(base)
        flipped[key] = truthy
        assert stream_driver.close_policy(flipped) is False, key


def test_cap_excess_notes_get_cap_reason(tmp_path):
    run = Run(tmp_path, gated=False, poll="0.2", start=False,
              env_extra={"QWEN_NOTE_MAX_PER_RUN": "3"})
    try:
        ids = [run.queue("steer around the corner %d" % i) for i in range(1, 6)]
        run.launch()
        assert run.wait(timeout=40) == 0
        rec = run.record()
        assert rec["notes_delivered"] == 3            # the budget: the first three, in order
        assert len(rec["note_ids"]) == 3
        assert set(rec["note_ids"]) <= set(ids)
        assert rec["num_turns"] == 4                  # the prompt turn + three note turns
        delivered = sorted(os.listdir(str(run.inbox / notes.DELIVERED)))
        assert delivered == sorted(nid + ".json" for nid in rec["note_ids"])
        excess = [nid for nid in ids if nid not in rec["note_ids"]]
        assert sorted(os.listdir(str(run.inbox / notes.UNDELIVERED))) == sorted(
            nid + ".json" for nid in excess)
        by_id = {}
        for line in run.log():
            by_id.setdefault(line["id"], []).append(line)
        for nid in rec["note_ids"]:
            assert [r["state"] for r in by_id[nid]] == ["queued", "delivered"], nid
        for nid in excess:
            lines = by_id[nid]
            assert [r["state"] for r in lines] == ["queued", "undelivered"], nid
            assert lines[1]["reason"] == "cap", nid
    finally:
        run.cleanup()


# ================================================================ --defer-on-end (P2)

@POSIX_ONLY
def test_defer_on_end_logs_deferred_and_leaves_new(tmp_path):
    run = Run(tmp_path, mode="ignoreterm", gated=True, poll="100", defer=True)
    try:
        run.marker("m_prompt_echoed")                 # the driver is asleep on its 100 s poll
        time.sleep(1.0)                               # let the echo's wake be consumed and
                                                      # the main loop settle back into wait:
                                                      # the note must never be seen by a scan
        nid = run.queue("park this")
        run.p.send_signal(signal.SIGTERM)
        assert run.wait(timeout=10) == 143
        recs = [r for r in run.log() if r["id"] == nid]
        assert [r["state"] for r in recs] == ["queued", "deferred"]   # never crashed: it
        assert all("reason" not in r for r in recs)                   # was never touched
        assert sorted(os.listdir(str(run.inbox / notes.NEW))) == [nid + ".json"]
        assert os.listdir(str(run.inbox / notes.DELIVERED)) == []
        assert os.listdir(str(run.inbox / notes.UNDELIVERED)) == []
    finally:
        run.cleanup()


@POSIX_ONLY
def test_no_defer_leaves_new_unlogged(tmp_path):
    run = Run(tmp_path, mode="ignoreterm", gated=True, poll="100")
    try:
        run.marker("m_prompt_echoed")
        time.sleep(1.0)                               # as above: settle before planting
        nid = run.queue("park this")
        run.p.send_signal(signal.SIGTERM)
        assert run.wait(timeout=10) == 143
        recs = [r for r in run.log() if r["id"] == nid]
        assert [r["state"] for r in recs] == ["queued"]     # only the sender's own line:
        assert sorted(os.listdir(str(run.inbox / notes.NEW))) == [nid + ".json"]
    finally:
        run.cleanup()
