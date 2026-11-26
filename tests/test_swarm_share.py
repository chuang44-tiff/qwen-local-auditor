"""The swarm share wiring (spec 11.4/12.5/12.6/12.7): lib/swarm.py driving
lib/share_relay.RunRelay, api.fan_out(share=True) and the runner's relay
lifecycle.

Every test builds a run folder under tmp_path (XDG_CACHE_HOME too, so the
watch registry is never touched), drives a real Swarm with
tests/fake_swarm_agent.py, and sets QWEN_RELAY_POLL=0.1 so the relay thread
turns over fast. A fake unit "shares" the way the qla_msg share tool does --
msgdir.write_out(QWEN_MSG_DIR, "share", {...}) -- and a fake receiver
"delivers" a note the way the stream driver does: it moves the file from its
inbox new/ to delivered/ and names the ids in
$FAKE_SWARM_DIR/<role>.note_ids.json (the fake's documented witness, which
turns the printed record into the streamed shape lib/swarm.py reads). Gates
poll up to 20 s; no test binds on wall clock below 10 s.
"""
import hashlib
import json
import os
import pathlib
import re
import sys
import time

import pytest

from lib import msgdir, notes, share_relay, swarm
from lib.swarm_engine import api, events, manifest, runner
from swarm_fixtures import FAKE, agent_args, make_workflow

SKILL = pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor"
RUN_ID = "ab12cd34ef56"
SENTINEL = "SENTINEL-7f3a"


@pytest.fixture(autouse=True)
def _share_env(monkeypatch):
    """A developer's own relay/cap/stream switches never decide a test."""
    for name in ("QWEN_SHARE_MAX", "QWEN_SHARE_IN_MAX", "QWEN_RELAY_POLL",
                 "QWEN_ASK_MAIN_MAX", "QWEN_STREAM", "QWEN_ASK_LEASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWEN_RELAY_POLL", "0.1")


# ----------------------------------------------------------------- the rig
def relayed_swarm(tmp_path, monkeypatch, seats=4, run=None, asks_ready=False,
                  emit=None, start=True):
    """A real Swarm on the fake agent wired the way the runner wires it:
    sw.relay/sw.run_id set and the relay's thread started. start=False leaves
    only the calls the units themselves drive -- register, retry, unit_done's
    last pass and run_phase's end_phase -- polling the outboxes."""
    if run is None:
        run = tmp_path / "run"
    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_SWARM_DIR", str(fake))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    monkeypatch.setenv("QWEN_SHARE_TEST_LIB", str(SKILL))
    monkeypatch.setenv("QWEN_SHARE_TEST_RUN", str(run))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("QWEN_WATCH_CHILD", "QWEN_WATCH_ID", "QWEN_WATCH_PARENT"):
        monkeypatch.setenv(name, "0")
    sw = swarm.Swarm([sys.executable, str(FAKE)], run, seats=seats,
                     timeout=30, backoff=0)
    relay = share_relay.RunRelay(str(run), RUN_ID, asks_ready, emit=emit)
    if start:
        relay.start()
    sw.relay, sw.run_id = relay, RUN_ID
    return sw, pathlib.Path(run), relay, fake


def roles_dir(tmp_path):
    d = tmp_path / "roles"
    d.mkdir(exist_ok=True)
    return d


def make_unit(roles, name, role, prompt="do the work", **kw):
    rf = roles / ("%s.md" % role)
    if not rf.exists():
        rf.write_text("role %s\n" % role, encoding="utf-8")
    base = dict(toolset="none", grants="", web=False, mcp_config=None,
                parse=swarm.extract_json)
    base.update(kw)                      # a caller may name any Unit field itself
    return swarm.Unit(name=name, role_file=str(rf), prompt=prompt, **base)


def opted(u, pid="p1"):
    u.share_phase = pid
    return u


