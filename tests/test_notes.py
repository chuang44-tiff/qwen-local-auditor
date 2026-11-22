"""lib/notes.py, phase 1: the address grammar, the inbox files, the log, and the CLI.

Group A drives the module in-process (conftest puts skill/local-auditor on sys.path, so
`from lib import notes` is the library the tests exercise); Group B runs the CLI as a
subprocess (sys.executable NOTES_PY send/notes ...) against hand-planted watch-registry
pointers -- both halves see ONE watch dir, the test's own cache home."""
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

from lib import notes

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTES_PY = ROOT / "skill" / "local-auditor" / "lib" / "notes.py"
RUN_ID = "a1b2c3d4e5f6"        # a watch id as watch.ID_RE demands: 12 lowercase hex
ADDR = RUN_ID                  # the address of RUN_ID's own inbox (no unit)


# ---------------------------------------------------------------- helpers

def env_for(tmp_path):
    """The child env the way test_cli.run() builds it: no developer QWEN_*/CLAUDE_*/
    ANTHROPIC_* setting leaks in, the cache home and the config file are this test's
    own, the one-time notice stays silent. PYTHONIOENCODING so the id/outcome lines
    come back utf-8 whichever locale the runner carries."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env["QWEN_CONFIG"] = str(tmp_path / "no-config")
    env["QWEN_NO_NOTICE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@pytest.fixture
def note_env(tmp_path, monkeypatch):
    """One watch dir for both halves: the in-process lib and the CLI child resolve
    pointers and inboxes under this test's cache home."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("QWEN_CONFIG", str(tmp_path / "no-config"))
    return cache, env_for(tmp_path)


def write_pointer(cache, run_id=RUN_ID, *, kind="agent", run_path=None, pid=None,
                  end=None):
    """The registry pointer exactly as lib/watch.py's register() writes it (its
    lines 345-347), planted by hand: the test decides the kind, the pid and end."""
    wdir = cache / "qwen-agent" / "watch"
    os.makedirs(str(wdir), exist_ok=True)
    pointer = {"id": run_id, "kind": kind, "title": "t",
               "run_path": str(run_path) if run_path else None,
               "pid": os.getpid() if pid is None else pid,
               "start": time.time(), "timeout": None, "max_rounds": None,
               "end": end, "exit": None}
    (wdir / ("%s.json" % run_id)).write_text(json.dumps(pointer), encoding="utf-8")
    return wdir


def live_agent(cache, *, kind="agent", end=None, pid=None, run_path=None, with_inbox=True):
    """A pointer for RUN_ID plus (by default) the inbox resolve_address demands.
    The agent kind's inbox sits beside the pointer; until-done's in the run folder."""
    wdir = write_pointer(cache, RUN_ID, kind=kind, end=end, pid=pid, run_path=run_path)
    box = wdir / ("%s.inbox" % RUN_ID) if kind == "agent" else pathlib.Path(run_path) / "inbox"
    if with_inbox:
        notes.init_inbox(str(box))
    return RUN_ID, box


def swarm_run(tmp_path):
    """A swarm run folder whose events.jsonl folds alpha RUNNING and beta CACHED
    (watch.fold_events: unit_start says running, a cached unit_done says cached)."""
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    lines = [
        {"kind": "run_start", "t": 1.0},
        {"kind": "unit_start", "t": 1.1, "unit": "alpha", "role": "r", "timeout": 60},
        {"kind": "unit_done", "t": 1.2, "unit": "beta", "cached": True, "ok": True,
         "seconds": 1},
    ]
    (run / "events.jsonl").write_bytes(
        "".join(json.dumps(line) + "\n" for line in lines).encode("utf-8"))
    return run


def read_log_lines(inbox):
    """The parsed notes.jsonl, oldest first; [] when no log exists yet."""
    path = os.path.join(str(inbox), notes.LOG_FILE)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    return [json.loads(raw) for raw in data.decode("utf-8", "replace").splitlines()
            if raw.strip()]


def run_cli(env, tmp_path, args, input=None, timeout=30):
    """The CLI as a child: subprocess.run with a timeout -- run() kills and drains on
    expiry, so no CLI child can outlive the test that spawned it."""
    cmd = [sys.executable, str(NOTES_PY)] + list(args)
    kw = dict(env=env, capture_output=True, encoding="utf-8", errors="replace",
              cwd=str(tmp_path), timeout=timeout)
    if input is None:
        kw["stdin"] = subprocess.DEVNULL       # never let a child wait on the test's stdin
    else:
        kw["input"] = input
    return subprocess.run(cmd, **kw)


