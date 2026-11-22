"""Phase-1 messaging test double for the stream-json `claude` CLI.

The driver spawns it as
  python tests/fake_claude_stream.py <other flags> --input-format stream-json
  --output-format stream-json --verbose --replay-user-messages
and talks JSON lines over stdin/stdout: the user prompt line comes in, is echoed
back with isReplay, then thinking / text / tool_use / tool_result events and that
turn's result go out -- one JSON object per line, flushed as written. The next
user line opens the next turn (k = 2, 3, 4, ...).

Knobs (env vars):
  FAKE_STREAM_MODE          ok (default) | crash_before_result | crash_mid_turn2 |
                            ignoreterm | spawn_child | big_line |
                            exit126_after_output | slow_tool | eof_then_wait_stdin
                            -- unknown: stderr + exit 2
  FAKE_MARKER_DIR           directory; each step drops an atomic marker file named below
  FAKE_GATE_DIR             directory; the fake parks at the gates below until a file
                            with the gate's exact name exists (0.02 s poll, 30 s give-up
                            -> exit 4); while parked it keeps draining + echoing notes
  FAKE_PID_FILE             file; {"pid": own, "child_pid": int|null}, atomic, written
                            as the very first action (spawn_child rewrites it once the
                            child exists, so the driver can target the whole group)
  FAKE_STREAM_TOOL_SECONDS  float, default 1.5 (slow_tool's sleep inside the tool)

Markers: m_prompt_got m_prompt_echoed m_tool1 m_tool_waited m_note_echoed
m_pre_result1 m_result1 m_crash2_pre m_stdout_closed m_stdin_eof m_note<k>
m_result<k>.  Gates: g_tool g_result1.
"""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

SESSION_ID = "fake-stream-session-1"
MODES = {"ok", "crash_before_result", "crash_mid_turn2", "ignoreterm", "spawn_child",
         "big_line", "exit126_after_output", "slow_tool", "eof_then_wait_stdin"}
USAGE = ("usage: python tests/fake_claude_stream.py [other flags] --input-format "
         "stream-json --output-format stream-json --verbose --replay-user-messages")
# The spawn_child decoy: a real second process that shrugs off SIGTERM, so only a
# group-wide kill stops it.
CHILD_CODE = ("import signal,time\n"
              "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              "time.sleep(600)")
NOTES = queue.Queue()
OUT_LOCK = threading.Lock()
STATE = {"child_pid": None}


def emit(obj):
    """One event = one JSON line, flushed; a lock keeps the reader thread's echoes whole."""
    with OUT_LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _exit(code):
    """End NOW: flush and os._exit. sys.exit() would run interpreter shutdown, and the
    daemon stdin reader still blocked on read() then races the teardown: when the
    driver closes stdin a beat later, the doomed thread wakes, cannot re-enter the
    half-torn-down interpreter and abort()s -- turning our exit 1 into signal 6."""
    try:
        sys.stdout.flush()
    except OSError:  # a driver that closed the pipe must not mask our exit code
        pass
    try:
        sys.stderr.flush()
    except OSError:
        pass
    os._exit(code)


def _write_atomic(path, text):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def marker(name):
    path = os.environ.get("FAKE_MARKER_DIR")
    if path:
        _write_atomic(os.path.join(path, name), name)


def write_pid_file():
    path = os.environ.get("FAKE_PID_FILE")
    if path:
        _write_atomic(path, json.dumps({"pid": os.getpid(),
                                        "child_pid": STATE["child_pid"]}))


def read_stdin():
    # The one stdin reader: user notes land on NOTES as content strings; anything else
    # (unparseable, other shapes) is ignored; EOF puts the None sentinel.
    for raw in sys.stdin.buffer:
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if (isinstance(obj, dict) and obj.get("type") == "user"
                and isinstance(obj.get("message"), dict)
                and obj["message"].get("role") == "user"
                and isinstance(obj["message"].get("content"), str)):
            record_line(obj)
            NOTES.put(obj["message"]["content"])
    NOTES.put(None)


def record_line(obj):
    """FAKE_LINES_FILE: the test's witness to exactly what the driver forwarded -- the
    whole parsed object of each user line, one JSON line per line received."""
    path = os.environ.get("FAKE_LINES_FILE")
    if not path:
        return
    with OUT_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")
            fh.flush()


def drain_notes():
    """Echo every queued note (isReplay) + m_note_echoed; used by slow_tool and gates.

    The EOF sentinel is put back so the turn loop still sees it.
    """
    while True:
        try:
            note = NOTES.get_nowait()
        except queue.Empty:
            return
        if note is None:
            NOTES.put(None)
            return
        emit(echo(note))
        marker("m_note_echoed")


def wait_gate(name):
    """Park until FAKE_GATE_DIR/name exists, draining notes each poll; 30 s -> exit 4."""
    gate_dir = os.environ.get("FAKE_GATE_DIR")
    if not gate_dir:
        return
    path = os.path.join(gate_dir, name)
    deadline = time.time() + 30.0
    while not os.path.exists(path):
        drain_notes()
        if time.time() > deadline:
            _exit(4)
        time.sleep(0.02)


def block_forever():
    # ignoreterm: the driver must escalate past the ignored SIGTERM to a group kill.
    while True:
        time.sleep(0.1)


def init_event():
    return {"type": "system", "subtype": "init", "session_id": SESSION_ID}


def echo(content):
    return {"type": "user", "message": {"role": "user", "content": content},
            "isReplay": True, "session_id": SESSION_ID}


def thinking(n):
    return {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "ZZTHINKZZ turn %d" % n}]}}