# the prelude every behaviour file runs with: the same modules the tools use,
# plus gates that fail the unit (loudly) instead of hanging the suite
PRELUDE = '''
import json, os, sys, time
sys.path.insert(0, os.environ["QWEN_SHARE_TEST_LIB"])
from lib import msgdir, notes

def _wait(pred, why, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return
        time.sleep(0.05)
    sys.stderr.write("fake gate timeout: %s\\n" % why)
    sys.exit(9)

def _note_bodies(inbox):
    out = []
    for sub in ("new", "delivered", "undelivered"):
        d = os.path.join(inbox, sub)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, n), "rb") as fh:
                    body = json.loads(fh.read().decode("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(body, dict):
                out.append(body)
    return out

def _shares(inbox):
    return [b for b in _note_bodies(inbox) if b.get("kind") == "share"]

def _pending_shares(inbox):
    return [b for b in _shares(inbox)
            if os.path.exists(os.path.join(inbox, "new", str(b.get("id")) + ".json"))]

def _deliver_all(inbox):
    # the stream driver's own move: every pending note new/ -> delivered/
    moved = []
    for path, nid in notes.list_new(inbox):
        dst = os.path.join(inbox, "delivered")
        os.makedirs(dst, exist_ok=True)
        os.replace(path, os.path.join(dst, "%s.json" % nid))
        moved.append(nid)
    return moved

def _relay_lines():
    try:
        with open(os.path.join(os.environ["QWEN_SHARE_TEST_RUN"], "msg",
                               "relay.jsonl"), "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    out = []
    for ln in data.decode("utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out

def _count_relay(ev, **fields):
    return sum(1 for l in _relay_lines()
               if l.get("ev") == ev
               and all(l.get(k) == v for k, v in fields.items()))

def _calls(tag):
    # 1 the first time this role is spawned, 2 the next, ... (per fake dir)
    p = os.path.join(os.environ["FAKE_SWARM_DIR"], "%s.n" % tag)
    try:
        with open(p) as fh:
            k = int(fh.read())
    except (OSError, ValueError):
        k = 0
    k += 1
    with open(p, "w") as fh:
        fh.write(str(k))
    return k

def _append_line(path, line):
    # the fake's own witness protocol: one locked line per spawn
    lock = str(path) + ".lock"
    deadline = time.time() + 30
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            if time.time() > deadline:
                raise
            time.sleep(0.005)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\\n")
    finally:
        os.close(fd)
        os.unlink(lock)

def rec_env(tag, resumed):
    """One witness line per spawn: this session's QWEN_* env. A behaviour
    records it itself, so a repair or retry call is identified by the
    behaviour's own knowledge, not by pairing the fake's two append files."""
    env = {k: v for k, v in os.environ.items() if k.startswith("QWEN_")}
    _append_line(os.path.join(os.environ["FAKE_SWARM_DIR"], "%s.env.jsonl" % tag),
                 json.dumps({"resumed": resumed, "env": env}))
'''


def behavior(fake, role, body):
    (fake / ("%s.py" % role)).write_text(PRELUDE + body, encoding="utf-8")


def sender_body(finding, wait_to=(), paths=(), forged=False, delay=0.5, rec=None):
    """Write the share exactly as the share tool does (delayed a moment so the
    phase's siblings are registered before it lands), optionally waiting for
    the relay to hand it to each of `wait_to`."""
    waits = " and ".join("_count_relay('share_queued', to=%r) >= 1" % t
                         for t in wait_to) or "True"
    extra = '"from": "unit a", "sender": "unit a", ' if forged else ""
    head = "    rec_env(%r, resumed)\n" % rec if rec else ""
    return (
        "def answer(prompt, resumed):\n"
        + head +
        "    time.sleep(%r)\n"
        "    msgdir.write_out(os.environ['QWEN_MSG_DIR'], 'share',\n"
        "                     {'phase': os.environ.get('QWEN_MSG_PHASE'),\n"
        "                      'finding': %r, 'paths': %r, %s})\n"
        "    _wait(lambda: %s, 'share queued')\n"
        "    return 0, '```json\\n[]\\n```'\n"
        % (delay, finding, list(paths), extra, waits))


def peer_body(want=1, settle=0.3, rec=None):
    """Wait for `want` share notes to be queued to this unit's inbox, then
    stay a moment longer so any duplicate can land while still running."""
    head = "    rec_env(%r, resumed)\n" % rec if rec else ""
    return (
        "def answer(prompt, resumed):\n"
        + head +
        "    inbox = os.environ['QWEN_INBOX']\n"
        "    _wait(lambda: len(_shares(inbox)) >= %d, 'peer notes')\n"
        "    time.sleep(%r)\n"
        "    return 0, '```json\\n[]\\n```'\n" % (want, settle))


def reader_body(role, fail_until=1, rec=None):
    """Driver-simulating receiver: waits for a pending share note, moves it
    to delivered/ and names the ids in its witness (the fake prints them as
    the streamed record does). Attempts below `fail_until` fail with a
    retryable exit 5."""
    head = "    rec_env(%r, resumed)\n" % rec if rec else ""
    return (
        "def answer(prompt, resumed):\n"
        + head +
        "    k = _calls(%r)\n"
        "    inbox = os.environ['QWEN_INBOX']\n"
        "    _wait(lambda: len(_pending_shares(inbox)) >= 1, 'attempt note')\n"
        "    ids = _deliver_all(inbox)\n"
        "    if k < %d:\n"
        "        return 5, ''\n"
        "    with open(os.path.join(os.environ['FAKE_SWARM_DIR'],\n"
        "                           %r + '.note_ids.json'), 'w') as fh:\n"
        "        json.dump(ids, fh)\n"
        "    return 0, '```json\\n[]\\n```'\n" % (role, fail_until, role))


# ----------------------------------------------------------------- readers
def _tail(path, n=600):
    """The last n chars of a log file, for assert messages ('' when absent)."""
    try:
        return pathlib.Path(path).read_text(encoding="utf-8",
                                            errors="replace")[-n:]
    except OSError:
        return ""