def dead_pid():
    """A pid we can prove gone: a real child, run to completion and reaped."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=30)
    return p.pid


def next_line(p, what):
    """One stdout line from a send --wait child; a child that exited before printing
    fails here with its rc in the message instead of hanging the suite."""
    line = p.stdout.readline()
    assert line, "the send child printed nothing (expected %s); rc %r" % (what, p.poll())
    return line.rstrip("\n")


@contextlib.contextmanager
def send_wait(env, tmp_path, address, seconds):
    """`send ADDRESS --wait SECONDS TEXT` as a pipe-backed child (K9: TEXT is one of
    the required alternatives, so the wait child always carries its own note); a 30 s
    watchdog kills any child still running when the test ends."""
    cmd = [sys.executable, str(NOTES_PY), "send", address, "--wait", str(seconds),
           "please wait for me"]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                         cwd=str(tmp_path))

    def _watchdog():
        with contextlib.suppress(OSError):
            p.kill()

    timer = threading.Timer(30.0, _watchdog)
    timer.daemon = True
    timer.start()
    try:
        yield p
    finally:
        timer.cancel()
        _watchdog()
        p.wait(timeout=30)


# ================================================================ Group A: the module API

def test_address_regex_accepts_refuses():
    unit64 = "u" + "x" * 63                              # 64 chars: the limit
    for ok in [RUN_ID, "0123456789ab", RUN_ID + "/x", RUN_ID + "/unit-1.2_3",
               RUN_ID + "/" + unit64]:
        assert notes.valid_address(ok), ok
    for bad in [None, "", "*", "A1b2c3d4e5f6", "a1b2c3d4e5f",      # uppercase, 11 hex
                "a1b2c3d4e5f67",                                    # 13 hex
                RUN_ID + "/",                                       # empty unit
                RUN_ID + "/.hidden", RUN_ID + "/..",                 # dot names cannot match
                RUN_ID + "/a/b",                                     # no second slash
                RUN_ID + "/" + "u" + "x" * 64,                       # 65-char unit
                RUN_ID + "/ unit"]:                                   # space, not a unit char
        assert not notes.valid_address(bad), bad


def test_address_split():
    assert notes.split_address("a1b2c3d4e5f6/x") == ("a1b2c3d4e5f6", "x")
    assert notes.split_address("a1b2c3d4e5f6") == ("a1b2c3d4e5f6", None)


def test_init_inbox_layout(tmp_path):
    inbox = tmp_path / "inbox"
    os.makedirs(str(inbox))
    (inbox / notes.CLOSED).write_text("stale", encoding="utf-8")   # an interrupted run's claim
    notes.init_inbox(str(inbox))
    for name in (notes.NEW, notes.DELIVERED, notes.UNDELIVERED):
        assert (inbox / name).is_dir()
    assert not (inbox / notes.CLOSED).exists()          # P3: the stale claim is removed
    if os.name == "posix":
        folders = [inbox] + [inbox / n for n in (notes.NEW, notes.DELIVERED, notes.UNDELIVERED)]
        for p in folders:
            assert os.stat(str(p)).st_mode & 0o777 == 0o700
    notes.init_inbox(str(inbox))                        # idempotent: a second owner is fine
    for name in (notes.NEW, notes.DELIVERED, notes.UNDELIVERED):
        assert (inbox / name).is_dir()


def test_inbox_note_file_written_by_rename(tmp_path):
    inbox = str(tmp_path / "inbox")
    notes.init_inbox(inbox)
    text = "the only line"
    nid = notes.queue_note(inbox, ADDR, text)
    new_dir = pathlib.Path(inbox) / notes.NEW
    names = sorted(os.listdir(str(new_dir)))
    assert names == [nid + ".json"]                     # landed by os.replace, no *.tmp
    assert not [n for n in names if n.endswith(".tmp")]
    assert notes.NOTE_ID_RE.match(nid), nid
    assert os.path.splitext(names[0])[0] == nid         # the stem is the id
    body = json.loads((new_dir / names[0]).read_text(encoding="utf-8"))
    assert set(body) == {"id", "t", "from", "to", "text"}
    assert body["id"] == nid and body["to"] == ADDR and body["from"] == "main"
    assert body["text"] == text
    assert isinstance(body["t"], float) and body["t"] == round(body["t"], 3)


def test_limit_chars_and_bytes():
    assert notes.validate_text("x" * 8000) == "x" * 8000
    with pytest.raises(notes.NotesError) as e:
        notes.validate_text("x" * 8001)
    assert e.value.code == 2
    with pytest.raises(notes.NotesError) as e:
        notes.validate_text("   \t\n  ")                # empty after strip
    assert e.value.code == 2
    # The brief asked for 8-byte emoji and a "5000-char 7-byte string"; UTF-8 tops out
    # at 4 bytes per code point (asserted below), so 4097 emoji are 16 388 B and legal,
    # and no <8 000-char string can pass the char limit yet breach the 32 768-byte one
    # (8 000 x 4 = 32 000): the byte check is unreachable -- reported, not asserted away.
    assert len("\U0001F600".encode("utf-8")) == 4
    assert notes.validate_text("\U0001F600" * 4096) == "\U0001F600" * 4096
    assert notes.validate_text("\U0001F600" * 4097) == "\U0001F600" * 4097
    assert notes.validate_text("\U0001F600" * 8000) == "\U0001F600" * 8000   # 32 000 B
    with pytest.raises(notes.NotesError) as e:
        notes.validate_text("\U0001F600" * 8001)
    assert e.value.code == 2


@pytest.mark.skipif(os.name != "posix",
                    reason="os.symlink may need privileges off POSIX")
def test_symlink_refusal(tmp_path):
    # (a) the inbox itself a symlink: init_inbox refuses it outright
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link-inbox"
    os.symlink(str(real), str(link))
    with pytest.raises(notes.NotesError) as e:
        notes.init_inbox(str(link))
    assert e.value.code == 3
    assert not (real / notes.NEW).exists()              # nothing created through the link

    # (b) a symlinked new/: queue_note refuses before writing a single byte
    inbox = tmp_path / "inbox"
    os.makedirs(str(inbox / notes.DELIVERED))
    os.makedirs(str(inbox / notes.UNDELIVERED))
    new_real = tmp_path / "new-real"
    new_real.mkdir()
    os.symlink(str(new_real), str(inbox / notes.NEW))
    with pytest.raises(notes.NotesError) as e:
        notes.queue_note(str(inbox), ADDR, "hi")
    assert e.value.code == 3
    assert list(new_real.iterdir()) == []

    # (c) a symlinked note file in new/: list_new skips it, load_note refuses to read it
    inbox2 = tmp_path / "inbox2"
    notes.init_inbox(str(inbox2))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    stem = "123456789-abcdef"                           # a well-formed note id
    target = elsewhere / (stem + ".json")
    target.write_text(json.dumps({"id": stem, "t": 1.0, "from": "main", "to": ADDR,
                                  "text": "a real note somewhere else"}), encoding="utf-8")
    linked = inbox2 / notes.NEW / (stem + ".json")
    os.symlink(str(target), str(linked))
    assert notes.list_new(str(inbox2)) == []            # a link is never forwarded
    with pytest.raises(notes.NotesError) as e:
        notes.load_note(str(linked))
    assert e.value.code == 3
    assert target.exists()                              # and never touched


def test_log_append_format(tmp_path):
    inbox = str(tmp_path / "inbox")
    notes.init_inbox(inbox)
    secret = "PLAINTEXT-MUST-NEVER-REACH-THE-LOG"
    nid = notes.queue_note(inbox, ADDR, secret)         # logs its own `queued`
    notes.log_note(inbox, nid, ADDR, "main", "delivered")
    notes.log_note(inbox, nid, ADDR, "main", "deferred")
    notes.log_note(inbox, nid, ADDR, "main", "undelivered", reason="crashed")
    raw = (pathlib.Path(inbox) / notes.LOG_FILE).read_bytes()
    lines = [ln for ln in raw.split(b"\n") if ln]
    assert len(lines) == 4
    allowed = {"t", "id", "to", "from", "state", "reason", "len"}
    recs = []
    for ln in lines:
        assert len(ln) < notes.MAX_LINE                 # every line stays under 4 KiB
        rec = json.loads(ln.decode("utf-8"))
        assert set(rec) <= allowed                      # no stray fields, no text
        recs.append(rec)
    assert [r["state"] for r in recs] == ["queued", "delivered", "deferred",
                                          "undelivered"]    # append order, all states
    assert secret.encode("utf-8") not in raw            # the text is never in the log
    assert all(r["id"] == nid for r in recs)
    assert recs[3]["reason"] == "crashed"
    assert recs[0]["to"] == ADDR and recs[0]["from"] == "main"
    assert recs[0]["len"] == len(secret)
    if os.name == "posix":
        assert os.stat(os.path.join(inbox, notes.LOG_FILE)).st_mode & 0o777 == 0o600


def test_close_inbox_sweeps_and_is_idempotent(tmp_path):
    inbox = str(tmp_path / "inbox")
    notes.init_inbox(inbox)
    id1 = notes.queue_note(inbox, ADDR, "first note")
    id2 = notes.queue_note(inbox, ADDR, "second note, longer")
    swept = notes.close_inbox(inbox, "run_ended")
    assert sorted(swept) == sorted([id1, id2])
    assert os.listdir(os.path.join(inbox, notes.NEW)) == []
    assert sorted(os.listdir(os.path.join(inbox, notes.UNDELIVERED))) == \
        sorted([id1 + ".json", id2 + ".json"])
    assert os.path.isfile(os.path.join(inbox, notes.CLOSED))
    sweep = [r for r in read_log_lines(inbox) if r["state"] == "undelivered"]
    assert len(sweep) == 2                              # one line per moved note...
    assert {r["reason"] for r in sweep} == {"run_ended"}
    assert {r["id"] for r in sweep} == {id1, id2}
    before = len(read_log_lines(inbox))
    assert notes.close_inbox(inbox, "loop_ended") == [] # the claim is the create: no re-sweep
    assert len(read_log_lines(inbox)) == before         # ...and it logs nothing at all
    with pytest.raises(notes.NotesError) as e:
        notes.queue_note(inbox, ADDR, "late note")
    assert e.value.code == 3                            # closed: no more notes accepted


def test_close_inbox_empty_still_claims(tmp_path):
    inbox = str(tmp_path / "inbox")
    notes.init_inbox(inbox)
    assert notes.close_inbox(inbox, "run_ended") == []  # nothing to move...
    assert os.path.isfile(os.path.join(inbox, notes.CLOSED))
    assert notes.is_closed(inbox)                       # ...but the claim stands
    assert read_log_lines(inbox) == []                  # empty sweep, empty log


# ================================================================ Group B: the CLI

def test_send_prints_id(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    res = run_cli(env, tmp_path, ["send", addr, "steer left a bit"])
    assert res.returncode == 0, res.stderr
    nid = res.stdout[:-1]
    assert res.stdout == nid + "\n" and notes.NOTE_ID_RE.match(nid), repr(res.stdout)
    body = json.loads((inbox / notes.NEW / (nid + ".json")).read_text(encoding="utf-8"))
    assert body["to"] == addr and body["text"] == "steer left a bit"
    recs = read_log_lines(inbox)
    assert [r["state"] for r in recs] == ["queued"] and recs[0]["id"] == nid


USAGE_CASES = [
    ["send", "XYZ/..", "t"],                       # bad grammar
    ["send", RUN_ID],                              # no text at all
    ["send", RUN_ID, "-f", "-", "text"],           # TEXT and -f together
    ["send", RUN_ID, "one", "two"],                # two TEXTs
    ["send", RUN_ID, "--wait"],                    # --wait without its value
    ["send", RUN_ID, "--wait", "0", "text"],       # out of range, low
    ["send", RUN_ID, "--wait", "3601", "text"],    # out of range, high
    ["send", RUN_ID, "--wait", "abc", "text"],     # not an integer
    ["send", RUN_ID, "-f"],                        # -f without its operand
    ["send", RUN_ID, "--bogus", "text"],           # unknown option after the address
    ["send", RUN_ID, "text", "--wait", "2"],       # options must come before TEXT
]


@pytest.mark.parametrize("args", USAGE_CASES,
                         ids=["bad-grammar", "no-text", "text-and-f", "two-texts",
                              "wait-no-value", "wait-0", "wait-3601", "wait-abc",
                              "f-no-operand", "unknown-option", "option-after-text"])
def test_send_exit2_usage(note_env, tmp_path, args):
    _, env = note_env
    # Deliberately no pointer and no inbox: a usage refusal must beat the
    # unavailable-by-address refusals (rc 3) that follow it.
    res = run_cli(env, tmp_path, args)
    assert res.returncode == 2, (args, res.stderr)
    assert res.stdout == ""


def test_send_exit2_oversize_text(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)                     # resolvable: the refusal is the text
    res = run_cli(env, tmp_path, ["send", addr, "x" * 8001])
    assert res.returncode == 2, res.stderr
    assert res.stdout == ""
    assert os.listdir(str(inbox / notes.NEW)) == []     # refused before writing


def test_send_dashdash_leading_dash(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    res = run_cli(env, tmp_path, ["send", addr, "--", "--hi"])
    assert res.returncode == 0, res.stderr
    nid = res.stdout[:-1]
    body = json.loads((inbox / notes.NEW / (nid + ".json")).read_text(encoding="utf-8"))
    assert body["text"] == "--hi"                       # a leading dash is TEXT, not an option


def test_send_f_stdin(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    res = run_cli(env, tmp_path, ["send", addr, "-f", "-"], input="from stdin\n")
    assert res.returncode == 0, res.stderr
    nid = res.stdout[:-1]
    body = json.loads((inbox / notes.NEW / (nid + ".json")).read_text(encoding="utf-8"))
    assert body["text"] == "from stdin\n"               # stored verbatim, newline and all


def test_send_exit2_empty_text(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    res = run_cli(env, tmp_path, ["send", addr, "   \t "])
    assert res.returncode == 2, res.stderr              # empty after strip
    assert res.stdout == ""
    assert os.listdir(str(inbox / notes.NEW)) == []


def test_send_exit3_unknown_run(note_env, tmp_path):
    _, env = note_env                                    # no pointer at all
    res = run_cli(env, tmp_path, ["send", RUN_ID, "hello"])
    assert res.returncode == 3, res.stderr
    assert res.stdout == ""


@pytest.mark.parametrize("end,use_dead_pid", [(123.0, False), (None, True)],
                         ids=["ended", "dead-pid"])
def test_send_exit3_not_running(note_env, tmp_path, end, use_dead_pid):
    cache, env = note_env
    pid = dead_pid() if use_dead_pid else os.getpid()
    addr, inbox = live_agent(cache, end=end, pid=pid)
    res = run_cli(env, tmp_path, ["send", addr, "hello"])
    assert res.returncode == 3, res.stderr
    assert res.stdout == ""
    assert os.listdir(str(inbox / notes.NEW)) == []     # refused before queueing


def test_send_exit3_closed_inbox(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    assert notes.close_inbox(str(inbox), "run_ended") == []   # claims an empty inbox
    res = run_cli(env, tmp_path, ["send", addr, "hello"])
    assert res.returncode == 3, res.stderr
    assert res.stdout == ""


def test_send_exit3_no_inbox(note_env, tmp_path):
    cache, env = note_env
    write_pointer(cache, RUN_ID, kind="agent")          # live pointer, no .inbox anywhere
    res = run_cli(env, tmp_path, ["send", RUN_ID, "hello"])
    assert res.returncode == 3, res.stderr
    assert not (cache / "qwen-agent" / "watch" / ("%s.inbox" % RUN_ID)).exists()


def test_send_exit2_swarm_without_unit(note_env, tmp_path):
    cache, env = note_env
    run = swarm_run(tmp_path)
    write_pointer(cache, RUN_ID, kind="swarm", run_path=run)
    res = run_cli(env, tmp_path, ["send", RUN_ID, "hello"])
    assert res.returncode == 2, res.stderr
    assert res.stdout == ""
    assert "alpha" in res.stderr                        # the refusal names the running unit


def test_send_exit3_unit_states(note_env, tmp_path):
    cache, env = note_env
    run = swarm_run(tmp_path)
    write_pointer(cache, RUN_ID, kind="swarm", run_path=run)
    unit_inbox = run / "inbox" / "alpha"
    notes.init_inbox(str(unit_inbox))                   # the running unit's owner made one

    res = run_cli(env, tmp_path, ["send", RUN_ID + "/alpha", "keep going"])
    assert res.returncode == 0, res.stderr              # running: accepted
    nid = res.stdout[:-1]
    assert notes.NOTE_ID_RE.match(nid), repr(res.stdout)
    assert sorted(os.listdir(str(unit_inbox / notes.NEW))) == [nid + ".json"]

    res = run_cli(env, tmp_path, ["send", RUN_ID + "/beta", "hi"])
    assert res.returncode == 3, res.stderr              # cached is a finished state
    res = run_cli(env, tmp_path, ["send", RUN_ID + "/gamma", "hi"])
    assert res.returncode == 3, res.stderr              # never in the events at all


def test_send_exit2_unit_on_unitless(note_env, tmp_path):
    cache, env = note_env
    live_agent(cache)                                   # kind agent: a single shot
    res = run_cli(env, tmp_path, ["send", RUN_ID + "/u1", "hello"])
    assert res.returncode == 2, res.stderr
    assert res.stdout == ""

    run = tmp_path / "ud"
    run.mkdir()
    write_pointer(cache, RUN_ID, kind="until-done", run_path=run)   # overwrites the pointer
    res = run_cli(env, tmp_path, ["send", RUN_ID + "/u1", "hello"])
    assert res.returncode == 2, res.stderr
    assert res.stdout == ""


def test_wait_delivered_exit0(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    with send_wait(env, tmp_path, addr, 20) as p:
        nid = next_line(p, "the note id")
        assert notes.NOTE_ID_RE.match(nid), nid
        # append the outcome BEFORE the poll (every 0.2 s) looks: no racing, deadline 20 s
        notes.log_note(str(inbox), nid, addr, "main", "delivered", text_len=4)
        line2 = next_line(p, "the outcome")
        assert re.fullmatch(r"delivered at \d{2}:\d{2}:\d{2}", line2), line2
        _out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err


def test_wait_still_queued_exit10(note_env, tmp_path):
    cache, env = note_env
    addr, _ = live_agent(cache)
    with send_wait(env, tmp_path, addr, 1) as p:        # the one deliberate pause: --wait 1
        nid = next_line(p, "the note id")
        assert notes.NOTE_ID_RE.match(nid), nid
        line2 = next_line(p, "the outcome")             # nobody ever delivers it
        assert line2 == "still queued"
        _out, err = p.communicate(timeout=30)
        assert p.returncode == 10, err


def test_wait_undelivered_exit11(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    with send_wait(env, tmp_path, addr, 20) as p:
        nid = next_line(p, "the note id")               # the id exists only now; the log
        notes.log_note(str(inbox), nid, addr, "main",  # line is appended before the next
                       "undelivered", reason="crashed")           # 0.2 s poll, 20 s early
        line2 = next_line(p, "the outcome")
        assert line2 == "undelivered: crashed"
        _out, err = p.communicate(timeout=30)
        assert p.returncode == 11, err


def test_wait_deferred_exit12(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    with send_wait(env, tmp_path, addr, 20) as p:
        nid = next_line(p, "the note id")
        notes.log_note(str(inbox), nid, addr, "main", "deferred")
        line2 = next_line(p, "the outcome")
        assert line2 == "deferred to the next round"
        _out, err = p.communicate(timeout=30)
        assert p.returncode == 12, err


def test_wait_pid_dead_exit11(note_env, tmp_path):
    cache, env = note_env
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    addr, _ = live_agent(cache, pid=target.pid)
    try:
        started = time.monotonic()
        with send_wait(env, tmp_path, addr, 15) as p:
            nid = next_line(p, "the note id")           # queued while the target lived
            assert notes.NOTE_ID_RE.match(nid), nid
            target.kill()                               # the poll re-checks the pid: <=0.2 s
            target.wait(timeout=10)
            line2 = next_line(p, "the outcome")
            assert line2 == "undelivered: pid_dead"
            _out, err = p.communicate(timeout=30)
            assert p.returncode == 11, err
        assert time.monotonic() - started < 10          # long before the 15 s --wait is up
    finally:
        if target.poll() is None:
            target.kill()
            target.wait(timeout=10)


def test_wait_closed_reread_exit11(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    with send_wait(env, tmp_path, addr, 15) as p:
        nid = next_line(p, "the note id")
        assert notes.close_inbox(str(inbox), "run_ended") == [nid]  # claim + sweep line
        line2 = next_line(p, "the outcome")
        assert line2 == "undelivered: run_ended"        # the re-read reports the sweep
        _out, err = p.communicate(timeout=30)
        assert p.returncode == 11, err


def test_wait_closed_no_sweep_exit11(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    with send_wait(env, tmp_path, addr, 15) as p:
        nid = next_line(p, "the note id")
        assert notes.NOTE_ID_RE.match(nid), nid
        (inbox / notes.CLOSED).write_text("", encoding="utf-8")   # claimed, never swept:
        line2 = next_line(p, "the outcome")                        # the note says `queued`
        assert line2 == "undelivered: closed"
        _out, err = p.communicate(timeout=30)
        assert p.returncode == 11, err


def test_notes_rows_and_no_notes(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    res = run_cli(env, tmp_path, ["notes", addr])
    assert res.returncode == 0, res.stderr
    assert res.stdout == "no notes\n"

    id1 = notes.queue_note(str(inbox), addr, "plain queued note")
    id2 = notes.queue_note(str(inbox), addr, "will be delivered")
    long_text = "head line\n" + "\x01secret\x01" + "tail " * 40    # >80 chars, control chars
    id3 = notes.queue_note(str(inbox), addr, long_text)
    notes.log_note(str(inbox), id2, addr, "main", "delivered")
    os.replace(str(inbox / notes.NEW / (id2 + ".json")),
               str(inbox / notes.DELIVERED / (id2 + ".json")))
    notes.log_note(str(inbox), id3, addr, "main", "undelivered", reason="cap")
    os.replace(str(inbox / notes.NEW / (id3 + ".json")),
               str(inbox / notes.UNDELIVERED / (id3 + ".json")))

    res = run_cli(env, tmp_path, ["notes", addr])
    assert res.returncode == 0, res.stderr
    rows = res.stdout.rstrip("\n").split("\n")
    assert len(rows) == 3
    pat = re.compile(r"^(\d{2}:\d{2}:\d{2})  (queued|delivered|undelivered: cap)"
                     r"  (\d+-[0-9a-f]{6})  (.*)$")    # two spaces between every field
    parsed = []
    for row in rows:
        m = pat.match(row)
        assert m, row
        parsed.append(m.groups())
    assert [g[2] for g in parsed] == [id1, id2, id3]   # rows in id (time) order
    assert [g[1] for g in parsed] == ["queued", "delivered", "undelivered: cap"]
    expected = "".join(c if " " <= c != "\x7f" else " " for c in long_text[:80])
    assert parsed[2][3] == expected                     # first 80 chars, controls as spaces
    assert len(expected) == 80
    for row in rows:
        assert not [c for c in row if ord(c) < 0x20]    # no control char ever reaches a row


def test_notes_rows_exit0_after_run_ended(note_env, tmp_path):
    cache, env = note_env
    addr, _ = live_agent(cache, end=123.0)              # the run ended, the pointer stands
    res = run_cli(env, tmp_path, ["notes", addr])
    assert res.returncode == 0, res.stderr
    assert res.stdout == "no notes\n"


def test_notes_rows_exit3_unknown_run(note_env, tmp_path):
    _, env = note_env                                    # no pointer: P8 same rule as send
    res = run_cli(env, tmp_path, ["notes", RUN_ID])
    assert res.returncode == 3, res.stderr
    assert res.stdout == ""


def test_notes_rows_exit2_swarm_without_unit(note_env, tmp_path):
    cache, env = note_env
    run = swarm_run(tmp_path)
    write_pointer(cache, RUN_ID, kind="swarm", run_path=run)
    res = run_cli(env, tmp_path, ["notes", RUN_ID])
    assert res.returncode == 2, res.stderr
    assert res.stdout == ""


def test_notes_rows_exit3_unknown_unit(note_env, tmp_path):
    cache, env = note_env
    run = swarm_run(tmp_path)
    write_pointer(cache, RUN_ID, kind="swarm", run_path=run)
    res = run_cli(env, tmp_path, ["notes", RUN_ID + "/gamma"])
    assert res.returncode == 3, res.stderr
    assert res.stdout == ""


def test_send_cli_matches_api(note_env, tmp_path):
    cache, env = note_env
    addr, inbox = live_agent(cache)
    res = run_cli(env, tmp_path, ["send", addr, "via the cli"])
    assert res.returncode == 0, res.stderr
    nid = res.stdout[:-1]
    states, order = notes.latest_states(str(inbox))     # the CLI's queue is the lib's log
    assert order == [nid]
    assert states[nid]["state"] == "queued" and states[nid]["to"] == ADDR
    nid2 = notes.queue_note(str(inbox), addr, "via the api")
    res = run_cli(env, tmp_path, ["notes", addr])       # and both rows read back through it
    assert res.returncode == 0, res.stderr
    rows = res.stdout.rstrip("\n").split("\n")
    assert len(rows) == 2 and nid in rows[0] and nid2 in rows[1]