def text(n):
    return {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "ZZASSISTZZ turn %d reply" % n}]}}


def tool_use(n):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-%d" % n, "name": "Bash",
         "input": {"command": "true"}}]}}


def tool_result(n):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu-%d" % n, "content": "done"}]}}  # no isReplay


def result(k):
    # total_cost_usd is cumulative (0.01, 0.02, ...); usage/duration are per turn.
    return {"type": "result", "subtype": "success", "is_error": False,
            "num_turns": 1, "duration_ms": 40 + k, "result": "answer turn %d" % k,
            "session_id": SESSION_ID,
            "usage": {"input_tokens": 100 * k, "output_tokens": 10 * k,
                      "cache_read_input_tokens": 5,
                      "cache_creation": {"ephemeral_5m_input_tokens": 7},
                      "server_tool_use": {"web_search_requests": 0}},
            "total_cost_usd": round(0.01 * k, 2),
            "permission_denials": [{"tool_name": "Bash", "tool_use_id": "tu-1"},
                                   {"tool_name": "Read", "turn": k}]}


def check_flags(argv):
    if "--help" in argv:
        print(USAGE)
        _exit(0)
    if len(argv) >= 2 and argv[-2] == "--":
        argv = argv[:-2]  # a trailing "-- <prompt>" pair is none of our business
    missing = []
    for flag, value in (("--input-format", "stream-json"),
                        ("--output-format", "stream-json")):
        if not any(argv[i] == flag and i + 1 < len(argv) and argv[i + 1] == value
                   for i in range(len(argv))):
            missing.append("%s %s" % (flag, value))
    for flag in ("--verbose", "--replay-user-messages"):
        if flag not in argv:
            missing.append(flag)
    if missing:
        sys.stderr.write("fake_claude_stream: missing required flag(s): %s\n"
                         % ", ".join(missing))
        _exit(2)


def main():
    write_pid_file()  # the very first action, before stdin or a single event
    argv = sys.argv[1:]
    check_flags(argv)
    mode = os.environ.get("FAKE_STREAM_MODE", "ok")
    if mode not in MODES:
        sys.stderr.write("fake_claude_stream: unknown FAKE_STREAM_MODE %r\n" % mode)
        _exit(2)
    tool_seconds = float(os.environ.get("FAKE_STREAM_TOOL_SECONDS", "1.5"))
    threading.Thread(target=read_stdin, daemon=True).start()

    emit(init_event())
    if mode == "ignoreterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "spawn_child":
        child = subprocess.Popen([sys.executable, "-c", CHILD_CODE])
        STATE["child_pid"] = child.pid
        write_pid_file()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    prompt = NOTES.get()
    if prompt is None:
        _exit(0)  # EOF before the prompt: nothing was asked, nothing to say
    marker("m_prompt_got")
    emit(echo(prompt))
    marker("m_prompt_echoed")

    if mode == "exit126_after_output":
        _exit(126)  # init + echo are already on the wire
    if mode == "crash_before_result":
        emit(thinking(1))
        emit(text(1))
        _exit(1)  # no result, ever

    if mode == "eof_then_wait_stdin":
        # The Windows hang reproduced on POSIX: stdout says the turn is over, the
        # process says it exits only once its stdin closes -- the two wait on each
        # other unless the driver treats stdout EOF as the end of the run.
        emit(thinking(1))
        emit(text(1))
        sys.stdout.flush()
        os.close(1)                     # the driver's reader sees a real stdout EOF...
        try:
            sys.stdout.close()          # ...and no half-flushed wrapper ever writes fd 1
        except OSError:
            pass
        marker("m_stdout_closed")
        while NOTES.get() is not None:  # the daemon reader's EOF sentinel ends the wait:
            pass                        # the driver closed our stdin (items before it: notes)
        marker("m_stdin_eof")           # the test's witness that stdin really closed
        os._exit(1)                     # and only now does the "claude" finish exiting

    if mode == "big_line":
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Z" * 5242880}]}})  # one ~5 MiB line
    emit(thinking(1))
    emit(text(1))

    if mode == "ignoreterm":
        block_forever()  # never a result, never an exit: force-kill is the only way out

    emit(tool_use(1))
    marker("m_tool1")
    if mode == "slow_tool":
        time.sleep(tool_seconds)
        drain_notes()
    elif os.environ.get("FAKE_GATE_DIR"):
        marker("m_tool_waited")
        wait_gate("g_tool")
    emit(tool_result(1))

    marker("m_pre_result1")
    wait_gate("g_result1")
    emit(result(1))
    marker("m_result1")

    k = 2
    while True:
        note = NOTES.get()
        if note is None:
            if mode == "ignoreterm":
                block_forever()
            _exit(0)  # a closed stdin ends a well-behaved session
        if mode == "crash_mid_turn2" and k == 2:
            emit(init_event())
            marker("m_crash2_pre")
            _exit(1)  # the note is never echoed: the turn dies mid-air
        emit(init_event())  # one init per turn
        emit(echo(note))
        marker("m_note%d" % k)
        emit(thinking(k))
        emit(text(k))
        emit(tool_use(k))
        emit(tool_result(k))
        emit(result(k))
        marker("m_result%d" % k)
        k += 1


if __name__ == "__main__":
    main()
