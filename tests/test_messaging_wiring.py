"""Phase 1 wiring: the launchers open inboxes, stream through the driver, and take
notes -- tested end to end with the real qwen-agent.sh, the real stream driver, the
real supervisor and the real Swarm, over tests/fake_claude_stream.py.

Stage A (tests/test_notes.py, tests/test_stream_driver.py) proved the modules. This
file proves the WIRING the spec's rev-3/rev-4 contracts demand:

  * K1/C4/P5: streaming happens only with QWEN_STREAM on AND an inbox set; otherwise
    claude gets today's argv, and QWEN_STREAM=0 creates no inbox anywhere.
  * K2: <watch_dir>/<id>.inbox/ is made by the registration before `watch: <url>`
    prints; cleanup() sweeps ONLY an inbox the run owns (INBOX_OWN).
  * 4.2: QWEN_INBOX/QWEN_INBOX_DEFER are SCRUB_LIST members -- claude never sees them.
  * 4.4/K9/P8: --send/--wait/--notes end to end against a live run, with their exit
    codes; record_scripts() runs without an inbox (K2); --dry-run prints the
    `# stream:` placeholder line (K9).
  * K5/P2 (until-done): a note missed by round 1 is `deferred`, delivered exactly
    once into round 2's prompt (`delivered: next_round`), and notes still pending at
    loop end are swept `undelivered: loop_ended` and listed under `## Steering
    notes`. QWEN_INBOX_DEFER=1 goes only when rounds < max_rounds.
  * K2/P3/K3/P6 (swarm): RUN/inbox/<unit>/ before the unit_start event with a stale
    `.closed` removed; QWEN_INBOX for the first call and retries, never for REPAIR;
    a steered attempt's answer is not cached; run_unit's finally closes the inbox.
  * P3: the supervisor opens RUN/inbox/ after makedirs(run_dir) and before the
    watch registration. P6: call_agent copies stream/notes_delivered/note_ids into
    round-N.json.

The until-done and swarm rounds run a scripted stand-in for qwen-agent itself (like
tests/fake_agent.py stands in for it in test_supervisor.py): it witnesses the
QWEN_INBOX/QWEN_INBOX_DEFER its launcher handed it and stands in for the driver's
exit-time deferral log, while the single-shot and review-round tests run the REAL
launcher with the real driver over tests/fake_claude_stream.py.
"""
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

import pytest

from lib import notes, supervisor, swarm as sw, watch
from lib.swarm_engine import events
from test_cli import AGENT, BASH, base_url, posix, run, server  # noqa: F401 (fixture)
from test_cli_deep import calls as scen_calls                    # noqa: E402
import test_scenarios                                            # noqa: E402
from test_replay import renv, script, recorder, toolchain        # noqa: E402,F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "local-auditor"
FAKE_STREAM = ROOT / "tests" / "fake_claude_stream.py"
NOTE_ID_RE = re.compile(r"^\d+-[0-9a-f]{6}$")
STREAM_TAIL = ["--verbose", "--replay-user-messages"]            # the driver's last two

# The --record test drives test_replay's offline UI-suite machinery; its fixtures
# travel by import the way test_replay itself takes the server fixture.
fake_scen = test_scenarios.fake_scen                             # noqa: E402


def _base_env(tmp_path, extra=None):
    """A launcher child env built from scratch (test_cli.run's hygiene), so nothing
    of the developer's QWEN_*/CLAUDE_*/ANTHROPIC_* settings reaches a test run."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
    env["QWEN_CONFIG"] = posix(tmp_path / "no-such-config")
    env["QWEN_NO_NOTICE"] = "1"
    env["QWEN_DEPTH"] = "shallow"
    env["QWEN_WATCH"] = "0"
    env["QWEN_WATCH_NOSERVE"] = "1"
    env["QA_SUPERVISED"] = "0"
    env.update(extra or {})
    return env


def cli(tmp_path, env, *args, timeout=60):
    """qwen-agent's own sender CLI, run as the main session would run it."""
    return subprocess.run([BASH, posix(AGENT), *[str(a) for a in args]], env=env,
                          capture_output=True, encoding="utf-8", errors="replace",
                          cwd=str(tmp_path), timeout=timeout)


def wait_for(pred, timeout=30, message="condition never became true"):
    deadline = time.time() + timeout
    while not pred():
        if time.time() > deadline:
            raise AssertionError("%s (after %.0f s)" % (message, timeout))
        time.sleep(0.05)


# The fake claude a test points QWEN_CLAUDE_BIN at: it witnesses every call (argv
# lines + full environment) into $WITNESS_DIR/call.<n> (a test that reads witnesses
# exports the dir), then becomes the stream fake for a DRIVEN call (the driver's
# stream flags are in its argv) or answers the plain old `claude -p` shape with one
# result line for an undriven call (QWEN_STREAM=0, --record, no inbox) -- exactly
# the K1 fork the launcher is supposed to take. $WRAP_RECORDER swaps in the
# --record fake for every call. The `--help` answer is the capability probe.
WRAP = r'''#!/usr/bin/env bash
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
if [ -n "${WITNESS_DIR:-}" ]; then
  mkdir -p "$WITNESS_DIR"
  n=0
  [ -f "$WITNESS_DIR/.n" ] && n="$(cat "$WITNESS_DIR/.n")"
  n=$((n + 1))
  printf '%s' "$n" > "$WITNESS_DIR/.n"
  { for a in "$@"; do printf 'ARG:%s\n' "$a"; done
    env | sed -n 's/^/ENV:/p'
  } > "$WITNESS_DIR/call.$n"
fi
if [ -n "${WRAP_RECORDER:-}" ]; then
  exec bash "$WRAP_RECORDER" "$@"
fi
for a in "$@"; do
  if [ "$a" = stream-json ]; then
    exec "${FAKE_PY:-python3}" "${FAKE_STREAM_PATH}" "$@"
  fi
done
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":5,"result":"plain answer","session_id":"fake-session-1","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}'
'''