def recorded_envs(fake, tag):
    """[{resumed, env}] -- one line per spawn of a behaviour that called
    rec_env(tag, ...): what THAT call was spawned with, in call order."""
    try:
        with open(fake / ("%s.env.jsonl" % tag), encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        return []


def relay_lines(run):
    try:
        with open(run / "msg" / "relay.jsonl", "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    return [json.loads(ln) for ln in data.decode("utf-8").splitlines() if ln.strip()]


def share_notes(run, unit):
    """The share notes ever queued to a unit's inbox: pending, delivered or
    swept -- the bodies exactly as the relay wrote them."""
    out = []
    for sub in ("new", "delivered", "undelivered"):
        d = run / "inbox" / unit / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            try:
                body = json.loads(p.read_bytes().decode("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(body, dict) and body.get("kind") == "share":
                out.append(body)
    return out


# ----------------------------------------------------------------- delivery
def test_share_reaches_running_siblings_with_stamped_sender(tmp_path, monkeypatch):
    finding = "the token cache skips expiry on unicode keys, proven by test 7"
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=3)
    behavior(fake, "sharer", sender_body(finding, wait_to=("b", "c")))
    behavior(fake, "peer", peer_body(rec="peer"))
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "a", "sharer")),
                            opted(make_unit(roles, "b", "peer")),
                            opted(make_unit(roles, "c", "peer"))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    for sibling in ("b", "c"):
        nb = share_notes(run, sibling)
        assert len(nb) == 1, sibling
        assert nb[0]["from"] == "unit a"              # stamped from the DIRECTORY
        assert nb[0]["kind"] == "share" and nb[0]["text"] == finding
        assert nb[0]["role"] == "sharer"
    assert share_notes(run, "a") == []                # never to the sender itself
    envs = {e["env"]["QWEN_MSG_SELF"]: e["env"] for e in recorded_envs(fake, "peer")}
    assert envs["%s/b" % RUN_ID]["QWEN_MSG_DIR"] == msgdir.unit_msgdir(str(run), "b")
    assert envs["%s/b" % RUN_ID]["QWEN_MSG_SELF"] == "%s/b" % RUN_ID
    assert envs["%s/c" % RUN_ID]["QWEN_MSG_PHASE"] == "p1"


def test_late_starter_gets_backlog_once(tmp_path, monkeypatch):
    finding = "the shared fixture resets the database between units"
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=1)
    behavior(fake, "sharer", sender_body(finding, delay=0.2))   # b starts later
    behavior(fake, "peer", peer_body())
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "a", "sharer")),
                            opted(make_unit(roles, "b", "peer"))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    nb = share_notes(run, "b")
    assert len(nb) == 1                                    # the backlog, once
    assert nb[0]["from"] == "unit a" and nb[0]["text"] == finding
    assert len(msgdir.findings_read(str(run), "p1")) == 1
    assert len([l for l in relay_lines(run)
                if l.get("ev") == "share_queued" and l.get("to") == "b"]) == 1


def test_sender_repair_and_cache_hits_get_nothing(tmp_path, monkeypatch):
    finding = "the parser dies on a trailing comma, with evidence"
    # the repair call carries no messaging env at all
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=2,
                                         asks_ready=True)
    behavior(fake, "stumbler", (
        "def answer(prompt, resumed):\n"
        "    rec_env('rep', resumed)\n"
        "    if not resumed:\n"
        "        time.sleep(0.3)\n"
        "        msgdir.write_out(os.environ['QWEN_MSG_DIR'], 'share',\n"
        "                         {'phase': os.environ.get('QWEN_MSG_PHASE'),\n"
        "                          'finding': %r, 'paths': []})\n"
        "        return 0, 'no json block anywhere'\n"
        "    return 0, '```json\\n[]\\n```'\n" % finding))
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "rep", "stumbler"))])
    assert results[0]["ok"], (results[0]["name"], results[0]["why"])
    envs = [e["env"] for e in recorded_envs(fake, "rep")]
    assert len(envs) == 2                                  # first call + repair
    assert [e["resumed"] for e in recorded_envs(fake, "rep")] == [False, True]
    assert "QWEN_MSG_DIR" in envs[0] and "QWEN_INBOX" in envs[0]
    assert envs[0]["QWEN_MSG_TOOLS"] == "reply,ask_main,share"
    assert "QWEN_ASKS_DIR" in envs[0]
    assert "QWEN_INBOX" not in envs[1] and "QWEN_ASKS_DIR" not in envs[1]
    assert not [k for k in envs[1] if k.startswith("QWEN_MSG_")]
    # a cached unit of the phase never reaches the relay: it is never
    # registered, receives nothing, and gets no msg dir at all
    run2 = tmp_path / "run2"
    sw1 = swarm.Swarm([sys.executable, str(FAKE)], run2, seats=2,
                      timeout=30, backoff=0)               # relay-less: caches
    assert sw1.run_phase([make_unit(roles, "old", "quiet")])[0]["ok"]
    msgdir.findings_append(str(run2), "p1", {
        "id": notes.new_note_id(), "sender": "early", "role": "sharer",
        "t": round(time.time(), 3), "text": finding, "paths": []})
    sw2, _run2, _relay2, _fake2 = relayed_swarm(tmp_path, monkeypatch, seats=2,
                                                run=run2)
    res = sw2.run_phase([opted(make_unit(roles, "old", "quiet"))])
    assert res[0]["ok"] and res[0]["cached"]
    assert share_notes(run2, "old") == []
    assert not (run2 / "msg" / "units" / "old").exists()
    assert [l for l in relay_lines(run2) if l.get("to") == "old"] == []


