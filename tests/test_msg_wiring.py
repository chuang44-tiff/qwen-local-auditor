"""The qla_msg wiring (spec 12.2): driver markers, launcher server, cleanup.

Covers the two halves of "every streamed agent has a message dir and the reply
tool": the DRIVER marks delivered asks and counts main notes in it (spec 12.2),
and the LAUNCHER creates the dir, writes the MCP config, grants the tools and
hands the driver --msg-dir -- only when it will use the driver at all (spec
12.7). watch._drop_activity takes the single-shot <id>.msg with the rest of the
run (spec 4.1).

Reuse, per the plan: tests/test_stream_driver.py's `Run` (the real driver over
the fake stream claude), tests/test_messaging_wiring.py's cache helpers, and
tests/test_cli.py's launcher harness.
"""
import json
import os
import pathlib
import queue
import sys
import threading

import pytest

from lib import msgdir, notes, stream_driver, watch
from test_cli import posix, run                                    # noqa: F401
from test_messaging_wiring import same_path, watch_cache           # noqa: F401
from test_stream_driver import RUN_ID, Run                         # noqa: F401

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="symlinks and the uid check are POSIX")


@pytest.fixture
def fake(tmp_path):
    """A stand-in claude (the test_cli_deep.py convention: each module owns its
    fixture): it only has to exist and be executable for the launcher's
    command -v, and answer the --help capability probe. A --dry-run never
    starts it."""
    p = tmp_path / "fake-claude"
    p.write_text('#!/usr/bin/env bash\n'
                 'if [ "${1:-}" = --help ]; then\n'
                 '  echo "  --restricted  Restricted mode"\n'
                 '  exit 0\n'
                 'fi\n'
                 'exit 0\n', encoding="utf-8", newline="\n")
    p.chmod(0o755)
    return p


def _dry_argv(r):
    """The argv lines of a --dry-run, without the brackets (the test_cli.py shape)."""
    return [ln.strip()[1:-1] for ln in r.stdout.splitlines() if ln.startswith("  [")]


def _flag(argv, name):
    """The value after `name` in an argv list, or None."""
    return argv[argv.index(name) + 1] if name in argv else None


def _stub_driver(inbox, msg_dir=None):
    """A Driver built without __init__ (the test_notes_render.py pattern): enough
    state for forward_note and _deliver, no threads, no claude."""
    d = stream_driver.Driver.__new__(stream_driver.Driver)
    d.cond = threading.Condition()
    d._wake = 0
    d.pending = {}
    d.unacked = {}
    d.seen_ids = set()
    d.forwarded = 0
    d.wqueue = queue.Queue()
    d.inbox = str(inbox)
    d.activity = None
    d.msg_dir = str(msg_dir) if msg_dir else None
    d._main_warned = False
    return d


def _body(note_id, kind="note", text="hello there"):
    sender = {"share": "unit alpha", "harness": "harness"}.get(kind, "main")
    body = {"id": note_id, "t": 1700000000.125, "from": sender, "to": RUN_ID,
            "kind": kind, "text": text}
    if kind == "share":
        body["role"] = "prober"
    return body


# ================================================================ driver markers

