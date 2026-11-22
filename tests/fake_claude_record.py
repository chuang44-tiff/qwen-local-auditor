"""A recording stand-in for the `claude` CLI, for the launch-parity tests.

The launcher starts it in ONE of two shapes, and it answers both the way the real CLI
does -- well enough for qwen-agent.sh to finish the run with exit 0:

  argv mode   (`QWEN_STREAM=0`, no inbox)
      claude -p ... --output-format json ... -- <prompt>
      The prompt is the last argv element. The answer is one JSON result line on
      stdout, which run_claude redirects into $RAW.

  stream mode (the driver's launch, lib/stream_driver.py)
      claude -p ... --input-format stream-json --output-format stream-json --verbose
      --replay-user-messages
      The prompt arrives on stdin as a stream-json user line; it is replayed back
      with isReplay (what --replay-user-messages does, and what the driver matches
      its notes on), one `result` event follows per turn, and the session ends when
      the driver closes stdin.

Every real launch writes ONE JSON record to `$FAKE_RECORD_DIR/<n>.json`, n counting
the launches from 1 in call order:

    {"argv":             the arguments it was started with, argv[0] left out,
                         "list" of strings,
     "stdin_user_texts": the text of every stream-json user message read from stdin,
                         in order -- empty in argv mode,
     "env":              the environment it saw, as a dict}

`--help` is the launcher's capability probe (qwen-agent.sh greps its answer for
--restricted before a --test run starts): answered, NEVER recorded. That is a
deliberate narrowing of "one record per invocation" to "one record per launch of a
session": the probe starts the binary but asks no model anything, and recording it
would shift every <n> the parity tests compare (the rounds of an --until-done run go
through --test-no-cmd, which probes once per round). The suite's other doubles answer
it the same way -- tests/test_cli.py:25-31, tests/test_messaging_wiring.py:101.

The record is written as one atomic rename, so a test that finds the file always
finds it whole.

Knobs (environment):
  FAKE_RECORD_DIR        required; where the records go
  FAKE_RECORD_SESSION    the session id every result reports (default
                         fake-record-session-1) -- a resumed round must hand it back
  FAKE_RECORD_RESULT     the result text (default "record answer")
  FAKE_RECORD_FLAG_FILE  written into as soon as a launch RESUMES a session
                         (`--resume` in its argv): the flag-file check's "the coder
                         fixed it" moment, identical in both launch modes
  FAKE_RECORD_DEADLINE   seconds a stream session may sit before the double writes
                         what it has and exits (default 120; the driver closes
                         stdin long before, and a test's own timeout before that)
"""
import json
import os
import sys
import threading
import time

DEFAULT_SESSION = "fake-record-session-1"
DEFAULT_RESULT = "record answer"
DEFAULT_DEADLINE = 120.0
# What the real CLI needs to agree to be driven over stdin at all (4.3): the driver
# appends exactly these, so a launch that claims to be streamed without one of them
# is a parity bug worth a loud failure rather than a silently wrong answer.
STREAM_PAIRS = (("--input-format", "stream-json"), ("--output-format", "stream-json"))
STREAM_FLAGS = ("--verbose", "--replay-user-messages")
HELP_ANSWER = ("  --permission-mode <mode>  Permission mode to use for the session\n"
               "  --restricted              Restricted mode\n")

LOCK = threading.Lock()
SEEN = {"argv": [], "user_texts": [], "written": False}