def witness_fake(tmp_path, name="fake-claude"):
    p = tmp_path / name
    p.write_text(WRAP, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    witness = tmp_path / "witness"
    witness.mkdir(exist_ok=True)
    return p, witness


def wcall(witness, n):
    """(argv, env) of the nth claude call, from the wrapper's dump. Wait for the
    dump to be COMPLETE, not merely to exist: the wrapper opens the file, writes
    the ARG lines, then the env, and a reader that caught it mid-append would see
    a truncated argv."""
    path = witness / ("call.%d" % n)
    wait_for(lambda: path.exists() and "ENV:PATH=" in
             path.read_text(encoding="utf-8", errors="replace"),
             message="no complete witness call.%d" % n)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    argv = [ln[4:] for ln in lines if ln.startswith("ARG:")]
    env = dict(ln[4:].split("=", 1) for ln in lines
               if ln.startswith("ENV:") and "=" in ln)
    return argv, env


def stream_env(markers, gates, lines_file, mode="ok", poll="0.2", gated=True):
    """The stream fake's env knobs (it reads them straight from its environment,
    which the launcher hands down through the driver)."""
    env = {"FAKE_PY": sys.executable, "FAKE_STREAM_PATH": posix(FAKE_STREAM),
           "FAKE_STREAM_MODE": mode, "FAKE_MARKER_DIR": posix(markers),
           "FAKE_LINES_FILE": posix(lines_file), "QWEN_NOTE_POLL": poll}
    if gated:
        env["FAKE_GATE_DIR"] = posix(gates)
    return env


def fake_dirs(tmp_path, tag=""):
    markers = tmp_path / ("markers%s" % tag)
    gates = tmp_path / ("gates%s" % tag)
    markers.mkdir(exist_ok=True)
    gates.mkdir(exist_ok=True)
    return markers, gates, tmp_path / ("lines%s.jsonl" % tag)


def watch_cache(tmp_path):
    return tmp_path / "cache"


def pointer_dir(tmp_path):
    return watch_cache(tmp_path) / "qwen-agent" / "watch"


def wait_pointer(tmp_path, timeout=60):
    """The watch id a registered run leaves behind, once its pointer exists."""
    d = pointer_dir(tmp_path)
    box = {}

    def pred():
        if not d.is_dir():
            return False
        for p in d.glob("*.json"):
            try:
                box["v"] = (p.stem, json.loads(p.read_text(encoding="utf-8")))
                return True
            except (OSError, ValueError):
                continue
        return False
    wait_for(pred, timeout=timeout, message="no watch pointer appeared")
    return box["v"]


def inbox_dir_for(tmp_path, rid):
    return pointer_dir(tmp_path) / ("%s.inbox" % rid)


def received_lines(path):
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if raw:
            try:
                out.append(json.loads(raw))
            except ValueError:
                pass        # a line caught mid-append: the next wait_for poll
    return out              # sees it whole (the fake flushes each line as written)


def log_states(inbox):
    by_id = {}
    for rec in notes.read_log(str(inbox)):
        by_id.setdefault(rec.get("id"), []).append(rec)
    return by_id


def _watchdog(steps, errors):
    """Run [(name, fn)] in order on a helper thread (the main thread drives the
    supervisor/swarm; these wait on the rounds' witnesses and drive the test side)."""

    def body():
        try:
            for _name, fn in steps:
                fn()
        except BaseException as e:                        # noqa: BLE001 -- raised below
            errors.append((e.__class__.__name__, str(e)))
    th = threading.Thread(target=body, daemon=True)
    th.start()
    return th


# ============================================================ the streamed launch (K1/K8/P8)

def test_stream_on_uses_the_driver_and_writes_one_record(tmp_path, server):  # noqa: F811
    fake, witness = witness_fake(tmp_path)
    markers, gates, lines = fake_dirs(tmp_path)
    extra = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
             "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
             "QWEN_BASE_URL": base_url(server), "QWEN_CLAUDE_BIN": posix(fake)}
    extra.update(stream_env(markers, gates, lines, gated=False))
    extra["WITNESS_DIR"] = posix(witness)
    r = run(tmp_path, ["--warn-denials", "--json", "fix the bug"], None, None,
            extra=extra)
    assert r.returncode == 0, r.stdout + r.stderr
    rid, pointer = wait_pointer(tmp_path)
    assert pointer["kind"] == "agent"
    inbox = inbox_dir_for(tmp_path, rid)
    assert (inbox / "new").is_dir() and (inbox / "delivered").is_dir()
    assert (inbox / "undelivered").is_dir()
    assert (inbox / ".closed").exists()              # cleanup()'s owned sweep ran (K2)
    argv, _env = wcall(witness, 1)
    # the driver's claude argv (4.3): the stream flags are in, --output-format json
    # is OUT, and the trailing `-- PROMPT` is the driver's to write -- so claude
    # never ends on it (the prompt went on stdin instead, witnessed below)
    assert argv[-2:] == STREAM_TAIL
    assert "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    for i, a in enumerate(argv):
        if a == "--output-format":
            assert argv[i + 1] == "stream-json"      # only the driver's own remains
    assert "--" not in argv
    got = received_lines(lines)
    assert [g["message"]["content"] for g in got] == ["fix the bug"]
    rec = json.loads(r.stdout)
    assert rec["stream"] is True
    assert rec["notes_delivered"] == 0 and rec["note_ids"] == []
    assert rec["result"] == "answer turn 1"


def test_stream_on_dry_run_prints_the_stream_line(tmp_path):
    fake, _witness = witness_fake(tmp_path)
    base = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
            "XDG_CACHE_HOME": posix(watch_cache(tmp_path))}
    # a run that would register prints the placeholder, as --browser prints its
    # folder, and the line stands ABOVE the argv (K9/P8, 4.3)
    r = run(tmp_path, ["--dry-run", "hi"], None, fake, extra=base)
    assert r.returncode == 0, r.stderr
    assert "# stream: lib/stream_driver.py --inbox <watch_dir>/<id>.inbox" in r.stdout
    assert r.stdout.index("# stream:") < r.stdout.index("# argv")
    # QWEN_INBOX set (a parent's): the path prints as it stands
    other = tmp_path / "parent-inbox"
    notes.init_inbox(str(other))
    r = run(tmp_path, ["--dry-run", "hi"], None, fake,
            extra=dict(base, QWEN_INBOX=posix(other)))
    assert "--inbox %s" % posix(other) in r.stdout
    # the deferral marker rides the line
    r = run(tmp_path, ["--dry-run", "hi"], None, fake,
            extra=dict(base, QWEN_INBOX=posix(other), QWEN_INBOX_DEFER="1"))
    assert "--defer-on-end" in r.stdout
    # QWEN_STREAM=0, --interactive and a not-registering run print no such line (K9)
    assert "# stream:" not in run(tmp_path, ["--dry-run", "hi"], None, fake,
                                  extra=dict(base, QWEN_STREAM="0")).stdout
    assert "# stream:" not in run(tmp_path, ["--dry-run", "hi"], None, fake,
                                  extra=dict(base, QWEN_WATCH="0")).stdout
    assert "# stream:" not in run(tmp_path, ["--dry-run", "--interactive"], None,
                                  fake, extra=base).stdout


def test_qwen_inbox_scrubbed_from_claude_env(tmp_path, server):   # noqa: F811
    # the launcher's own inbox (watch-registered): SCRUB_LIST keeps QWEN_INBOX and
    # QWEN_INBOX_DEFER away from claude, its tools and its subagents (4.2)
    fake, witness = witness_fake(tmp_path)
    markers, gates, lines = fake_dirs(tmp_path)
    extra = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
             "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
             "QWEN_BASE_URL": base_url(server), "QWEN_CLAUDE_BIN": posix(fake)}
    extra.update(stream_env(markers, gates, lines, gated=False))
    extra["WITNESS_DIR"] = posix(witness)
    r = run(tmp_path, ["--warn-denials", "hi"], None, None,
            extra=dict(extra, QWEN_INBOX_DEFER="1"))      # the defer marker too
    assert r.returncode == 0, r.stdout + r.stderr
    _argv, env = wcall(witness, 1)
    assert "QWEN_INBOX" not in env and "QWEN_INBOX_DEFER" not in env
    # and an inbox a PARENT handed down: scrubbed from claude too -- and this run
    # does not own it: no .closed, nothing swept (K2)
    parent_inbox = tmp_path / "parent" / "inbox"
    notes.init_inbox(str(parent_inbox))
    nid = notes.queue_note(str(parent_inbox), "a1b2c3d4e5f6", "mid-run steer")
    fake2 = witness_fake(tmp_path, "fake-claude2")[0]
    extra2 = dict(extra, QWEN_WATCH="0", QWEN_CLAUDE_BIN=posix(fake2),
                  QWEN_INBOX=posix(parent_inbox), QWEN_INBOX_DEFER="1")
    r = run(tmp_path, ["--warn-denials", "hi"], None, None, extra=extra2)
    assert r.returncode == 0, r.stdout + r.stderr
    _argv2, env2 = wcall(witness, 2)                      # the second run's call
    assert "QWEN_INBOX" not in env2 and "QWEN_INBOX_DEFER" not in env2
    got = received_lines(lines)
    assert any("mid-run steer" in g["message"]["content"] for g in got)
    assert not (parent_inbox / ".closed").exists()        # not ours: never closed (K2)
    assert list((parent_inbox / "new").iterdir()) == []   # delivered instead
    assert (parent_inbox / "delivered" / ("%s.json" % nid)).exists()