def test_receiver_budget_and_drop_log(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_SHARE_IN_MAX", "1")             # one note per attempt
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=4)
    behavior(fake, "send1", sender_body("first shared finding, with evidence"))
    behavior(fake, "send2", sender_body("second shared finding, with evidence"))
    behavior(fake, "budgeted", (
        "def answer(prompt, resumed):\n"
        "    inbox = os.environ['QWEN_INBOX']\n"
        "    _wait(lambda: len(_shares(inbox)) >= 1\n"
        "              and _count_relay('share_dropped', reason='receiver_cap',"
        " to='r') >= 1, 'the cap')\n"
        "    time.sleep(0.3)\n"
        "    return 0, '```json\\n[]\\n```'\n"))
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "s1", "send1")),
                            opted(make_unit(roles, "s2", "send2")),
                            opted(make_unit(roles, "r", "budgeted"))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    assert len(share_notes(run, "r")) == 1                   # the budget held
    caps = [l for l in relay_lines(run) if l.get("ev") == "share_dropped"
            and l.get("reason") == "receiver_cap"]
    assert len(caps) == 1 and caps[0]["to"] == "r"           # the drop is logged
    assert len(msgdir.findings_read(str(run), "p1")) == 2    # both findings exist


def test_retry_requeue_no_duplicates(tmp_path, monkeypatch):
    finding = "the flaky port test hides a real race, proven twice"
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=3)
    behavior(fake, "repeater", sender_body(finding, wait_to=("r",)))
    behavior(fake, "flaky", reader_body("flaky", fail_until=2, rec="flaky"))
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "s", "repeater")),
                            opted(make_unit(roles, "r", "flaky", retries=1))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    assert len(recorded_envs(fake, "flaky")) == 2             # failed, then succeeded
    got = share_notes(run, "r")
    assert len(got) == 2                                      # once per attempt
    assert len({b["id"] for b in got}) == 2                   # under distinct ids
    queued = [l for l in relay_lines(run) if l.get("ev") == "share_queued"
              and l.get("to") == "r"]
    assert [l.get("attempt") for l in queued] == ["a1", "a2"]
    new_dir = run / "inbox" / "r" / "new"
    assert not new_dir.is_dir() or list(new_dir.iterdir()) == []


def test_receiver_not_cached_sender_cached(tmp_path, monkeypatch):
    finding = "the retry loop double-frees, caught with the sanitizer"
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=3)
    behavior(fake, "offering", sender_body(finding, wait_to=("r",)))
    behavior(fake, "hearing", reader_body("hearing"))
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "s", "offering")),
                            opted(make_unit(roles, "r", "hearing"))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    assert len(share_notes(run, "r")) == 1
    assert (run / "agents" / "s.json").is_file()          # heard nothing: caches
    assert not (run / "agents" / "r.json").exists()       # heard a share: never
    assert share_notes(run, "s") == []


def test_resume_replays_findings(tmp_path, monkeypatch):
    finding = "the config loader ignores XDG on Windows, shown by the trace"
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=3)
    behavior(fake, "offering", sender_body(finding, wait_to=("r",)))
    behavior(fake, "hearing", reader_body("hearing"))
    roles = roles_dir(tmp_path)
    defs = lambda: [opted(make_unit(roles, "s", "offering")),     # noqa: E731
                    opted(make_unit(roles, "r", "hearing"))]
    res0 = sw.run_phase(defs())
    assert all(r["ok"] for r in res0), [(r["name"], r["why"]) for r in res0]
    relay.close()                                    # the run ends, as --resume sees it
    # a second Swarm + relay on the same folder is what a resumed run builds
    sw2, run, relay2, _fake2 = relayed_swarm(tmp_path, monkeypatch, seats=3, run=run)
    res = sw2.run_phase(defs())
    by = {r["name"]: r for r in res}
    assert by["s"]["ok"] and by["s"]["cached"]       # shared, heard nothing: cache
    assert by["r"]["ok"] and not by["r"]["cached"]   # heard a share: reruns
    got = share_notes(run, "r")
    assert len(got) == 2 and len({b["id"] for b in got}) == 2
    queued = [l for l in relay_lines(run) if l.get("ev") == "share_queued"
              and l.get("to") == "r"]
    assert len(queued) == 2                          # one backlog note this run
    assert len(msgdir.findings_read(str(run), "p1")) == 1   # and nothing re-admitted