def _exit(code):
    """Flush and leave. The deadline thread is a daemon, and os._exit keeps it from
    waking inside a half-torn-down interpreter the way sys.exit() would let it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except OSError:
            pass
    os._exit(code)


def emit(obj):
    """One event = one JSON line, flushed as written (the driver reads lines)."""
    with LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def record_dir():
    return os.environ.get("FAKE_RECORD_DIR") or ""


def write_record():
    """$FAKE_RECORD_DIR/<n>.json, once: the next free number, then an atomic rename."""
    with LOCK:
        if SEEN["written"]:
            return
        SEEN["written"] = True
        out_dir = record_dir()
        if not out_dir:
            sys.stderr.write("fake_claude_record: FAKE_RECORD_DIR is not set\n")
            _exit(3)
        n = 1
        while os.path.exists(os.path.join(out_dir, "%d.json" % n)):
            n += 1
        payload = {"argv": list(SEEN["argv"]),
                   "stdin_user_texts": list(SEEN["user_texts"]),
                   "env": {k: v for k, v in os.environ.items()}}
        tmp = os.path.join(out_dir, ".%d.json.tmp.%d" % (n, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, os.path.join(out_dir, "%d.json" % n))


def session_id():
    return os.environ.get("FAKE_RECORD_SESSION") or DEFAULT_SESSION


def result_text():
    return os.environ.get("FAKE_RECORD_RESULT") or DEFAULT_RESULT


def result_event(turn):
    """The one record line a turn ends with -- the shape run_claude parses and the
    driver merges: no error fields, no denials, a session id a resume round carries
    back, and usage the report can total."""
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
            "duration_ms": 5 + turn, "result": result_text(), "session_id": session_id(),
            "usage": {"input_tokens": 10 * turn, "output_tokens": 2 * turn},
            "permission_denials": []}


def maybe_write_flag_file():
    """The flag-file check's other half: a round the harness RESUMED was told its
    check failed, so this one fixes it -- the same in both launch modes, keyed on the
    one thing both modes carry: --resume <the session this double named>."""
    path = os.environ.get("FAKE_RECORD_FLAG_FILE")
    if not path or "--resume" not in SEEN["argv"]:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("fixed\n")
    except OSError as e:                       # a flag file the double cannot write is
        sys.stderr.write("fake_claude_record: %s\n" % e)   # the test's business, not ours


def user_message(obj):
    """The content of a stream-json user line, or None for anything else (a tool
    result block, a line that is not JSON, a line that is not an object)."""
    if not isinstance(obj, dict) or obj.get("type") != "user":
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def run_stream():
    """Answer every user line the driver writes, until it closes stdin."""
    emit({"type": "system", "subtype": "init", "session_id": session_id()})
    turn = 0
    for raw in sys.stdin.buffer:
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        content = user_message(obj)
        if content is None:
            continue
        SEEN["user_texts"].append(content)
        turn += 1
        emit({"type": "user", "message": {"role": "user", "content": content},
              "isReplay": True, "session_id": session_id()})
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "record reply turn %d" % turn}]}})
        emit(result_event(turn))
    write_record()
    _exit(0)


def run_argv():
    """Today's launch: the prompt is the last argv element, the answer is one line."""
    write_record()
    emit(result_event(1))
    _exit(0)


def deadline_watchdog():
    """Never hold a test hostage to a driver that stopped reading: at the deadline the
    double writes what it has and leaves, which is also how a real claude ends when
    its own work is done. Anything unreadable in the knob is the default."""
    try:
        seconds = float(os.environ.get("FAKE_RECORD_DEADLINE", ""))
    except ValueError:
        seconds = DEFAULT_DEADLINE
    if not seconds > 0:                       # also the NaN case: sleep(nan) would raise
        seconds = DEFAULT_DEADLINE
    time.sleep(seconds)
    write_record()
    _exit(0)


def check_stream_flags(argv):
    missing = ["%s %s" % pair for pair in STREAM_PAIRS
               if not any(argv[i] == pair[0] and i + 1 < len(argv) and argv[i + 1] == pair[1]
                          for i in range(len(argv)))]
    missing += [flag for flag in STREAM_FLAGS if flag not in argv]
    if missing:
        sys.stderr.write("fake_claude_record: asked to stream without %s\n"
                         % ", ".join(missing))
        _exit(2)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--help":           # the capability probe: answered, not recorded
        sys.stdout.write(HELP_ANSWER)
        _exit(0)
    SEEN["argv"] = list(argv)
    out_dir = record_dir()
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            sys.stderr.write("fake_claude_record: %s\n" % e)
            _exit(3)
    maybe_write_flag_file()
    if "--input-format" in argv:
        check_stream_flags(argv)
        threading.Thread(target=deadline_watchdog, daemon=True).start()
        run_stream()
    run_argv()


if __name__ == "__main__":
    main()