def test_stream_off_keeps_todays_argv_and_makes_no_inbox(tmp_path, server):   # noqa: F811
    fake, witness = witness_fake(tmp_path)
    markers, gates, lines = fake_dirs(tmp_path)
    extra = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
             "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
             "QWEN_BASE_URL": base_url(server), "QWEN_CLAUDE_BIN": posix(fake)}
    extra.update(stream_env(markers, gates, lines, gated=False))
    extra["WITNESS_DIR"] = posix(witness)
    r = run(tmp_path, ["--warn-denials", "say hi"], None, None,
            extra=dict(extra, QWEN_STREAM="0"))
    assert r.returncode == 0, r.stdout + r.stderr
    argv, _env = wcall(witness, 1)
    # today's argv exactly (K1/C4): -p first (the witness dumps "$@", argv[0] is the
    # flag not the bin), --output-format json, the trailing `-- PROMPT`, and none of
    # the driver's stream flags anywhere
    assert argv[0] == "-p"
    assert argv[-2:] == ["--", "say hi"]
    i = argv.index("--output-format")
    assert argv[i + 1] == "json"
    assert "--input-format" not in argv and "--replay-user-messages" not in argv
    assert "--verbose" not in argv
    assert received_lines(lines) == []                    # no driver: stdin never opened
    rid, _pointer = wait_pointer(tmp_path)
    assert not inbox_dir_for(tmp_path, rid).exists()      # P5: no inbox anywhere


# ================================================== the sender CLI, live (4.4/K9/P8)

def test_single_shot_send_wait_notes_end_to_end(tmp_path, server):   # noqa: F811
    fake, _witness = witness_fake(tmp_path)
    markers, gates, lines = fake_dirs(tmp_path)
    extra = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
             "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
             "QWEN_BASE_URL": base_url(server), "QWEN_CLAUDE_BIN": posix(fake)}
    extra.update(stream_env(markers, gates, lines, gated=True))
    env = _base_env(tmp_path, extra)
    proc = subprocess.Popen([BASH, posix(AGENT), "--warn-denials", "--json",
                             "fix the thing"], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8",
                            errors="replace", cwd=str(tmp_path))
    try:
        rid, pointer = wait_pointer(tmp_path)
        assert pointer["kind"] == "agent"
        inbox = inbox_dir_for(tmp_path, rid)
        wait_for(lambda: (markers / "m_tool_waited").exists(), timeout=60,
                 message="the fake never parked inside its tool call")

        # --send: exit 0, prints the id, the note file lands in new/ at once
        r = cli(tmp_path, env, "--send", rid, "use the utf8 reader")
        assert r.returncode == 0, r.stderr
        nid1 = r.stdout.strip()
        assert NOTE_ID_RE.fullmatch(nid1), repr(nid1)
        assert (inbox / "new" / ("%s.json" % nid1)).exists()

        # --notes: the queued row, with the preview (K9)
        r = cli(tmp_path, env, "--notes", rid)
        assert r.returncode == 0, r.stderr
        assert nid1 in r.stdout and "queued" in r.stdout
        assert "use the utf8 reader" in r.stdout

        # release the tool: the driver forwards, the parked fake echoes -> delivered
        (gates / "g_tool").touch()
        wait_for(lambda: (markers / "m_note_echoed").exists(), timeout=60,
                 message="the note was never echoed")
        wait_for(lambda: any(x.get("id") == nid1 and x["state"] == "delivered"
                             for x in notes.read_log(str(inbox))), timeout=30,
                 message="no delivered log line")

        # --send --wait: the outcome line arrives while the fake parks at result 1
        r = cli(tmp_path, env, "--send", rid, "--wait", "30", "and mind the tail")
        assert r.returncode == 0, r.stdout + r.stderr
        out_lines = r.stdout.splitlines()
        nid2 = out_lines[0]
        assert NOTE_ID_RE.fullmatch(nid2) and out_lines[1].startswith("delivered at ")

        (gates / "g_result1").touch()
        out, err = proc.communicate(timeout=90)
        assert proc.returncode == 0, out + err
    finally:
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
    rec = json.loads(out)
    assert rec["stream"] is True
    assert rec["notes_delivered"] == 2 and set(rec["note_ids"]) == {nid1, nid2}
    contents = [g["message"]["content"] for g in received_lines(lines)]
    assert contents[0] == "fix the thing"
    assert any("use the utf8 reader" in c for c in contents)
    assert any("mind the tail" in c for c in contents)
    states = log_states(inbox)
    for nid in (nid1, nid2):
        assert (inbox / "delivered" / ("%s.json" % nid)).exists()
        assert [x["state"] for x in states[nid]] == ["queued", "delivered"]
    assert list((inbox / "new").iterdir()) == []
    assert (inbox / ".closed").exists()               # cleanup() swept what was ours
    # after the end, the target is simply not there to receive anything (4.1)
    assert cli(tmp_path, env, "--send", rid, "late").returncode == 3