def test_phase_end_drains_leftovers(tmp_path, monkeypatch):
    late = "planted as the sender exited, before the phase ended"
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=2, start=False)
    behavior(fake, "quick", sender_body(late, delay=0.5))   # writes, then returns now
    behavior(fake, "peer", peer_body(settle=0.1))
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "a", "quick")),
                            opted(make_unit(roles, "b", "peer"))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    # no relay thread ever ran: unit_done's last pass admitted what the sender
    # wrote on its way out, and queued it to the still-running sibling
    assert len(msgdir.findings_read(str(run), "p1")) == 1
    assert len([l for l in relay_lines(run) if l.get("ev") == "share_admitted"
                and l.get("unit") == "a"]) == 1
    assert len(share_notes(run, "b")) == 1
    # a share planted after the phase ended is share_dropped: phase_ended
    msg_dir = msgdir.unit_msgdir(str(run), "a")
    assert msgdir.write_out(msg_dir, "share",
                            {"phase": "p1", "finding": "a share after the end",
                             "paths": []})
    relay.register("a", "quick", str(run / "inbox" / "a"), pid="p1")
    relay.poll_once()
    ended = [l for l in relay_lines(run) if l.get("ev") == "share_dropped"
             and l.get("reason") == "phase_ended"]
    assert len(ended) == 1 and ended[0]["unit"] == "a"
    assert len(msgdir.findings_read(str(run), "p1")) == 1   # nothing more admitted


def test_forged_out_file_in_other_dir_stamped_as_dir_owner(tmp_path, monkeypatch):
    finding = "the ledger double-counts the first spawn"
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=3)
    behavior(fake, "forger", sender_body(finding, wait_to=("a", "c"), forged=True))
    behavior(fake, "peer", peer_body())
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "a", "peer")),
                            opted(make_unit(roles, "b", "forger")),
                            opted(make_unit(roles, "c", "peer"))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    for sibling in ("a", "c"):
        nb = share_notes(run, sibling)
        assert len(nb) == 1, sibling
        assert nb[0]["from"] == "unit b"            # the DIRECTORY wins the stamp
        assert nb[0]["text"] == finding             # the file's own from is junk
    assert msgdir.findings_read(str(run), "p1")[0]["sender"] == "b"


def test_sender_cap_bypass_files_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_SHARE_MAX", "1")                # one finding per sender
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=1)
    behavior(fake, "bulk", (
        "def answer(prompt, resumed):\n"
        "    time.sleep(0.3)\n"
        "    for text in ('first finding with evidence one',\n"
        "                 'second finding with evidence two',\n"
        "                 'third finding with evidence three'):\n"
        "        msgdir.write_out(os.environ['QWEN_MSG_DIR'], 'share',\n"
        "                         {'phase': os.environ.get('QWEN_MSG_PHASE'),\n"
        "                          'finding': text, 'paths': []})\n"
        "    _wait(lambda: _count_relay('share_admitted', unit='a') >= 1\n"
        "              and _count_relay('share_dropped', unit='a',\n"
        "                               reason='sender_cap') >= 2, 'the cap')\n"
        "    return 0, '```json\\n[]\\n```'\n"))
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "a", "bulk"))])
    assert results[0]["ok"], (results[0]["name"], results[0]["why"])
    assert len(msgdir.findings_read(str(run), "p1")) == 1   # three files, one finding
    dropped = [l for l in relay_lines(run) if l.get("ev") == "share_dropped"
               and l.get("reason") == "sender_cap"]
    assert len(dropped) == 2 and all(l["unit"] == "a" for l in dropped)


