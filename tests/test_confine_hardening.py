"""Bash-unit confinement hardening: the Windows scoped grant, the
confined unit's private Claude temp dir (fail-closed when it cannot be made, the
real path in the env, a sweepable prefix), the empty share path's own refusal,
the trailing --wait after `--`, the settings tmp's close-before-unlink order,
and the stale private temp dirs a swarm start sweeps.

Offline, no model: units run on tests/fake_swarm_agent.py, and every run folder,
watch registry and temp base sits under tmp_path.
"""
import json
import os
import sys
import time

import pytest

from lib import msg_mcp, msgdir, notes, swarm
from lib.swarm_engine import fences
from test_share_relay import (RUN_ID, add_unit, make_relay, make_run,  # noqa: E402
                              plant_share, relay_lines)
from test_swarm_share import make_unit, relayed_swarm, roles_dir  # noqa: E402
from swarm_fixtures import FAKE  # noqa: E402


@pytest.fixture(autouse=True)
def _messaging_env(monkeypatch):
    """A developer's own relay/cap/stream switches never decide a test, and the
    relay thread the swarm rigs start turns over fast."""
    for name in ("QWEN_SHARE_MAX", "QWEN_SHARE_IN_MAX", "QWEN_ASK_MAIN_MAX",
                 "QWEN_STREAM", "QWEN_ASK_LEASE", "CLAUDE_CODE_TMPDIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWEN_RELAY_POLL", "0.1")


# ----------------------------------------------------------------- item 1
def test_windows_scoped_grants(tmp_path, monkeypatch):
    if os.name != "nt":
        # this block is the POSIX form of the rule path, asserted before any patch
        run = str(tmp_path / "run")
        R = os.path.realpath(run)
        assert fences.scoped_grants("Read,Edit,Write,Bash", run, "u1") == \
            "Read,Edit(//%s/sandboxes/u1/**),Bash" % R.lstrip("/")

    # Windows: no early return -- the bare Edit/Write become the ONE scoped rule in
    # the launcher's rule_path() spelling (a wrong path fails CLOSED, the refusal;
    # a bare grant fails OPEN, so keeping it was never the safer choice)
    monkeypatch.setattr(fences.os, "name", "nt")
    monkeypatch.setattr(fences.os.path, "realpath", lambda p: str(p))
    assert fences.scoped_grants("Read,Edit,Write,Bash", "C:\\work\\run", "u1") == \
        "Read,Edit(//c/work/run/sandboxes/u1/**),Bash"


# ----------------------------------------------------------------- item 2
def test_private_tmp_failure_fails_unit(tmp_path, monkeypatch):
    sw, run, _relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=2)
    roles = roles_dir(tmp_path)
    conf = fences.confine_settings(str(run), "u1")
    assert "sandbox" in conf

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(swarm.tempfile, "mkdtemp", boom)
    results = sw.run_phase([make_unit(roles, "u1", "prober", settings_extra=conf)])
    (res,) = results
    assert res["ok"] is False
    assert res["why"].startswith("private temp dir could not be made"), res["why"]
    assert sw.agents_run == 0                           # no qwen-agent call, no retry
    assert not (fake / "calls.jsonl").exists()          # the fake was never spawned
    evs = [json.loads(ln) for ln in (run / "events.jsonl").read_text(
        encoding="utf-8").splitlines() if ln.strip()]
    dones = [e for e in evs if e.get("kind") == "unit_done" and e.get("unit") == "u1"]
    assert len(dones) == 1 and dones[0]["ok"] is False, evs
    assert dones[0]["why"].startswith("private temp dir could not be made")