def test_ask_marker_before_forward(tmp_path, monkeypatch):
    """The reply tool only answers a question whose seen marker exists, so the
    marker MUST be on disk before the note's stdin line is queued: the fake
    writer records, at the instant the line lands in the queue, whether
    seen/ask-<id> already exists."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    inbox = tmp_path / "inbox"
    notes.init_inbox(str(inbox))
    msg = tmp_path / "msg"
    msgdir.init_msgdir(str(msg))
    driver = _stub_driver(inbox, msg)

    events = []

    class RecordingQueue(queue.Queue):
        def put(self, item):
            note_id = item[0]
            events.append((note_id,
                           msgdir.ask_seen(str(msg), note_id) if note_id else None))
            queue.Queue.put(self, item)

    driver.wqueue = RecordingQueue()

    ask_id = notes.new_note_id()
    entry = driver.forward_note(_body(ask_id, "ask", "how far?"),
                                str(inbox / notes.NEW / ("%s.json" % ask_id)), ask_id)
    assert entry is not None, "the ask was not forwarded"
    # at the moment its line was queued, the marker was already there
    assert events == [(ask_id, True)]
    assert msgdir.ask_seen(str(msg), ask_id)

    # a plain note forwards without marking anything (only asks get a marker)
    note_id = notes.new_note_id()
    entry = driver.forward_note(_body(note_id),
                                str(inbox / notes.NEW / ("%s.json" % note_id)), note_id)
    assert entry is not None
    assert events[-1] == (note_id, False)


@posix_only
def test_marker_failure_sweeps_note(tmp_path, monkeypatch):
    """An unwritable seen/ (chmod 0500) means the ask can never be answered: it is
    swept undelivered: marker_failed and never reaches stdin -- while a plain note
    (needs no marker) still goes through end to end."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    msg = tmp_path / "msg"
    msgdir.init_msgdir(str(msg))
    seen = msg / "seen"
    seen.chmod(0o500)
    if os.access(str(seen), os.W_OK):
        pytest.skip("a read-only directory is still writable here")

    r = Run(tmp_path / "drv", mode="ok", gated=True, poll="0.2", start=False)
    ask_id = notes.queue_note(str(r.inbox), RUN_ID, "how far along?", kind="ask")
    note_id = r.queue("please steer")
    at = r.argv.index("--")
    r.argv[at:at] = ["--msg-dir", str(msg)]
    try:
        r.launch()
        r.marker("m_note_echoed")                 # the plain note echoed at the gate
        r.gate("g_tool")
        r.gate("g_result1")
        assert r.wait() == 0, r.stderr
    finally:
        r.cleanup()

    # the ask: swept undelivered with the marker's reason, logged, and marked
    # nowhere -- and its line never reached claude's stdin
    assert (r.inbox / notes.UNDELIVERED / ("%s.json" % ask_id)).is_file()
    assert not (r.inbox / notes.NEW / ("%s.json" % ask_id)).exists()
    swept = [line for line in r.log()
             if line.get("id") == ask_id and line.get("state") == "undelivered"]
    assert swept and swept[0]["reason"] == "marker_failed"
    assert not msgdir.ask_seen(str(msg), ask_id)
    texts = [obj["message"]["content"] for obj in r.received()]
    assert not any("how far along?" in t for t in texts)

    # the plain note: forwarded, echoed, acknowledged, delivered
    assert any("please steer" in t for t in texts)
    assert (r.inbox / notes.DELIVERED / ("%s.json" % note_id)).is_file()
    rec = r.record()
    assert rec["notes_delivered"] == 1 and rec["note_ids"] == [note_id]