# ----------------------------------------------------------------- fences/key
def _fence_workflow(tmp_path, monkeypatch):
    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_SWARM_DIR", str(fake))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    monkeypatch.setenv("QWEN_SHARE_TEST_LIB", str(SKILL))
    monkeypatch.setenv("QWEN_SHARE_TEST_RUN", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("QWEN_WATCH_CHILD", "QWEN_WATCH_ID", "QWEN_WATCH_PARENT"):
        monkeypatch.setenv(name, "0")
    folder = make_workflow(tmp_path / "wfs", {"target": "required", "roles": {
        "w": {"file": "roles/w.md", "fence": "web"},
        "searchy": {"file": "roles/searchy.md", "fence": "search"},
        "br": {"file": "roles/br.md", "fence": "browser"},
        "probe": {"file": "roles/probe.md", "fence": "browser-probe"},
        "rd": {"file": "roles/rd.md", "fence": "read"}}},
        roles=("w", "searchy", "br", "probe", "rd"))
    m = manifest.load(folder)
    cfg = {"goal": "g", "depth": "quick", "items": 1, "max_agents": 3,
           "max_items": 2, "timeout_per_item": 100, "retries": 0, "effort": None,
           "role_effort": {}, "hours": None, "deadline": None, "rounds": 1,
           "target": None, "claude_confirm": "opus"}
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    (run / "mcp.json").write_text("{}", encoding="utf-8")
    sw = swarm.Swarm([sys.executable, str(FAKE)], run, seats=3, timeout=30, backoff=0)
    wf = api.Workflow(m, cfg, run, sw, goal="g", mcp=run / "mcp.json")
    return wf, sw, run, fake


def test_web_and_browser_fences_have_no_ask_main(tmp_path, monkeypatch):
    wf, sw, run, fake = _fence_workflow(tmp_path, monkeypatch)
    plain = lambda r: wf._unit("u-" + r, r, "p", 300, swarm.extract_json, None)  # noqa: E731
    assert plain("w").ask_main is False               # the web fence asks nobody
    assert plain("searchy").ask_main is False         # search is a web fence too
    assert plain("br").ask_main is False              # and so is the browser
    assert plain("probe").ask_main is False
    assert plain("rd").ask_main is True               # a read unit may ask
    # a streamed run of such units: QWEN_MSG_TOOLS without ask_main for them
    for r in ("w", "br", "rd"):
        behavior(fake, r, "def answer(prompt, resumed):\n"
                          "    rec_env(%r, resumed)\n"
                          "    return 0, '```json\\n[]\\n```'\n" % r)
    relay = share_relay.RunRelay(str(run), RUN_ID, True)
    relay.start()
    sw.relay, sw.run_id = relay, RUN_ID
    try:
        units = [wf._unit(n, r, "p", 300, swarm.extract_json, None)
                 for n, r in (("u-w", "w"), ("u-br", "br"), ("u-rd", "rd"))]
        res_ph = sw.run_phase(units)
        assert all(r["ok"] for r in res_ph), [(r["name"], r["why"]) for r in res_ph]
        env_w = recorded_envs(fake, "w")[0]["env"]
        env_br = recorded_envs(fake, "br")[0]["env"]
        env_rd = recorded_envs(fake, "rd")[0]["env"]
        assert env_w["QWEN_MSG_TOOLS"] == "reply"
        assert "ask_main" not in env_w["QWEN_MSG_TOOLS"]
        assert env_br["QWEN_MSG_TOOLS"] == "reply"
        assert env_rd["QWEN_MSG_TOOLS"] == "reply,ask_main"
        assert env_rd["QWEN_ASKS_DIR"] == str(run / "asks")
        assert "QWEN_ASKS_DIR" not in env_w
        assert "QWEN_MSG_SELF" in env_w and "QWEN_MSG_SELF" in env_br
    finally:
        relay.close()


def _key_26f995f(u):
    """The released cache-key formula, transcribed into this test (never
    imported from lib.swarm): the very blob 26f995f hashed."""
    role_text = pathlib.Path(u.role_file).read_text(encoding="utf-8")
    mcp_text = ("" if u.mcp_config is None
                else pathlib.Path(u.mcp_config).read_text(encoding="utf-8"))
    blob = "%s\n%s\n%s\n%s\n%s\n%s\n%s" % (role_text, u.prompt, u.toolset,
                                           u.grants, u.web, mcp_text,
                                           u.effort or "")
    if u.key_extra:
        blob += "\n" + u.key_extra
    if u.browser:
        blob += "\n--browser"
    if u.browser_eval:
        blob += "\n--browser-eval"
    if u.claude and any(v != "off" for v in u.claude.values()):
        blob += "\n--claude " + " ".join("%s=%s" % kv
                                         for kv in sorted(u.claude.items()))
    if u.deep:
        blob += "\ndeep:" + ",".join(u.deep)
    return hashlib.sha256(blob.encode("utf-8", "surrogateescape")).hexdigest()


class _StubSw:
    """Stands in for the Swarm: captures the units fan_out built, runs none."""

    def __init__(self, agents_dir):
        self.agents_dir = pathlib.Path(agents_dir)
        self.seats = 4
        self.captured = []

    def run_phase(self, units, seats=None, label=None):
        self.captured = list(units)
        return [{"name": u.name, "ok": True, "data": [], "why": "",
                 "tokens": 0, "seconds": 0.0, "cached": False, "deadline": False}
                for u in units]


def _stub_workflow(tmp_path, tag):
    folder = make_workflow(tmp_path / ("wfs-" + tag))
    m = manifest.load(folder)
    cfg = {"goal": "g", "depth": "quick", "items": 2, "max_agents": 2,
           "max_items": 2, "timeout_per_item": 100, "retries": 0, "effort": None,
           "role_effort": {}, "hours": None, "deadline": None, "rounds": 1,
           "target": None, "claude_confirm": "opus"}
    run = tmp_path / ("run-" + tag)
    run.mkdir(exist_ok=True)
    stub = _StubSw(run / "agents")
    return api.Workflow(m, cfg, run, stub, goal="g"), stub


def _wave_prompt(batch):
    return "items:\n" + "\n".join("- %s:" % it["id"] for it in batch)


def _wave_parse(text, batch):
    return [{"id": it["id"]} for it in batch]


def test_non_opted_unit_key_identical(tmp_path, monkeypatch):
    sw = swarm.Swarm([sys.executable, str(FAKE)], tmp_path / "k", seats=2,
                     timeout=30, backoff=0)
    roles = roles_dir(tmp_path)
    u = make_unit(roles, "fixed", "worker", prompt="a PROMPT with bytes",
                  toolset="Read,Glob,Grep", grants="Read", effort="high")
    assert sw._key(u) == _key_26f995f(u)
    u.share_phase, u.ask_main = "p9", False      # neither rides the key
    assert sw._key(u) == _key_26f995f(u)
    wf, stub = _stub_workflow(tmp_path, "a")
    items = [{"id": "I1"}]
    wf.fan_out("plain", "worker", items, _wave_prompt, _wave_parse)
    (plain,) = stub.captured
    assert plain.share_phase is None and plain.grants == ""
    assert plain.prompt == _wave_prompt(items)
    assert sw._key(plain) == _key_26f995f(plain)      # non-opted: the released key
    wf.fan_out("opt", "worker", items, _wave_prompt, _wave_parse, share=True)
    (one,) = stub.captured
    assert one.share_phase == "p1"                    # first opted call
    assert one.grants == api.SHARE_GRANT              # only the grant appended...
    assert one.prompt == plain.prompt + "\n\n" + api.SHARE_RULE
    # ...and only the rule and the grant differ
    for attr in ("toolset", "web", "browser", "browser_eval", "key_extra",
                 "deep", "effort", "mcp_config"):
        assert getattr(one, attr) == getattr(plain, attr)
    assert _key_26f995f(one) == sw._key(one)          # same formula, two appends
    assert sw._key(one) != sw._key(plain)
    wf.fan_out("opt2", "worker", items, _wave_prompt, _wave_parse, share=True)
    assert stub.captured[0].share_phase == "p2"       # next opted call, next id
    wf.fan_out("off", "worker", items, _wave_prompt, _wave_parse)
    assert stub.captured[0].share_phase is None       # a non-opted call spends none
    # a fence that already grants tools gets the grant APPENDED, not replaced
    wf2, stub2 = _stub_workflow(tmp_path, "b")
    gdir = tmp_path / "gr"
    gdir.mkdir(exist_ok=True)
    (gdir / "g.md").write_text("role g", encoding="utf-8")
    wf2.manifest.roles["grantee"] = manifest.Role("grantee", str(gdir / "g.md"),
                                                  "read", 1, None)
    wf2.fan_out("g", "grantee", items, _wave_prompt, _wave_parse, share=True)
    assert stub2.captured[0].grants == "Read,Glob,Grep," + api.SHARE_GRANT


# ----------------------------------------------------------------- events
def test_share_event_has_no_text(tmp_path, monkeypatch):
    finding = "the queue starves when the poller holds the lock across IO"
    run = tmp_path / "run"
    sw, run, relay, fake = relayed_swarm(
        tmp_path, monkeypatch, seats=2, run=run,
        emit=lambda kind, **fields: events.emit(run, kind, **fields))
    behavior(fake, "offering", sender_body(finding, wait_to=("r",)))
    behavior(fake, "hearing", peer_body())
    roles = roles_dir(tmp_path)
    results = sw.run_phase([opted(make_unit(roles, "s", "offering")),
                            opted(make_unit(roles, "r", "hearing"))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    raw = (run / "events.jsonl").read_text(encoding="utf-8")
    share_evs = [json.loads(ln) for ln in raw.splitlines() if ln.strip()
                 and json.loads(ln).get("kind") == "share"]
    assert len(share_evs) == 1
    assert set(share_evs[0]) == {"kind", "t", "unit", "role", "share_id",
                                 "to_count", "len"}          # exactly these keys
    assert share_evs[0]["unit"] == "s" and share_evs[0]["role"] == "offering"
    assert share_evs[0]["to_count"] == 1 and share_evs[0]["len"] == len(finding)
    assert finding not in raw                                # no text, ever
    assert "share" in events.ENGINE_KINDS
    wf, _stub = _stub_workflow(tmp_path, "ev")
    with pytest.raises(ValueError):
        wf.event("share", unit="s")                          # not a workflow kind


# ----------------------------------------------------------------- report
SHARE_SCRIPT = '''
def run(wf):
    items = [{"id": "I1"}, {"id": "I2"}]
    res = wf.fan_out("work", "worker", items,
                     lambda batch: "items:\\n" + "\\n".join(
                         "- %s:" % it["id"] for it in batch),
                     lambda text, batch: [{"id": it["id"]} for it in batch],
                     share=True)
    wf.report("# Echo\\n\\n%d rows\\n" % len(res.rows))
'''


RUNNER_WORKER_T = '''
def _asks_logged():
    try:
        with open(os.path.join(os.environ["QWEN_SHARE_TEST_RUN"], "asks",
                               "log.jsonl"), "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    for ln in data.decode("utf-8").splitlines():
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("ev") in ("question_admitted",
                                                       "question_dropped"):
            return True
    return False

def answer_cwd(prompt, resumed, cwd):
    with open(os.path.join(cwd, "transcript.jsonl"), "a") as fh:
        fh.write(json.dumps({"type": "assistant",
                             "thinking": "mulling __THOUGHT__ now"}) + "\\n")
    time.sleep(0.3)
    if "- I1:" in prompt:
__SHARE__        _wait(lambda: _count_relay("share_queued", to="work-2") >= 1,
                      "to work-2")
        return 0, '```json\\n{"note": "__THOUGHT__"}\\n```'
    _wait(lambda: len(_pending_shares(os.environ["QWEN_INBOX"])) >= 1, 'my note')
    time.sleep(0.3)
    return 0, '```json\\n[]\\n```'
'''

SHARE_LINES = (
    "        msgdir.write_out(os.environ['QWEN_MSG_DIR'], 'share',\n"
    "                         {'phase': os.environ.get('QWEN_MSG_PHASE'),\n"
    "                          'finding': __FINDING__, 'paths': ['src/cache.py']})\n"
    "        msgdir.write_out(os.environ['QWEN_MSG_DIR'], 'question',\n"
    "                         {'question': 'which fixture owns the port?',\n"
    "                          'options': []})\n"
    "        _wait(_asks_logged, 'question logged')\n")


def _runner_env(tmp_path, monkeypatch):
    """The runner's own world: the fake agent, a tmp watch registry, and watch
    ON so the run registers (the relay's addresses come from the watch id)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("QWEN_WATCH", "1")
    monkeypatch.setenv("QWEN_WATCH_NOSERVE", "1")
    monkeypatch.setenv("QWEN_WATCH_PARENT", str(os.getpid()))
    monkeypatch.setenv("QWEN_WATCH_CHILD", "0")
    monkeypatch.setenv("QWEN_WATCH_ID", "")
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    monkeypatch.setenv("QWEN_SHARE_TEST_LIB", str(SKILL))
    for k in list(os.environ):
        if k.startswith(("QWEN_SWARM_", "QWEN_DR_")):
            monkeypatch.delenv(k)
    fake = tmp_path / "fake"
    fake.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAKE_SWARM_DIR", str(fake))
    out = tmp_path / "out"
    monkeypatch.setenv("QWEN_SHARE_TEST_RUN", str(out))
    return fake, out


def test_report_has_shared_findings(tmp_path, monkeypatch):
    finding = "the migration drops the index on lowercase columns"
    fake, out = _runner_env(tmp_path, monkeypatch)
    behavior(fake, "worker", (RUNNER_WORKER_T
                              .replace("__THOUGHT__", "an ordinary thought")
                              .replace("__SHARE__",
                                       SHARE_LINES.replace("__FINDING__",
                                                           json.dumps(finding)))))
    folder = make_workflow(tmp_path / "wfs", script=SHARE_SCRIPT)
    rc = runner.main(agent_args() + [str(folder), "share two", "--out", str(out)])
    assert rc == 0, (rc, _tail(out / "run.log"))
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "# Echo" in report                             # the workflow's own text
    assert "## Shared findings" in report
    assert re.search(r"- \d\d:\d\d:\d\d unit work-1 \(worker\), phase p1, "
                     r"to work-2", report), report
    assert "> " + finding in report                       # quoted, the renderer's rule
    assert "Paths: src/cache.py" in report
    ev_kinds = [json.loads(ln).get("kind") for ln in
                (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    assert "share" in ev_kinds                            # the runner's own emit
    assert (out / "asks" / ".ended").is_file()            # and the ended stamp
    # a run without findings and without asks gets no section at all
    fake2, out2 = _runner_env(tmp_path / "b", monkeypatch)
    folder2 = make_workflow(tmp_path / "b" / "wfs")       # the stock echo: not opted
    rc2 = runner.main(agent_args() + [str(folder2), "plain", "--out", str(out2)])
    assert rc2 == 0, (rc2, _tail(out2 / "run.log"))
    report2 = (out2 / "report.md").read_text(encoding="utf-8")
    assert "# Echo" in report2
    assert "## Shared findings" not in report2


def test_thinking_sentinel_absent_swarm(tmp_path, monkeypatch):
    finding = "the shared finding itself, phrased without the sentinel"
    fake, out = _runner_env(tmp_path, monkeypatch)
    behavior(fake, "worker", (RUNNER_WORKER_T
                              .replace("__THOUGHT__", SENTINEL)
                              .replace("__SHARE__",
                                       SHARE_LINES.replace("__FINDING__",
                                                           json.dumps(finding)))))
    folder = make_workflow(tmp_path / "wfs", script=SHARE_SCRIPT)
    rc = runner.main(agent_args() + [str(folder), "sentinel", "--out", str(out)])
    assert rc == 0, (rc, _tail(out / "run.log"))
    # the sentinel really did travel the system: it is in the unit's result
    # text and in the transcript line it planted...
    assert SENTINEL in (out / "agents" / "work-1.out").read_text(encoding="utf-8")
    assert SENTINEL in (out / "agents" / "work-1" /
                        "transcript.jsonl").read_text(encoding="utf-8")
    # ...and the messaging dirs were used (the share and the question went
    # through them) -- yet the sentinel appears in none of their files.
    checked = 0
    for top in (out / "msg", out / "asks"):
        assert top.is_dir()
        for dirpath, _dirs, files in os.walk(top):
            for name in files:
                checked += 1
                p = pathlib.Path(dirpath) / name
                assert SENTINEL not in p.read_bytes().decode("utf-8", "replace"), p
    for name in ("report.md", "events.jsonl"):
        assert SENTINEL not in (out / name).read_bytes().decode(
            "utf-8", "replace"), name
    assert checked >= 2                                   # the trees were real