# ----------------------------------------------------------------- item 3
def test_private_tmp_realpath_and_prefix(tmp_path, monkeypatch):
    sw, run, _relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=1)
    record = (
        "import json, os\n"
        "def answer(prompt, resumed):\n"
        "    t = os.environ.get('CLAUDE_CODE_TMPDIR')\n"
        "    with open(os.path.join(os.environ['FAKE_SWARM_DIR'],\n"
        "                           'confined.env.json'), 'w', encoding='utf-8') as fh:\n"
        "        json.dump({'tmp': t, 'existed': bool(t) and os.path.isdir(t)}, fh)\n"
        "    return 0, '```json\\n[]\\n```'\n")
    (fake / "prober.py").write_text(record, encoding="utf-8")
    roles = roles_dir(tmp_path)
    conf = fences.confine_settings(str(run), "u1")
    results = sw.run_phase([make_unit(roles, "u1", "prober", settings_extra=conf)])
    assert results[0]["ok"], (results[0]["name"], results[0]["why"])
    rec = json.loads((fake / "confined.env.json").read_text(encoding="utf-8"))
    tmp = rec["tmp"]
    assert tmp and rec["existed"], rec                  # set, and a real dir at spawn
    assert tmp == os.path.realpath(tmp), tmp             # macOS: /private/tmp, not /tmp
    assert os.path.basename(tmp).startswith("qla-u-"), tmp   # the sweep knows this name
    if os.name != "nt":
        # the short-path rule is POSIX's (Windows temp dirs are long), and so is
        # the dir mode the twin test pins
        assert len(tmp) < 40, tmp                        # SHORT: the bridge sockets


# ----------------------------------------------------------------- item 4
def test_empty_share_path_message(tmp_path, monkeypatch):
    # the share tool: its own Refused text, and nothing written
    M = tmp_path / "M"
    msgdir.init_msgdir(str(M))
    reply = msg_mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "share",
                    "arguments": {"finding": "a verified finding, with evidence",
                                  "paths": [""]}}},
        {"QWEN_MSG_DIR": str(M), "QWEN_MSG_TOOLS": "share", "QWEN_MSG_PHASE": "p1"})
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"] == "a path may not be empty"
    assert msgdir.list_out(str(M)) == []

    # the reader validator: its own error text too
    nid = notes.new_note_id()
    body = {"id": nid, "t": round(time.time(), 3), "from": "unit a",
            "to": "%s/u" % RUN_ID, "kind": "share", "text": "the finding",
            "role": "prober", "paths": [""]}
    with pytest.raises(notes.NotesError) as ei:
        notes.validate_note_body(body, nid)
    assert str(ei.value) == "a path may not be empty"

    # the relay: an empty path drops with its EXISTING admission reason (nothing new)
    run = make_run(tmp_path, monkeypatch)
    inbox = add_unit(run, "u")
    relay = make_relay(run, asks_ready=False)
    relay.start_phase("p1")
    relay.register("u", "prober", inbox, pid="p1")
    plant_share(run, "u", "a finding whose path is empty", paths=[""])
    relay.poll_once()
    drops = [line for line in relay_lines(run) if line.get("ev") == "share_dropped"
             and line.get("reason") == "invalid"]
    assert len(drops) == 1, drops


# ----------------------------------------------------------------- item 5
def test_trailing_wait_after_dashdash():
    # --ask's grammar (allow_trailing_wait=True): the LAST two tokens, --wait and
    # an integer, are the wait even after the `--` -- the dashed TEXT survives
    address, wait, text = notes._parse_send(["ADDR", "--", "-x", "--wait", "30"],
                                            allow_trailing_wait=True)
    assert (address, wait, text) == ("ADDR", 30, "-x")

    # --send (allow_trailing_wait=False) is unchanged: --wait and 30 ride on as
    # positional TEXT, and the four positionals are the usage error they were
    with pytest.raises(notes.NotesError) as ei:
        notes._parse_send(["ADDR", "--", "-x", "--wait", "30"])
    assert ei.value.code == notes.EXIT_USAGE

    # only a genuine seconds value rides the tail: "soon" is TEXT, not a wait
    with pytest.raises(notes.NotesError):
        notes._parse_send(["ADDR", "--", "-x", "--wait", "soon"],
                          allow_trailing_wait=True)
    # a tail wait never excuses a second --wait
    with pytest.raises(notes.NotesError):
        notes._parse_send(["ADDR", "--wait", "5", "--", "-x", "--wait", "30"],
                          allow_trailing_wait=True)
    # and it obeys the same 1..3600 as an in-place one
    with pytest.raises(notes.NotesError):
        notes._parse_send(["ADDR", "--", "-x", "--wait", "99999"],
                          allow_trailing_wait=True)
    # the --wait= rewrite loses nothing: a tail pair after it is still a second
    # --wait, not a silently ignored one
    with pytest.raises(notes.NotesError):
        notes._parse_send(["ADDR", "--wait=5", "--", "-x", "--wait", "30"],
                          allow_trailing_wait=True)