def test_main_count_on_echo_only_for_note_and_ask(tmp_path, monkeypatch):
    """seen/main-count counts the echo of a `note` or an `ask` (a body without a
    kind is a phase 1 note) and nothing else: share and harness do not count."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    inbox = tmp_path / "inbox"
    notes.init_inbox(str(inbox))
    msg = tmp_path / "msg"
    msgdir.init_msgdir(str(msg))
    driver = _stub_driver(inbox, msg)

    for kind in ("note", "ask", "share", "harness"):
        driver._deliver(notes.new_note_id(), {"body": _body("x", kind), "path": None})
    assert msgdir.main_count(str(msg)) == 2

    # a missing kind counts as a note (phase 1 bodies)
    driver._deliver(notes.new_note_id(),
                    {"body": {"id": "y", "t": 1700000000.0, "from": "main",
                              "to": RUN_ID, "text": "old body"}, "path": None})
    assert msgdir.main_count(str(msg)) == 3

    # a driver without --msg-dir counts nothing and never raises
    plain = _stub_driver(inbox, msg_dir=None)
    plain._deliver(notes.new_note_id(), {"body": _body("z", "note"), "path": None})
    assert msgdir.main_count(str(msg)) == 3


# ================================================================ launcher wiring

def _stream_base(tmp_path, inbox):
    """The env of a dry run the launcher would stream: QWEN_INBOX set by a parent,
    the page nominally on, and TMPDIR under tmp_path so the dry run's run folder
    (which holds the config file it prints) lands where pytest cleans up."""
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
            "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
            "TMPDIR": posix(tmp_path / "tmp"),
            "QWEN_INBOX": posix(inbox)}


def test_dry_run_has_qla_msg_config_and_reply_grant(tmp_path, fake):
    parent_inbox = tmp_path / "a1b2c3d4e5f6.inbox"
    notes.init_inbox(str(parent_inbox))
    base = _stream_base(tmp_path, parent_inbox)

    r = run(tmp_path, ["--dry-run", "hi"], None, fake, extra=dict(base))
    assert r.returncode == 0, r.stderr
    argv = _dry_argv(r)
    cfgs = [argv[i + 1] for i, a in enumerate(argv) if a == "--mcp-config"]
    assert len(cfgs) == 1, "one --mcp-config with no caller config: %r" % argv
    cfg = json.loads(pathlib.Path(cfgs[0]).read_text(encoding="utf-8"))
    assert "qla_msg" in cfg["mcpServers"], cfgs[0]
    srv = cfg["mcpServers"]["qla_msg"]
    assert os.path.basename(str(srv["args"][-1])) == "msg_mcp.py"
    assert os.path.basename(str(srv["command"])).startswith("python")
    env = srv["env"]
    expected_msg = str(parent_inbox)[:-len(".inbox")] + ".msg"   # .inbox -> .msg
    assert same_path(env["QWEN_MSG_DIR"], expected_msg)
    assert env["QWEN_MSG_TOOLS"] == "reply"           # the default when unset
    assert env["PYTHONIOENCODING"] == "utf-8"
    for absent in ("QWEN_MSG_SELF", "QWEN_MSG_PHASE", "QWEN_ASKS_DIR", "QWEN_SHARE_MAX"):
        assert absent not in env, "%s is unset but in the config env: %r" % (absent, env)
    # the dir the launcher promised the agent is real, and shaped
    assert (pathlib.Path(expected_msg) / "seen").is_dir()
    assert (pathlib.Path(expected_msg) / "out" / "new").is_dir()

    grants = (_flag(argv, "--allowed-tools") or "").split(",")
    assert "mcp__qla_msg__reply" in grants, argv

    # a caller --mcp-config means TWO entries (claude merges several)
    caller = tmp_path / "caller.json"
    caller.write_text(json.dumps({"mcpServers": {"mine": {"command": "x", "args": []}}}),
                      encoding="utf-8")
    r = run(tmp_path, ["--dry-run", "--mcp-config", posix(caller), "hi"], None, fake,
            extra=dict(base, QWEN_MSG_TOOLS="reply, share , ask_main, bogus",
                       QWEN_MSG_SELF="a1b2c3d4e5f6", QWEN_MSG_PHASE="p1",
                       QWEN_ASKS_DIR=posix(tmp_path / "asks"), QWEN_SHARE_MAX="4"))
    assert r.returncode == 0, r.stderr
    argv = _dry_argv(r)
    cfgs = [argv[i + 1] for i, a in enumerate(argv) if a == "--mcp-config"]
    assert len(cfgs) == 2, "caller config plus qla_msg: %r" % argv
    qla = [c for c in cfgs
           if "qla_msg" in json.loads(pathlib.Path(c).read_text(encoding="utf-8"))
                                            .get("mcpServers", {})]
    assert len(qla) == 1, cfgs
    env = json.loads(pathlib.Path(qla[0]).read_text(encoding="utf-8"))["mcpServers"]["qla_msg"]["env"]
    assert env["QWEN_MSG_SELF"] == "a1b2c3d4e5f6"
    assert env["QWEN_MSG_PHASE"] == "p1"
    assert same_path(env["QWEN_ASKS_DIR"], tmp_path / "asks")
    assert env["QWEN_SHARE_MAX"] == "4"
    grants = (_flag(argv, "--allowed-tools") or "").split(",")
    for t in ("share", "reply", "ask_main"):
        assert "mcp__qla_msg__%s" % t in grants, argv
    assert "mcp__qla_msg__bogus" not in grants, argv      # only the three tools


def test_no_inbox_no_qla_msg(tmp_path, fake):
    """QWEN_STREAM=0: no driver, so no message dir, no server, no grants -- the
    printed argv equals the constant snapshot taken on the base tree (26f995f,
    before the qla_msg launcher block existed), argv[0] excepted: it
    names the claude binary the caller handed in."""
    r = run(tmp_path, ["--dry-run", "--no-timeout", "--no-autocompact", "hi"],
            None, fake,
            extra={"QWEN_WATCH": "0", "QWEN_WATCH_NOSERVE": "1",
                   "QWEN_STREAM": "0", "QWEN_MODEL": "snap-model",
                   "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
                   "TMPDIR": posix(tmp_path / "tmp")})
    assert r.returncode == 0, r.stderr
    argv = _dry_argv(r)
    # The two signatures of the wiring, checked precisely: no --mcp-config entry
    # at all (so no qla_msg server), and no mcp__qla_msg__ grant. A bare substring
    # scan for "qla_msg" would not do here: pytest names this test's tmp_path after
    # the test itself, so every path token carries the test's name -- including the
    # part that spells qla_msg. The snapshot equality below is the full check.
    assert "--mcp-config" not in argv, argv
    assert "mcp__qla_msg__" not in (_flag(argv, "--allowed-tools") or ""), argv
    assert argv[0] == posix(fake)
    assert argv[1:] == SNAPSHOT_STREAM0[1:]


# The --dry-run argv of `qwen-agent --dry-run --no-timeout --no-autocompact hi`
# with QWEN_WATCH=0, QWEN_STREAM=0, QWEN_MODEL=snap-model, QWEN_DEPTH=shallow and
# an absent config file -- captured on the base tree (this task's parent commit),
# argv[0] replaced by the placeholder the test fills with the caller's claude.
SNAPSHOT_STREAM0 = [
    "<CLAUDE-BIN>",
    "-p",
    "--model", "snap-model",
    "--output-format", "json",
    "--allowed-tools",
    "Bash(ls:*),Bash(grep:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(wc:*),"
    "Read,Glob,Grep",
    "--effort", "medium",
    "--tools", "Read,Glob,Grep",
    "--strict-mcp-config",
    "--", "hi",
]


def test_qla_msg_clash_refused(tmp_path, fake):
    """A caller --mcp-config that names qla_msg itself would leave two servers by
    one name: refused with the usage exit, like the qla_advisor check."""
    parent_inbox = tmp_path / "a1b2c3d4e5f6.inbox"
    notes.init_inbox(str(parent_inbox))
    caller = tmp_path / "clash.json"
    caller.write_text(json.dumps(
        {"mcpServers": {"qla_msg": {"command": "python3", "args": []}}}),
        encoding="utf-8")
    base = _stream_base(tmp_path, parent_inbox)
    r = run(tmp_path, ["--dry-run", "--mcp-config", posix(caller), "hi"], None, fake,
            extra=base)
    assert r.returncode == 2, "%d\n%s\n%s" % (r.returncode, r.stdout, r.stderr)
    assert "qla_msg" in r.stderr


def test_single_shot_msg_dir_dropped_with_activity(tmp_path, monkeypatch):
    """The single-shot <id>.msg belongs to its pointer: dismiss/prune's
    watch._drop_activity removes the folder with the activity file and the inbox,
    and an id that is not a run id touches nothing."""
    monkeypatch.setenv("XDG_CACHE_HOME", posix(watch_cache(tmp_path)))
    wd = watch_cache(tmp_path) / "qwen-agent" / "watch"
    wd.mkdir(parents=True)
    rid = "ffff00001111"
    (wd / ("%s.json" % rid)).write_text('{"id": "%s"}\n' % rid, encoding="utf-8")
    pathlib.Path(watch.activity_path(rid)).write_text("{}\n", encoding="utf-8")
    inbox = wd / ("%s.inbox" % rid)
    notes.init_inbox(str(inbox))
    msg = wd / ("%s.msg" % rid)
    msgdir.init_msgdir(str(msg))
    assert msg.is_dir() and (msg / "seen").is_dir()

    watch._drop_activity(rid)
    # the activity file, the inbox and the msg dir go; the POINTER is its caller's
    # to remove (dismiss unlinks it, the prune moves it aside) -- _drop_activity
    # itself never touched it, before this task or after it.
    assert not pathlib.Path(watch.activity_path(rid)).exists()
    assert not inbox.exists()
    assert not msg.exists()

    # a malformed id deletes nothing (the guard the inbox already obeys)
    other = wd / ("ffff00002222.msg")
    msgdir.init_msgdir(str(other))
    watch._drop_activity("nope")
    assert other.is_dir()