def _spawn_dead_pid():
    """A pid that is provably dead: spawn, reap, return its number."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    p.kill()
    p.wait(timeout=10)
    return p.pid


def test_cli_send_and_notes_exit_codes(tmp_path):
    rid = "f00dbeef1234"
    env = _base_env(tmp_path, {"XDG_CACHE_HOME": posix(watch_cache(tmp_path))})
    wd = pointer_dir(tmp_path)
    wd.mkdir(parents=True, exist_ok=True)

    def write_pointer(**over):
        p = {"kind": "agent", "id": rid, "title": "t", "pid": os.getpid(),
             "start": round(time.time(), 3)}
        p.update(over)
        (wd / ("%s.json" % rid)).write_text(json.dumps(p), encoding="utf-8")

    # grammar (K9): malformed addresses are usage (2) -- not-hex, wrong case
    assert cli(tmp_path, env, "--send", "not-an-id", "text").returncode == 2
    assert cli(tmp_path, env, "--send", "ABCDEF123456", "text").returncode == 2
    assert cli(tmp_path, env, "--notes").returncode == 2
    # no such run (3): the pointer is the address book
    assert cli(tmp_path, env, "--send", rid, "text").returncode == 3
    assert cli(tmp_path, env, "--notes", rid).returncode == 3
    # a registered, live, open target: 0 and the id on stdout
    write_pointer()
    inbox = wd / ("%s.inbox" % rid)
    notes.init_inbox(str(inbox))
    r = cli(tmp_path, env, "--send", rid, "hello there")
    assert r.returncode == 0 and NOTE_ID_RE.fullmatch(r.stdout.strip()), r.stdout
    nid = r.stdout.strip()
    # text rules (4.2): empty-after-strip and >8000 chars are usage (2); -f reads a
    # file; -- lets the text start with a dash; two positionals are usage (P8)
    assert cli(tmp_path, env, "--send", rid, "   ").returncode == 2
    assert cli(tmp_path, env, "--send", rid, "x" * 8001).returncode == 2
    nf = tmp_path / "note.txt"
    nf.write_text("from a file", encoding="utf-8")
    assert cli(tmp_path, env, "--send", rid, "-f", posix(nf)).returncode == 0
    assert cli(tmp_path, env, "--send", rid, "--", "-dash-first").returncode == 0
    assert cli(tmp_path, env, "--send", rid, "a", "b").returncode == 2
    # --wait's SECONDS bound (K9)
    assert cli(tmp_path, env, "--send", rid, "--wait", "0", "t").returncode == 2
    assert cli(tmp_path, env, "--send", rid, "--wait", "3601", "t").returncode == 2
    assert cli(tmp_path, env, "--send", rid, "--wait", "x", "t").returncode == 2
    # a /unit on a run with no units: usage (2)
    assert cli(tmp_path, env, "--send", "%s/alpha" % rid, "t").returncode == 2
    # --notes rows: latest state per id, preview from the note file (K9)
    r = cli(tmp_path, env, "--notes", rid)
    assert r.returncode == 0
    assert nid in r.stdout and "queued" in r.stdout and "hello there" in r.stdout
    # the run's pid dead: 3 (4.1)
    write_pointer(pid=_spawn_dead_pid())
    assert cli(tmp_path, env, "--send", rid, "t").returncode == 3
    # a finished run (pointer end set) is not running: --send 3, --notes still 0 (4.1)
    write_pointer(end=round(time.time(), 3))
    assert cli(tmp_path, env, "--send", rid, "t").returncode == 3
    assert cli(tmp_path, env, "--notes", rid).returncode == 0
    # a closed inbox refuses notes (4.1/4.2): 3
    write_pointer()                                          # live again
    notes.close_inbox(str(inbox), "run_ended")
    assert cli(tmp_path, env, "--send", rid, "late").returncode == 3


def test_cli_wait_reports_pid_dead_and_closed(tmp_path):
    # --wait's four outcomes (K9): the target pid dies -> `undelivered: pid_dead` 11;
    # a sweep landing while waiting is reported with its own reason -> 11; nothing
    # ever reads the note -> `still queued` 10; the owner's exit logs it deferred
    # while we wait -> `deferred to the next round` 12.
    rid = "f00dbeef9876"
    env = _base_env(tmp_path, {"XDG_CACHE_HOME": posix(watch_cache(tmp_path))})
    wd = pointer_dir(tmp_path)
    wd.mkdir(parents=True, exist_ok=True)
    inbox = wd / ("%s.inbox" % rid)
    notes.init_inbox(str(inbox))
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (wd / ("%s.json" % rid)).write_text(
        json.dumps({"kind": "agent", "id": rid, "title": "t", "pid": victim.pid,
                    "start": round(time.time(), 3)}), encoding="utf-8")
    def _kill_and_reap():
        # reap it in the thread too: a killed-but-unwaited CHILD stays a zombie and
        # keeps pid_alive true -- --wait must see the pid gone, not still "alive"
        time.sleep(1.0)
        victim.kill()
        with contextlib.suppress(Exception):
            victim.wait(timeout=5)
    killer = threading.Thread(target=_kill_and_reap)
    killer.start()
    r = cli(tmp_path, env, "--send", rid, "--wait", "30", "will crash with the pid")
    killer.join(timeout=10)
    assert r.returncode == 11, r.stdout + r.stderr
    assert r.stdout.splitlines()[1] == "undelivered: pid_dead"
    (wd / ("%s.json" % rid)).write_text(
        json.dumps({"kind": "agent", "id": rid, "title": "t", "pid": os.getpid(),
                    "start": round(time.time(), 3)}), encoding="utf-8")
    sweeper = threading.Thread(target=lambda: (time.sleep(1.0),
                                               notes.close_inbox(str(inbox), "run_ended")))
    sweeper.start()
    r = cli(tmp_path, env, "--send", rid, "--wait", "30", "swept while waiting")
    sweeper.join(timeout=10)
    assert r.returncode == 11, r.stdout + r.stderr
    assert r.stdout.splitlines()[1] == "undelivered: run_ended"
    # nothing drains this inbox (no driver behind it): --wait runs out -> 10.
    notes.init_inbox(str(inbox))                       # reopen: clears the .closed
    r = cli(tmp_path, env, "--send", rid, "--wait", "2", "nobody reads me")
    assert r.returncode == 10, r.stdout + r.stderr
    assert r.stdout.splitlines()[1] == "still queued"
    # the owner's exit logging the note deferred while we wait: --wait -> 12. The note
    # file alone tells us when --send has queued it; 0.3 s later its `queued` log line
    # has landed, so the `deferred` line we add is genuinely the LATEST state.
    for f in (inbox / "new").glob("*.json"):
        f.unlink()                                     # only the new note in new/

    def _defer_once():
        deadline = time.time() + 20
        while time.time() < deadline:
            files = sorted((inbox / "new").glob("*.json"))
            if files:
                time.sleep(0.3)
                notes.log_note(str(inbox), files[0].stem, rid, "main", "deferred")
                return
            time.sleep(0.05)
    waiter = threading.Thread(target=_defer_once)
    waiter.start()
    r = cli(tmp_path, env, "--send", rid, "--wait", "30", "defer me at the round end")
    waiter.join(timeout=25)
    assert r.returncode == 12, r.stdout + r.stderr
    assert r.stdout.splitlines()[1] == "deferred to the next round"


# ==================================================== until-done (4.5/K5/P2/P6)

FAKE_UD_AGENT = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["QWEN_SKILL_DIR"])
from lib import notes

d = os.environ["UD_DIR"]
os.makedirs(os.path.join(d, "calls"), exist_ok=True)
argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


n = 1
while os.path.exists(os.path.join(d, "calls", "started.%d.json" % n)):
    n += 1
rec = {"n": n, "role": opt("-r"), "resume": opt("--resume"),
       "inbox": os.environ.get("QWEN_INBOX"),
       "defer": os.environ.get("QWEN_INBOX_DEFER")}
with open(os.path.join(d, "calls", "started.%d.json" % n), "w") as fh:
    json.dump(rec, fh)
gate = os.path.join(d, "gate.%d" % n)
t0 = time.time()
while not os.path.exists(gate) and time.time() - t0 < 90:
    time.sleep(0.05)
# What a --defer-on-end driver does at its exit (K4): untouched new/ notes are
# logged deferred and LEFT -- this fake stands in for the driver's finalize.
if rec["inbox"] and rec["defer"] == "1":
    for path, nid in notes.list_new(rec["inbox"]):
        to, ln = notes.note_meta(path)
        notes.log_note(rec["inbox"], nid, to, "main", "deferred", text_len=ln)
if n == 2:                                    # round 2 is the round that fixes it
    with open(os.path.join(os.environ["UD_REPO"], "good"), "w"):
        pass
# The audit role answers the CONTRADICTION protocol (supervisor.py:276-302): the
# clean verdict keeps the loop's done-exit; an unusable reply would downgrade the
# run to EXIT_PARTIAL ("review the diff manually"), which this test is not about.
ans = "NO CONTRADICTIONS" if rec["role"] == "auditor" else "worked on it"
out = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
       "duration_ms": 5, "result": ans, "session_id": "sess-%d" % n,
       "usage": {"input_tokens": 10, "output_tokens": 2}, "permission_denials": []}
if n == 1 and rec["inbox"]:   # ONLY a streamed round carries the additive trio (P6:
    out["stream"] = True      # round-N.json keeps what the record held); an unstreamed
    out["notes_delivered"] = 0  # round's record is a plain claude result, trio-free
    out["note_ids"] = []
print(json.dumps(out))
'''


def _ud_setup(tmp_path, monkeypatch,
              extra_flags=("--no-deviation-audit",)):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo)] + args, check=True,
                       capture_output=True)
    (repo / "check.py").write_text(
        "import os, sys\nsys.exit(0 if os.path.exists('good') else 1)\n",
        encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True,
                   capture_output=True)
    task = tmp_path / "task.md"
    task.write_text("- [ ] value is good -- check: cmd %s check.py\n"
                    % posix(sys.executable), encoding="utf-8")
    state = tmp_path / "state"
    ud = tmp_path / "ud"
    ud.mkdir()
    monkeypatch.setenv("QWEN_AGENT_STATE", posix(state))
    monkeypatch.setenv("QWEN_SUPERVISOR_BACKOFF", "0")
    monkeypatch.setenv("QWEN_SKILL_DIR", posix(SKILL))
    monkeypatch.setenv("UD_DIR", posix(ud))
    monkeypatch.setenv("UD_REPO", posix(repo))
    monkeypatch.delenv("QWEN_INBOX", raising=False)
    monkeypatch.delenv("QWEN_INBOX_DEFER", raising=False)
    fake = tmp_path / "fake-ud-agent.py"
    fake.write_text(FAKE_UD_AGENT, encoding="utf-8")
    argv = ["--task", str(task), "--repo", str(repo),
            "--agent", sys.executable, "--agent", str(fake),
            "--max-rounds", "2", *extra_flags]
    return state, ud, argv


def _ud_started(ud, n):
    p = ud / "calls" / ("started.%d.json" % n)
    wait_for(lambda: p.exists(), timeout=90, message="round %d never started" % n)
    return json.loads(p.read_text(encoding="utf-8"))


def test_until_done_note_missed_in_round_one_is_deferred_then_delivered_once(
        tmp_path, monkeypatch):
    state, ud, argv = _ud_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("QWEN_STREAM", raising=False)      # streaming on (C4 default)
    errors = []
    seen = {}
    box = {}

    def spy_register(kind, title, run_path=None, **kw):
        # P3: the inbox must already exist when the loop registers its address
        seen["at_register"] = bool(run_path) and \
            (pathlib.Path(run_path) / "inbox" / "new").is_dir()
        return None
    monkeypatch.setattr(supervisor.watch, "register", spy_register)

    def queue_during(n, text):
        rec = _ud_started(ud, n)
        box["inbox"] = rec["inbox"]
        return notes.queue_note(rec["inbox"], "a1b2c3d4e5f6", text)
    th = _watchdog([("round 1 up", lambda: _ud_started(ud, 1)),
                    ("queue the missed note", lambda: box.update(
                        nid1=queue_during(1, "mind the utf8 boundary"))),
                    ("release round 1", lambda: (ud / "gate.1").touch()),
                    ("round 2 up", lambda: _ud_started(ud, 2)),
                    ("queue the loop-end note", lambda: box.update(
                        nid2=queue_during(2, "a note for the loop end"))),
                    ("release round 2", lambda: (ud / "gate.2").touch())], errors)
    code = supervisor.main(list(argv))
    th.join(timeout=120)
    assert not errors, errors
    assert not th.is_alive()
    assert code == supervisor.EXIT_OK, code
    run_dir = next(state.rglob("report.md")).parent

    # P3: the page saw an inbox already in place (init right after makedirs)
    assert seen.get("at_register") is True
    inbox = pathlib.Path(box["inbox"])
    assert inbox == run_dir / "inbox"
    assert (inbox / "new").is_dir() and (inbox / ".closed").exists()

    # every round got the inbox; DEFER rode only the round that could be followed
    rec1 = _ud_started(ud, 1)
    rec2 = _ud_started(ud, 2)
    assert rec1["inbox"] == str(inbox)
    assert rec1["defer"] == "1"                       # rounds 1 < max 2 (P2)
    assert rec2["inbox"] == str(inbox)
    assert rec2["defer"] is None                      # the last round defers to no one

    # the missed note: deferred by round 1, delivered ONCE into round 2's prompt
    p1 = (run_dir / "round-1.prompt.md").read_text(encoding="utf-8")
    p2 = (run_dir / "round-2.prompt.md").read_text(encoding="utf-8")
    assert "mind the utf8 boundary" not in p1
    assert "Notes from the main session" in p2
    assert p2.count("mind the utf8 boundary") == 1    # delivered once, not twice
    states = log_states(inbox)
    nid1, nid2 = box["nid1"], box["nid2"]
    assert [x["state"] for x in states[nid1]] == ["queued", "deferred", "delivered"]
    assert states[nid1][-1]["reason"] == "next_round"
    assert (inbox / "delivered" / ("%s.json" % nid1)).exists()
    # the loop-end note: pending through the last round, swept loop_ended (K5)
    assert [x["state"] for x in states[nid2]] == ["queued", "undelivered"]
    assert states[nid2][-1]["reason"] == "loop_ended"
    assert (inbox / "undelivered" / ("%s.json" % nid2)).exists()
    assert list((inbox / "new").iterdir()) == []

    # report.md carries the section with every note's latest state (K5)
    report_text = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## Steering notes" in report_text
    steering = report_text.split("## Steering notes", 1)[1].split("full diff:", 1)[0]
    assert nid1 in steering and nid2 in steering
    assert "mind the utf8 boundary" in steering and "a note for the loop end" in steering
    assert "delivered" in steering and "undelivered: loop_ended" in steering

    # P6: the streamed round's additive fields ride into round-N.json; the round
    # whose record lacked them carries nothing
    r1 = json.loads((run_dir / "round-1.json").read_text(encoding="utf-8"))
    assert r1["stream"] is True and r1["notes_delivered"] == 0 and r1["note_ids"] == []
    r2 = json.loads((run_dir / "round-2.json").read_text(encoding="utf-8"))
    assert "stream" not in r2 and "notes_delivered" not in r2 and "note_ids" not in r2


def test_until_done_stream_off_leaves_no_inbox(tmp_path, monkeypatch):
    state, ud, argv = _ud_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("QWEN_STREAM", "0")
    errors = []
    th = _watchdog([("release round 1", lambda: (ud / "gate.1").touch()),
                    ("release round 2", lambda: (ud / "gate.2").touch())], errors)
    code = supervisor.main(list(argv))
    th.join(timeout=120)
    assert not errors, errors
    assert code == supervisor.EXIT_OK
    run_dir = next(state.rglob("report.md")).parent
    assert not (run_dir / "inbox").exists()             # P5: the baseline layout
    rec1 = _ud_started(ud, 1)
    assert rec1["inbox"] is None and rec1["defer"] is None
    assert "## Steering notes" not in (run_dir / "report.md").read_text(encoding="utf-8")
    r1 = json.loads((run_dir / "round-1.json").read_text(encoding="utf-8"))
    assert "stream" not in r1


def test_watch_pointer_removal_takes_the_inbox_with_it(tmp_path, monkeypatch):
    # 4.2: the single-shot's inbox lives in the watch dir beside its pointer, and
    # _drop_activity -- the ONE removal path both the viewer's prune and `dismiss`
    # go through -- takes the inbox folder down with the activity file. An id
    # outside the grammar must drop nothing at all.
    monkeypatch.setenv("XDG_CACHE_HOME", posix(watch_cache(tmp_path)))
    wd = pointer_dir(tmp_path)
    wd.mkdir(parents=True, exist_ok=True)
    rid = "abcd1234ef56"
    (wd / ("%s.json" % rid)).write_text(json.dumps({"kind": "agent", "id": rid}),
                                        encoding="utf-8")
    act = pathlib.Path(watch.activity_path(rid))
    act.write_text('{"tool": "Bash"}\n', encoding="utf-8")
    box = wd / ("%s.inbox" % rid)
    notes.init_inbox(str(box))
    notes.queue_note(str(box), rid, "doomed with the pointer")
    watch._drop_activity(rid)
    assert not act.exists()
    assert not box.exists()                               # new/ and its note go with it
    other = wd / "ffff00001111.inbox"                     # a real inbox...
    notes.init_inbox(str(other))
    watch._drop_activity("nope")                          # ...a malformed id touches
    assert other.exists()                                 # neither file


def test_until_done_deviation_audit_is_called_without_an_inbox(tmp_path, monkeypatch):
    # The deviation audit is NOT on the messaging path (4.6): its call_agent carries
    # no inbox and no deferral -- the plain, unstreamed launch -- while the rounds of
    # the same run are steered and the loop's own inbox is opened and swept as ever.
    state, ud, argv = _ud_setup(tmp_path, monkeypatch, extra_flags=())
    monkeypatch.delenv("QWEN_STREAM", raising=False)      # streaming on (C4 default)
    errors = []
    th = _watchdog([("release round 1", lambda: (ud / "gate.1").touch()),
                    ("release round 2", lambda: (ud / "gate.2").touch()),
                    ("release audit", lambda: (ud / "gate.3").touch()),
                    ("release audit retry", lambda: (ud / "gate.4").touch())], errors)
    code = supervisor.main(list(argv))
    th.join(timeout=120)
    assert not errors, errors
    assert code == supervisor.EXIT_OK, code               # the clean audit keeps done->OK
    run_dir = next(state.rglob("report.md")).parent
    assert (run_dir / "inbox" / "new").is_dir()           # the LOOP was steerable
    assert (run_dir / "inbox" / ".closed").exists()       # swept at the loop's end
    assert _ud_started(ud, 2)["inbox"] == str(run_dir / "inbox")   # rounds carried it
    n = 3                                                 # the audit call(s), if any:
    while (ud / "calls" / ("started.%d.json" % n)).exists() \
            or n == 3:                                    # (n=3 must exist: it ran)
        rec = _ud_started(ud, n)
        assert rec["role"] == "auditor", rec
        assert rec["inbox"] is None, rec                  # no QWEN_INBOX, ever
        assert rec["defer"] is None, rec                  # and no deferral
        n += 1
    assert n > 3                                          # the audit did run
    assert n <= 5                                         # capped at the two attempts


# ========================================================== swarm (4.6/K2/K3/P6)

FAKE_UNIT_AGENT = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["QWEN_SKILL_DIR"])

d = os.environ["SU_DIR"]
argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


prompt = opt("-f")
unit = os.path.basename(prompt)[: -len(".prompt.md")]
cnt = os.path.join(d, "%s.n" % unit)
k = 1
if os.path.exists(cnt):
    k = int(open(cnt).read()) + 1
open(cnt, "w").write(str(k))
rec = {"unit": unit, "k": k, "resume": opt("--resume"),
       "inbox": os.environ.get("QWEN_INBOX"),
       "defer": os.environ.get("QWEN_INBOX_DEFER")}
with open(os.path.join(d, "%s.%d.json" % (unit, k)), "w") as fh:
    json.dump(rec, fh)
gate = os.path.join(d, "gate.%s.%d" % (unit, k))
t0 = time.time()
while not os.path.exists(gate) and time.time() - t0 < 90:
    time.sleep(0.05)
if os.environ.get("SU_JUNK_FIRST") == "1" and k == 1:
    result = "no fenced block anywhere"
else:
    result = '```json\n{"ok": true}\n```'
out = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
       "duration_ms": 5, "result": result, "session_id": "sess-%s-%d" % (unit, k),
       "usage": {"input_tokens": 7, "output_tokens": 3}, "permission_denials": []}
if os.environ.get("SU_STREAMED") == "1":
    out["stream"] = True
    out["notes_delivered"] = int(os.environ.get("SU_NOTES", "0"))
    out["note_ids"] = []
print(json.dumps(out))
'''


def _unit_fake(tmp_path):
    p = tmp_path / "fake-unit-agent.py"
    p.write_text(FAKE_UNIT_AGENT, encoding="utf-8")
    return p


def _role_file(tmp_path, name):
    tmp_path.mkdir(parents=True, exist_ok=True)
    r = tmp_path / ("%s.md" % name)
    r.write_text("role %s\n" % name, encoding="utf-8")
    return r


def _unit(tmp_path, name):
    return sw.Unit(name=name, role_file=_role_file(tmp_path, name),
                   prompt="do %s" % name, toolset="none", grants="", web=False,
                   mcp_config=None)


def _swarm_setup(tmp_path, monkeypatch, seats=2):
    monkeypatch.setenv("QWEN_SKILL_DIR", posix(SKILL))
    su = tmp_path / "su"
    su.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SU_DIR", posix(su))
    run_dir = tmp_path / "run"
    sw.Swarm([sys.executable, str(_unit_fake(tmp_path))], run_dir,
             seats=seats, timeout=60, backoff=0)        # makes agents/ (and run_dir)
    return su, run_dir


def _su_wait(su, unit, k):
    p = su / ("%s.%d.json" % (unit, k))
    wait_for(lambda: p.exists(), timeout=90,
             message="%s call %d never started" % (unit, k))
    return json.loads(p.read_text(encoding="utf-8"))


def _swarm_pointer(tmp_path, rid, run_path):
    wd = pointer_dir(tmp_path)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / ("%s.json" % rid)).write_text(
        json.dumps({"kind": "swarm", "id": rid, "title": "s", "pid": os.getpid(),
                    "run_path": str(run_path), "start": round(time.time(), 3)}),
        encoding="utf-8")


def _run_swarm(s, units, steps, errors):
    """run_phase on a worker thread; the test-side steps drive the gates on theirs."""
    th = _watchdog(steps, errors)
    holder = []
    th2 = threading.Thread(target=lambda: holder.append(s.run_phase(units)))
    th2.start()
    th.join(timeout=120)
    th2.join(timeout=120)
    assert not errors, errors
    assert not th.is_alive() and not th2.is_alive()
    return holder[0]


def test_swarm_note_reaches_only_that_unit(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    su, run_dir = _swarm_setup(tmp_path, monkeypatch)
    rid = "ab12cd34ef56"
    _swarm_pointer(tmp_path, rid, run_dir)
    env = _base_env(tmp_path, {"XDG_CACHE_HOME": posix(watch_cache(tmp_path))})
    a, b = _unit(tmp_path, "alpha"), _unit(tmp_path, "beta")
    s = _swarm(tmp_path, su, run_dir, seats=2)
    box = {}
    steps = [("alpha up", lambda: _su_wait(su, "alpha", 1)),
             ("beta up", lambda: _su_wait(su, "beta", 1)),
             ("send alpha", lambda: box.update(r_a=cli(
                 tmp_path, env, "--send", "%s/alpha" % rid, "note for alpha"))),
             ("send beta", lambda: box.update(r_b=cli(
                 tmp_path, env, "--send", "%s/beta" % rid, "note for beta"))),
             ("bare id", lambda: box.update(r_bare=cli(
                 tmp_path, env, "--send", rid, "no unit given"))),
             ("no such unit", lambda: box.update(r_bad=cli(
                 tmp_path, env, "--send", "%s/nosuch" % rid, "t"))),
             ("release alpha", lambda: (su / "gate.alpha.1").touch()),
             ("release beta", lambda: (su / "gate.beta.1").touch())]
    results = _run_swarm(s, [a, b], steps, box.setdefault("errors", []))
    assert all(r["ok"] for r in results)
    r_a, r_b = box["r_a"], box["r_b"]
    assert r_a.returncode == 0 and NOTE_ID_RE.fullmatch(r_a.stdout.strip()), r_a.stderr
    assert r_b.returncode == 0 and NOTE_ID_RE.fullmatch(r_b.stdout.strip()), r_b.stderr
    nid_a, nid_b = r_a.stdout.strip(), r_b.stdout.strip()
    # each note landed in exactly its unit's inbox (spec 4.1 resolution, K2 path).
    # run_unit's finally has already swept new/ by now, so the landing place is read
    # from the unit's own undelivered/ -- and never from the other unit's.
    assert (run_dir / "inbox" / "alpha" / "undelivered" / ("%s.json" % nid_a)).exists()
    assert not (run_dir / "inbox" / "beta" / "undelivered" / ("%s.json" % nid_a)).exists()
    assert (run_dir / "inbox" / "beta" / "undelivered" / ("%s.json" % nid_b)).exists()
    assert not (run_dir / "inbox" / "alpha" / "undelivered" / ("%s.json" % nid_b)).exists()
    # the bare run id of a swarm needs a /<unit> (2) and lists the running units;
    # a unit that never existed is unavailable (3)
    assert box["r_bare"].returncode == 2
    assert "alpha" in box["r_bare"].stderr and "beta" in box["r_bare"].stderr
    assert box["r_bad"].returncode == 3
    # run_unit's finally closed both inboxes (4.6): .closed plus every pending note
    # moved and logged undelivered: run_ended
    for unit, nid in (("alpha", nid_a), ("beta", nid_b)):
        inbox = run_dir / "inbox" / unit
        assert (inbox / ".closed").exists()
        assert [x["state"] for x in log_states(inbox)[nid]] == ["queued", "undelivered"]
        assert log_states(inbox)[nid][-1]["reason"] == "run_ended"
        assert (inbox / "undelivered" / ("%s.json" % nid)).exists()
    # a note to a unit that is no longer running: 3
    assert cli(tmp_path, env, "--send", "%s/alpha" % rid, "late").returncode == 3


def test_swarm_resume_removes_stale_closed(tmp_path, monkeypatch):
    # K2/P3: the unit's inbox is made BEFORE unit_start, and the `.closed` an
    # interrupted earlier run (qwen-swarm --resume) left behind is removed with it
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    su, run_dir = _swarm_setup(tmp_path, monkeypatch, seats=1)
    rid = "ab12cd34ef57"
    _swarm_pointer(tmp_path, rid, run_dir)
    env = _base_env(tmp_path, {"XDG_CACHE_HOME": posix(watch_cache(tmp_path))})
    stale = run_dir / "inbox" / "gamma"
    notes.init_inbox(str(stale))
    notes.close_inbox(str(stale), "run_ended")              # the interrupted run's mark
    assert (stale / ".closed").exists()
    seen = {}
    real_emit = events.emit

    def spy_emit(run_dir_, kind, **fields):
        if kind == "unit_start":
            box = run_dir_ / "inbox" / str(fields.get("unit"))
            seen["new_dir"] = (box / "new").is_dir()
            seen["closed"] = (box / ".closed").exists()
        return real_emit(run_dir_, kind, **fields)
    monkeypatch.setattr(events, "emit", spy_emit)
    box = {}
    steps = [("gamma up", lambda: _su_wait(su, "gamma", 1)),
             ("send", lambda: box.update(r=cli(tmp_path, env, "--send",
                                                "%s/gamma" % rid, "live note"))),
             ("release", lambda: (su / "gate.gamma.1").touch())]
    results = _run_swarm(_swarm(tmp_path, su, run_dir), [_unit(tmp_path, "gamma")],
                         steps, box.setdefault("errors", []))
    assert results[0]["ok"]
    assert seen["new_dir"] is True and seen["closed"] is False  # open when announced
    assert box["r"].returncode == 0, box["r"].stderr + box["r"].stdout


def _swarm(tmp_path, su, run_dir, seats=1):
    return sw.Swarm([sys.executable, str(_unit_fake(tmp_path))], run_dir,
                    seats=seats, timeout=60, backoff=0)


def test_swarm_repair_call_has_no_inbox(tmp_path, monkeypatch):
    # 4.6/K2: the first call (and plain retries) get QWEN_INBOX; REPAIR gets an env
    # WITHOUT it -- and the swarm never defers (P2)
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    su, run_dir = _swarm_setup(tmp_path, monkeypatch, seats=1)
    monkeypatch.setenv("SU_JUNK_FIRST", "1")
    box = {}
    steps = [("call 1", lambda: _su_wait(su, "delta", 1)),
             ("release 1", lambda: (su / "gate.delta.1").touch()),
             ("call 2", lambda: _su_wait(su, "delta", 2)),
             ("release 2", lambda: (su / "gate.delta.2").touch())]
    results = _run_swarm(_swarm(tmp_path, su, run_dir), [_unit(tmp_path, "delta")],
                         steps, box.setdefault("errors", []))
    assert results[0]["ok"] and results[0]["data"] == {"ok": True}
    c1 = _su_wait(su, "delta", 1)
    c2 = _su_wait(su, "delta", 2)
    assert c1["inbox"] == str(run_dir / "inbox" / "delta")   # the first call: steerable
    assert c1["defer"] is None                               # P2: swarm never defers
    assert c2["resume"] == "sess-delta-1"                    # the REPAIR call
    assert c2["inbox"] is None                               # 4.6: no inbox for REPAIR


def test_swarm_steered_unit_is_not_cached(tmp_path, monkeypatch):
    # K3: an answer whose session received a note (notes_delivered > 0 on that
    # attempt's record) is not cached under the unsteered key; an unsteered answer
    # caches exactly as before
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    monkeypatch.setenv("SU_STREAMED", "1")

    def one(tag, notes_count):
        sub = tmp_path / tag
        su, run_dir = _swarm_setup(sub, monkeypatch, seats=1)
        monkeypatch.setenv("SU_NOTES", str(notes_count))
        box = {}
        steps = [("up", lambda: _su_wait(su, "epsilon", 1)),
                 ("release", lambda: (su / "gate.epsilon.1").touch())]
        results = _run_swarm(_swarm(sub, su, run_dir), [_unit(sub, "epsilon")],
                             steps, box.setdefault("errors", []))
        assert results[0]["ok"]
        return (run_dir / "agents" / "epsilon.json").exists()
    assert one("steered", 1) is False          # steered attempt: never cached
    assert one("plain", 0) is True             # unsteered: cached exactly as always


def test_swarm_stream_off_creates_no_inbox_and_keeps_one_arg_spawn(tmp_path, monkeypatch):
    # P5: with QWEN_STREAM=0 a unit gets no inbox at all -- and with no extra env
    # the plain one-argument _spawn(argv) call survives (K3)
    monkeypatch.setenv("QWEN_STREAM", "0")
    su, run_dir = _swarm_setup(tmp_path, monkeypatch, seats=1)
    s = _swarm(tmp_path, su, run_dir)
    calls = []
    real_spawn = s._spawn

    def spy_spawn(*args, **kw):
        calls.append(len(args) + len(kw))
        return real_spawn(*args, **kw)
    s._spawn = spy_spawn
    box = {}
    steps = [("up", lambda: _su_wait(su, "zeta", 1)),
             ("release", lambda: (su / "gate.zeta.1").touch())]
    results = _run_swarm(s, [_unit(tmp_path, "zeta")], steps,
                         box.setdefault("errors", []))
    assert results[0]["ok"]
    assert calls == [1]                                   # K3: _spawn(argv), env None
    assert not (run_dir / "inbox").exists()               # P5: nothing was created
    assert _su_wait(su, "zeta", 1)["inbox"] is None


# ================================================== review round and record (4.5/K2)

def test_review_round_sends_its_own_prompt(tmp_path, server):   # noqa: F811
    # 4.3: the driver's prompt is the LAST element of CLAUDE_ARGV -- the review
    # round's own prompt, not $PROMPT; and the two streamed records sum their
    # per-turn figures while total_cost_usd stays the LAST (cumulative) one (rev 3).
    fake, witness = witness_fake(tmp_path)
    markers, gates, lines = fake_dirs(tmp_path)
    extra = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
             "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
             "QWEN_BASE_URL": base_url(server), "QWEN_CLAUDE_BIN": posix(fake)}
    extra.update(stream_env(markers, gates, lines, gated=False))
    extra["WITNESS_DIR"] = posix(witness)
    r = run(tmp_path, ["--warn-denials", "--review-round", "--json", "say hi"],
            None, None, extra=extra)
    assert r.returncode == 0, r.stdout + r.stderr
    argv2, _ = wcall(witness, 2)                       # the review call went out
    assert "--resume" in argv2                         # ...resuming the first session
    assert argv2[-2:] == STREAM_TAIL                   # driver-built: no prompt tail
    contents = [g["message"]["content"] for g in received_lines(lines)]
    assert contents[0] == "say hi"
    assert any(c.startswith("Review round: before your answer is final")
               for c in contents[1:]), contents         # its OWN prompt, forwarded
    rec = json.loads(r.stdout)
    assert rec["stream"] is True
    assert rec["num_turns"] == 2                       # one per call, summed
    assert rec["total_cost_usd"] == 0.01               # LAST, not the sum: cumulative
    assert rec["notes_delivered"] == 0 and rec["note_ids"] == []
    assert rec["qwen_agent"]["review_round"]["status"] == "ok"


def test_review_round_call_two_forwards_a_pending_note(tmp_path, server):   # noqa: F811
    # 4.5: the review call delivers pending notes like a round -- while P2 keeps it
    # un-deferred; the note that call 1 never saw reaches call 2's session.
    fake, witness = witness_fake(tmp_path)
    markers, gates, lines = fake_dirs(tmp_path)
    extra = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
             "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
             "QWEN_BASE_URL": base_url(server), "QWEN_CLAUDE_BIN": posix(fake)}
    extra.update(stream_env(markers, gates, lines, gated=True))
    extra["WITNESS_DIR"] = posix(witness)
    env = _base_env(tmp_path, extra)
    proc = subprocess.Popen([BASH, posix(AGENT), "--warn-denials", "--review-round",
                             "--json", "say hi"], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8",
                            errors="replace", cwd=str(tmp_path))
    try:
        rid, _pointer = wait_pointer(tmp_path)
        inbox = inbox_dir_for(tmp_path, rid)
        wait_for(lambda: (markers / "m_tool_waited").exists(), timeout=60,
                 message="call 1 never parked in its tool")
        (gates / "g_tool").touch()
        wait_for(lambda: (markers / "m_pre_result1").exists(), timeout=60,
                 message="call 1 never reached its result gate")
        (gates / "g_result1").touch()                  # call 1 finishes now
        # the m_result1 marker PROVES call 1 saw its gate before we take the gates
        # away again (the fake polls every 0.02 s: unlinking the instant we touch
        # would race the poll and park it to a 30 s give-up exit)
        wait_for(lambda: (markers / "m_result1").exists(), timeout=60,
                 message="call 1 never emitted its result")
        # fresh gates for call 2 (the fake parks only while the file is absent)
        for g in ("g_tool", "g_result1"):
            with contextlib.suppress(OSError):
                os.unlink(gates / g)
        wait_for(lambda: (witness / "call.2").exists(), timeout=60,
                 message="the review call never started")
        nid = notes.queue_note(str(inbox), rid, "steer before the review")
        wait_for(lambda: any("steer before the review" in g["message"]["content"]
                             for g in received_lines(lines)), timeout=30,
                 message="the pending note never reached a session")
        (gates / "g_tool").touch()
        (gates / "g_result1").touch()
        out, err = proc.communicate(timeout=90)
        assert proc.returncode == 0, out + err
    finally:
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
    rec = json.loads(out)
    assert nid in rec["note_ids"] and rec["notes_delivered"] >= 1
    assert (inbox / "delivered" / ("%s.json" % nid)).exists()
    assert (inbox / ".closed").exists()                # the single-shot owns its inbox
    _argv2, env2 = wcall(witness, 2)
    assert "QWEN_INBOX" not in env2                    # scrubbed, always


# ====================================== --record: the record round runs un-inboxed

@pytest.mark.skipif(os.name != "posix", reason="the fake node is a bash script")
def test_record_scripts_runs_without_an_inbox(tmp_path, server, recorder, fake_scen,   # noqa: F811
                                              toolchain):                                # noqa: F811
    # K2: record_scripts() runs run_claude with QWEN_INBOX cleared -- the record
    # round is the plain old call (its argv carries --output-format json and the
    # trailing -- PROMPT), while the run's OWN call streamed.
    suite = tmp_path / "suite.md"
    suite.write_text(test_scenarios.UNIT, encoding="utf-8")
    ids = ("add", "s2", "s3")
    src = tmp_path / "replay-src"                      # the recorder fixture also makes it
    src.mkdir(exist_ok=True)
    for sid in ids:
        (src / ("%s.mjs" % sid)).write_text(script(sid), encoding="utf-8")
    rec = tmp_path / "recording"
    answer = test_scenarios.report([{"id": i, "status": "PASS"} for i in ids])
    fake, _witness = witness_fake(tmp_path, "fake-claude-rec")
    extra = dict(renv(tmp_path, toolchain),
                 FAKE_SCEN_BIN=posix(fake_scen), FAKE_REPLAY_SRC=posix(src),
                 WRAP_RECORDER=posix(recorder),
                 QWEN_WATCH="1", QWEN_WATCH_NOSERVE="1",
                 XDG_CACHE_HOME=posix(watch_cache(tmp_path)))
    r = run(tmp_path, ["--scenarios", posix(suite), "--record", posix(rec)],
            server, fake, extra=test_scenarios.senv(tmp_path, answer=answer, extra=extra))
    assert r.returncode == 0, r.stdout + r.stderr
    called = scen_calls(tmp_path)                      # what the recorder fake received
    assert len(called) == 2, called
    main_argv, record_argv = called[0][0], called[1][0]
    # the main call: driver-built (stream flags in, no --output-format json, no
    # prompt tail -- the driver took the prompt to the prompt file)
    assert "--input-format" in main_argv and "--replay-user-messages" in main_argv
    assert "--" not in main_argv
    # the record call: today's argv -- the pair, the trailing prompt, no stream flags
    assert record_argv[-2] == "--" and "deterministic replay script" in record_argv[-1]
    i = record_argv.index("--output-format")
    assert record_argv[i + 1] == "json"
    assert "--input-format" not in record_argv


# ======================================================= the timeout contract (4.3)

HANG_AFTER_RESULT = r'''
import json, sys, time
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "num_turns": 1, "duration_ms": 5,
                  "result": "the turn that finished before the deadline",
                  "session_id": "s-timing", "usage": {}, "permission_denials": []}))
sys.stdout.flush()
sys.stdin.read()          # the driver closes stdin; keep the session alive anyway
time.sleep(120)
'''


def test_stream_on_timeout_labels_the_payload_last_finished_turn(tmp_path, server):   # noqa: F811
    # 4.3: TIMEOUT with a non-empty $RAW still prints the payload to stderr -- and
    # since the streamed driver only ever writes FINISHED turns there, the message
    # names it as claude's last finished turn before the deadline.
    hang = tmp_path / "hang-claude.py"
    hang.write_text(HANG_AFTER_RESULT, encoding="utf-8")
    fake, _witness = witness_fake(tmp_path)
    markers, gates, lines = fake_dirs(tmp_path)
    extra = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
             "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
             "QWEN_BASE_URL": base_url(server), "QWEN_CLAUDE_BIN": posix(fake),
             "FAKE_PY": sys.executable, "FAKE_STREAM_PATH": posix(hang),
             "FAKE_MARKER_DIR": posix(markers), "FAKE_LINES_FILE": posix(lines)}
    r = run(tmp_path, ["--timeout", "3", "--warn-denials", "take your time"],
            None, None, extra=extra, timeout=120)
    assert r.returncode == 5, r.stdout + r.stderr     # QA_TIMEOUT
    assert "claude's last finished turn before the deadline" in r.stderr
    assert "the turn that finished before the deadline" in r.stderr