# ----------------------------------------------------------------- item 6
def test_settings_tmp_closed_before_unlink(tmp_path, monkeypatch):
    agents = tmp_path / "run" / "agents"
    agents.mkdir(parents=True)
    calls = []
    real_close, real_unlink = os.close, os.unlink

    def boom_write(fd, data):
        raise OSError(28, "No space left on device")

    def spy_close(fd):
        calls.append("close")
        return real_close(fd)

    def spy_unlink(path):
        calls.append("unlink")
        return real_unlink(path)

    target = str(agents / "u1.settings.json")
    with monkeypatch.context() as m:
        m.setattr(os, "write", boom_write)
        m.setattr(os, "close", spy_close)
        m.setattr(os, "unlink", spy_unlink)
        with pytest.raises(OSError):
            swarm._write_settings_file(target, b"{}")
    # the fd closes BEFORE the unlink: Windows cannot unlink an open file
    assert calls == ["close", "unlink"], calls
    assert not [n for n in os.listdir(str(agents)) if n.endswith(".tmp")]
    assert not os.path.exists(target)


# ----------------------------------------------------------------- item 7
def test_stale_private_tmp_swept(tmp_path, monkeypatch):
    base = tmp_path / "tmpbase"
    base.mkdir()

    def plant(name, age):
        d = base / name
        d.mkdir()
        stamp = time.time() - age
        os.utime(str(d), (stamp, stamp))
        return d

    stale = plant("qla-u-old", 3 * 24 * 3600)            # a killed unit's leftover
    fresh = plant("qla-u-new", 0.0)                      # a live unit's dir
    other = plant("qla-advisor-x", 3 * 24 * 3600)        # another tool's name
    not_a_dir = base / "qla-u-notadir"                   # the prefix, but a file
    not_a_dir.write_text("x", encoding="utf-8")
    if os.name == "posix":
        # a planted SYMLINK under our prefix: never removed, never followed
        outside = base / "outside-target"
        outside.mkdir()
        stamp = time.time() - 3 * 24 * 3600
        os.utime(str(outside), (stamp, stamp))
        os.symlink(str(outside), str(base / "qla-u-link"))
    monkeypatch.setattr(swarm, "_private_tmp_base", lambda: str(base))
    swarm.Swarm([sys.executable, str(FAKE)], tmp_path / "run", seats=1,
                timeout=5, backoff=0)                    # the sweep runs at swarm start
    assert not stale.exists()
    assert fresh.exists()                                # young: another unit may use it
    assert other.exists()                                # no name outside the prefix
    assert not_a_dir.exists()                            # directories only, never files
    if os.name == "posix":
        assert (base / "qla-u-link").is_symlink()        # the link stays ...
        assert (base / "outside-target").is_dir()        # ... and its target untouched
        # POSIX removes only what the current uid owns
        theirs = plant("qla-u-other-uid", 3 * 24 * 3600)
        real_getuid = os.getuid
        monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1000)
        swarm.Swarm([sys.executable, str(FAKE)], tmp_path / "run2", seats=1,
                    timeout=5, backoff=0)
        assert theirs.exists()
