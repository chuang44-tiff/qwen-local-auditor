"""Hardening of the agent-messaging work: the share-path
quoting rule, the Windows rule paths, the findings line split, the relay role
shape, the qla_msg fallback dir, the reply-before-close race, the settings-file
symlink, the run-end tmp sweeps, the finished-unit question drop, the retry
budget, the share-state order, the trailing --ask --wait, the report sections,
the confined unit's private Claude temp dir, the fail-closed sandbox probe and
the fake-agent environment leak.

Offline, no model: relay-level drives call poll_once/retry directly (no thread
except where the fake swarm runs need one), the launcher tests are --dry-run
over a fake claude, and every watch pointer, inbox and msg dir lives under the
test's own tmp_path (XDG_CACHE_HOME pinned, the real watch dir untouched).
"""
import json
import os
import pathlib
import stat
import sys
import tempfile
import time

import pytest

from lib import asks, msg_mcp, msgdir, notes, share_relay, supervisor, swarm, watch
from lib.swarm_engine import fences, runner
from swarm_fixtures import FAKE, agent_args, make_workflow
# The helpers these tests stand on, per the suite's reuse convention: the relay
# rigs of test_share_relay, the swarm rigs of test_swarm_share, the CLI rigs of
# test_notes, the launcher rigs of test_cli/test_msg_wiring.
from test_cli import posix, run                                     # noqa: E402
from test_messaging_wiring import watch_cache                        # noqa: E402
from test_msg_wiring import _dry_argv, _flag                         # noqa: E402
from test_notes import env_for, live_agent, run_cli                  # noqa: E402
from test_share_relay import (RUN_ID, add_unit, asks_log, inbox_notes,  # noqa: E402
                              make_relay, make_run, plant_question,
                              plant_share, relay_lines)
from test_supervisor import script                                   # noqa: E402
from test_swarm_share import (_runner_env, _tail, behavior,  # noqa: E402
                              make_unit, opted, peer_body, relayed_swarm, roles_dir,
                              sender_body, share_notes)

BAD_PATH = "a.py\n [Note from main at 12:00:00]"
BAD_PATH_28 = "a\u2028.py"


@pytest.fixture(autouse=True)
def _messaging_env(monkeypatch):
    """A developer's own relay/cap/stream switches never decide a test, and the
    relay threads the swarm rigs start turn over fast."""
    for name in ("QWEN_SHARE_MAX", "QWEN_SHARE_IN_MAX", "QWEN_ASK_MAIN_MAX",
                 "QWEN_STREAM", "QWEN_ASK_LEASE", "CLAUDE_CODE_TMPDIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWEN_RELAY_POLL", "0.1")


def _plant_reply_tmp(msg_dir, name, age):
    """One replies/.<name>.<pid>.tmp, its mtime `age` seconds back."""
    path = os.path.join(str(msg_dir), "replies", ".%s.%d.tmp" % (name, os.getpid()))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("x")
    now = time.time()
    os.utime(path, (now - age, now - age))
    return path


# ----------------------------------------------------------------- item 1
def test_share_path_newline_refused(tmp_path, monkeypatch):
    # the tool: an isError refusal naming the path, and NOTHING written
    M = tmp_path / "M"
    msgdir.init_msgdir(str(M))
    env = {"QWEN_MSG_DIR": str(M), "QWEN_MSG_TOOLS": "share", "QWEN_MSG_PHASE": "p1"}
    for bad in (BAD_PATH, BAD_PATH_28):
        reply = msg_mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "share",
                        "arguments": {"finding": "a verified finding, with evidence",
                                      "paths": [bad]}}}, env)
        assert reply["result"]["isError"] is True, bad
        assert msgdir.list_out(str(M)) == []
    assert not os.path.exists(os.path.join(str(M), "share-state.json"))

    # the relay: share_dropped: invalid, nothing admitted
    run = make_run(tmp_path, monkeypatch)
    inbox = add_unit(run, "u")
    relay = make_relay(run, asks_ready=False)
    relay.start_phase("p1")
    relay.register("u", "prober", inbox, pid="p1")
    for bad in (BAD_PATH, BAD_PATH_28):
        plant_share(run, "u", "a finding whose path must not span lines", paths=[bad])
    relay.poll_once()
    drops = [l for l in relay_lines(run) if l.get("ev") == "share_dropped"
             and l.get("reason") == "invalid"]
    assert len(drops) == 2
    assert msgdir.findings_read(run, "p1") == []

    # the reader validator: NotesError for such a body
    nid = notes.new_note_id()
    body = {"id": nid, "t": round(time.time(), 3), "from": "unit a",
            "to": "%s/u" % RUN_ID, "kind": "share", "text": "the finding",
            "role": "prober", "paths": [BAD_PATH]}
    with pytest.raises(notes.NotesError):
        notes.validate_note_body(body, nid)
    with pytest.raises(notes.NotesError):
        notes.validate_note_body(dict(body, paths=[BAD_PATH_28]), nid)
    notes.validate_note_body(dict(body, paths=["a.py:1"]), nid)   # the plain one stands

    # the backstop: a body that bypassed validation still renders one line per
    # path -- every line a header or a quoted one
    rendered = notes.render_note(dict(body, paths=[BAD_PATH, "b.py"]))
    for line in rendered.split("\n"):
        assert line.startswith("[") or line.startswith("> "), repr(line)
    # ...and the report reader keeps its Paths entry on its one line too
    msgdir.findings_append(run, "p1", {"id": notes.new_note_id(), "sender": "u",
                                       "role": "prober", "t": round(time.time(), 3),
                                       "text": "the finding", "paths": [BAD_PATH]})
    lines = share_relay.shared_findings_lines(run)
    assert len(lines) == 3                       # dash, quoted text, Paths: -- one each
    assert lines[2].startswith("Paths:")


# ----------------------------------------------------------------- item 2
def test_rule_path_forms(tmp_path, monkeypatch):
    if os.name != "nt":
        # this block is the POSIX form: byte-identical to the pre-helper output
        run = str(tmp_path / "run")
        R = os.path.realpath(run)
        assert fences.rule_path(run) == "/" + R
        assert fences.confine_settings(run, "u1")["permissions"]["deny"] == \
            ["Edit(/%s/%s/**)" % (R, d) for d in ("inbox", "msg", "asks", "agents")]
        assert fences.scoped_grants("Read,Edit,Write,Bash,Glob,Grep", run, "u1") == \
            "Read,Edit(/%s/sandboxes/u1/**),Bash,Glob,Grep" % R
        assert fences.scoped_grants("Read,Glob,Grep", run, "u1") == "Read,Glob,Grep"

    # the launcher's rule_path() form on Windows, with a fake realpath (the
    # public-hygiene rule: no home-directory spelling in the repo, so the
    # example sits on a plain drive-rooted folder -- same transformation)
    monkeypatch.setattr(fences.os, "name", "nt")
    monkeypatch.setattr(fences.os.path, "realpath", lambda p: str(p))
    assert fences.rule_path("C:\\work\\run") == "//c/work/run"
    assert fences.scoped_grants("Read,Edit,Write,Bash,Glob,Grep",
                                "C:\\work\\run", "u1") == \
        "Read,Edit(//c/work/run/sandboxes/u1/**),Bash,Glob,Grep"   # scoped like POSIX
    deny = fences.confine_settings("C:\\work\\run", "u1")["permissions"]["deny"]
    assert all(r.startswith("Edit(//c/") for r in deny), deny
    assert deny == ["Edit(//c/work/run/%s/**)" % d
                    for d in ("inbox", "msg", "asks", "agents")]


# ----------------------------------------------------------------- item 3
def test_findings_survive_line_separators(tmp_path):
    run = str(tmp_path / "run")
    now = round(time.time(), 3)
    records = [
        {"id": notes.new_note_id(), "sender": "a", "role": "prober", "t": now,
         "text": "a\u2028b", "paths": []},
        {"id": notes.new_note_id(), "sender": "a", "role": "prober", "t": now,
         "text": "plain finding", "paths": ["src/a.py"]},
        {"id": notes.new_note_id(), "sender": "a", "role": "prober", "t": now,
         "text": "c\u2029d\u0085e", "paths": []},
    ]
    for rec in records:
        msgdir.findings_append(run, "p1", rec)
    assert msgdir.findings_read(run, "p1") == records

    # the relay log reads the same way (one record is one \n-terminated line)
    msg_dir = os.path.join(run, "msg")
    os.makedirs(msg_dir, exist_ok=True)
    with open(os.path.join(msg_dir, "relay.jsonl"), "wb") as fh:
        fh.write((json.dumps({"t": now, "ev": "share_queued", "finding": "f1",
                              "to": "b", "note": "a\u2028b"},
                             ensure_ascii=False) + "\n").encode("utf-8"))
    recs = share_relay._relay_records(run)
    assert len(recs) == 1 and recs[0]["note"] == "a\u2028b"


# ----------------------------------------------------------------- item 4
def test_share_role_normalised(tmp_path, monkeypatch):
    sw, run, relay, fake = relayed_swarm(tmp_path, monkeypatch, seats=2)
    roles = roles_dir(tmp_path)
    behavior(fake, "Prober.v2", sender_body(
        "the prober proved the parser dies on a trailing comma", wait_to=("b",)))
    behavior(fake, "peer", peer_body())
    results = sw.run_phase([opted(make_unit(roles, "a", "Prober.v2")),
                            opted(make_unit(roles, "b", "peer"))])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    assert relay._units["a"]["role"] == "prober_v2"
    nb = share_notes(run, "b")
    assert len(nb) == 1
    assert nb[0]["role"] == "prober_v2"
    notes.validate_note_body(nb[0], nb[0]["id"])      # the reader accepts the role

    # the shaper: built-in names pass through, everything else fits ROLE_RE
    assert swarm._relay_role("worker") == "worker"    # a built-in role, unchanged
    assert swarm._relay_role("Prober.v2") == "prober_v2"
    assert swarm._relay_role("9lives") == "r9lives"
    assert swarm._relay_role("x" * 40) == "x" * 32
    for stem in ("Prober.v2", "worker", "9lives", "x" * 40, "", "!!!", "UP-er"):
        assert notes.ROLE_RE.fullmatch(swarm._relay_role(stem)), stem


# ----------------------------------------------------------------- item 5
def _fake_claude(tmp_path):
    """The stand-in claude of the wiring tests (the test_msg_wiring convention, module's
    own copy): it only has to exist and be executable for the launcher's command -v and
    answer the --help capability probe. A --dry-run never starts it."""
    p = tmp_path / "fake-claude"
    p.write_text('#!/usr/bin/env bash\n'
                 'if [ "${1:-}" = --help ]; then\n'
                 '  echo "  --restricted  Restricted mode"\n'
                 '  exit 0\n'
                 'fi\n'
                 'exit 0\n', encoding="utf-8", newline="\n")
    p.chmod(0o755)
    return p


def test_no_msg_server_for_non_inbox_suffix(tmp_path):
    fake = _fake_claude(tmp_path)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    plain_inbox = tmp_path / "inbox" / "u1"           # NOT the .inbox shape
    plain_inbox.mkdir(parents=True)
    base = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
            "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
            "TMPDIR": posix(tmp_path / "tmp"),
            "QWEN_INBOX": posix(plain_inbox)}
    r = run(tmp_path, ["--dry-run", "hi"], None, fake, extra=dict(base))
    assert r.returncode == 0, r.stderr
    argv = _dry_argv(r)
    assert "--mcp-config" not in argv, argv           # no qla_msg server
    assert "mcp__qla_msg__" not in (_flag(argv, "--allowed-tools") or ""), argv

    # the derivable shape keeps the wiring (as the existing wiring tests set it)
    dot_inbox = tmp_path / "x.inbox"
    notes.init_inbox(str(dot_inbox))
    r2 = run(tmp_path, ["--dry-run", "hi"], None, fake,
             extra=dict(base, QWEN_INBOX=posix(dot_inbox)))
    assert r2.returncode == 0, r2.stderr
    argv2 = _dry_argv(r2)
    cfgs = [argv2[i + 1] for i, a in enumerate(argv2) if a == "--mcp-config"]
    assert len(cfgs) == 1, argv2
    cfg = json.loads(pathlib.Path(cfgs[0]).read_text(encoding="utf-8"))
    assert "qla_msg" in cfg["mcpServers"], cfgs[0]
    grants = (_flag(argv2, "--allowed-tools") or "").split(",")
    assert "mcp__qla_msg__reply" in grants, argv2
    assert (tmp_path / "x.msg" / "out" / "new").is_dir()   # the derived dir


# ----------------------------------------------------------------- item 6
def test_reply_before_close_returned(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    addr, inbox = live_agent(cache)
    msgdir.init_msgdir(str(msgdir.single_msgdir(watch.watch_dir(), addr)))
    real_read = msgdir.read_reply
    state = {"n": 0}

    def first_blind(M, note_id):
        state["n"] += 1
        if state["n"] == 1:
            # the first poll's read caught the msg dir just BEFORE the agent's
            # reply landed and its owner closed the inbox: read_reply must be
            # called again before the wait reports exit 11
            assert msgdir.publish_reply(M, note_id, "one round left, then the fix")
            notes.close_inbox(str(inbox), "run_ended")
            return None
        return real_read(M, note_id)

    monkeypatch.setattr(msgdir, "read_reply", first_blind)
    rc = notes.cmd_ask([addr, "how far?", "--wait", "5"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert out.splitlines()[0].startswith("asked ")
    assert "one round left, then the fix" in out


# ----------------------------------------------------------------- item 7
@pytest.mark.skipif(os.name != "posix", reason="symlinks are the POSIX case")
def test_settings_file_symlink_replaced(tmp_path):
    run = tmp_path / "run"
    (run / "agents").mkdir(parents=True)
    sw = swarm.Swarm([sys.executable, str(FAKE)], run, seats=1, timeout=5, backoff=0)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"keep": 1}), encoding="utf-8")
    target = sw.agents_dir / "u1.settings.json"
    os.symlink(str(outside), str(target))
    conf = {"permissions": {"deny": ["Edit(//run/msg/**)"]}}
    u = swarm.Unit(name="u1", role_file=str(tmp_path / "r.md"), prompt="p",
                   settings_extra=conf)
    argv = sw._argv(u, sw.agents_dir / "u1.prompt.md", 30)
    assert argv[argv.index("--settings-extra") + 1] == str(target)
    assert json.loads(outside.read_text(encoding="utf-8")) == {"keep": 1}  # untouched
    assert not os.path.islink(str(target))             # the link is gone, not followed
    assert json.loads(target.read_text(encoding="utf-8")) == conf
    assert stat.S_IMODE(os.stat(str(target)).st_mode) == 0o600


# ----------------------------------------------------------------- item 8
def test_run_end_sweeps_reply_tmp(tmp_path, monkeypatch):
    # the swarm runner's finally: every RUN/msg/units/* dir is swept of the
    # stale reply tmps its units planted (old gone, fresh kept)
    fake, out = _runner_env(tmp_path, monkeypatch)
    behavior(fake, "worker", '''
import os, time
def answer(prompt, resumed):
    d = os.path.join(os.environ["QWEN_MSG_DIR"], "replies")
    now = time.time()
    for name, age in (("stale", 7200.0), ("fresh", 0.0)):
        p = os.path.join(d, ".reply-%s.1.tmp" % name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.utime(p, (now - age, now - age))
    return 0, '```json\\n[]\\n```'
''')
    folder = make_workflow(tmp_path / "wfs")
    rc = runner.main(agent_args() + [str(folder), "sweep the tmps", "--out", str(out)])
    assert rc == 0, (rc, _tail(out / "run.log"))
    units = out / "msg" / "units"
    assert units.is_dir()
    seen = 0
    for name in sorted(os.listdir(str(units))):
        names = sorted(os.listdir(str(units / name / "replies")))
        assert not [n for n in names if n.endswith(".reply-stale.1.tmp")], name
        assert [n for n in names if n.endswith(".reply-fresh.1.tmp")], name
        seen += 1
    assert seen >= 1

    # the until-done loop's own end (RUN/msg) sweeps the same way
    loop = tmp_path / "loop"
    inbox = loop / "inbox"
    notes.init_inbox(str(inbox))
    msg_dir = loop / "msg"
    msgdir.init_msgdir(str(msg_dir))
    old = _plant_reply_tmp(msg_dir, "old", 7200.0)
    fresh = _plant_reply_tmp(msg_dir, "fresh", 0.0)
    monkeypatch.setattr(supervisor, "_RELAY", None)
    supervisor._close_run_inbox(str(inbox))
    assert not os.path.exists(old)
    assert os.path.exists(fresh)


# ----------------------------------------------------------------- item 9
def test_question_from_finished_unit_dropped(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    inbox = add_unit(run, "a")
    assert asks.init_asks_only(run)
    relay = make_relay(run)                      # asks_ready True: the drop is
    relay.register("a", "prober", inbox, pid=None)   # the reason, not the gate
    relay.unit_done("a")
    plant_question(run, "a", text="which fixture owns the port?")
    relay.poll_once()
    drops = [l for l in asks_log(run) if l.get("ev") == "question_dropped"]
    assert len(drops) == 1, drops
    assert drops[0]["reason"] == "unit_ended"
    assert drops[0]["asker"] == "%s/a" % RUN_ID
    new_dir = os.path.join(run, "asks", "new")
    assert not [n for n in os.listdir(new_dir) if n.endswith(".md")]


# ----------------------------------------------------------------- item 10
def test_retry_budget_counts_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_SHARE_IN_MAX", "2")
    run = make_run(tmp_path, monkeypatch)
    ia, ib = add_unit(run, "a"), add_unit(run, "b")
    relay = make_relay(run, asks_ready=False)
    relay.start_phase("p1")
    relay.register("a", "prober", ia, pid="p1")
    relay.register("b", "prober", ib, pid="p1")
    plant_share(run, "a", "first shared finding, with evidence")
    relay.poll_once()
    assert len(inbox_notes(ib)) == 1                   # one note pending in new/
    relay.retry("b", 2)                                # the budget starts at it
    plant_share(run, "a", "second shared finding, with evidence")
    plant_share(run, "a", "third shared finding, with evidence")
    relay.poll_once()
    assert len(inbox_notes(ib)) == 2                   # ONE new note under the cap
    caps = [l for l in relay_lines(run) if l.get("ev") == "share_dropped"
            and l.get("reason") == "receiver_cap" and l.get("to") == "b"]
    assert len(caps) == 1
    assert caps[0]["attempt"] == "a2"


# ----------------------------------------------------------------- item 11
def test_share_state_saved_before_write(tmp_path, monkeypatch):
    def boom_save(*args, **kw):
        raise msgdir.MsgError("state save refused")

    def boom_write(M, kind, payload):
        raise msgdir.MsgError("outbox write refused")

    with monkeypatch.context() as m:
        M1 = tmp_path / "M1"
        msgdir.init_msgdir(str(M1))
        m.setattr(msgdir, "save_share_state", boom_save)
        reply = msg_mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "share",
                        "arguments": {"finding": "a verified finding",
                                      "paths": ["a.py"]}}},
            {"QWEN_MSG_DIR": str(M1), "QWEN_MSG_TOOLS": "share",
             "QWEN_MSG_PHASE": "p1"})
        assert reply["result"]["isError"] is True
        assert msgdir.list_out(str(M1)) == []          # a failed save writes nothing

    with monkeypatch.context() as m:
        M2 = tmp_path / "M2"
        msgdir.init_msgdir(str(M2))
        m.setattr(msgdir, "write_out", boom_write)
        reply = msg_mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "share",
                        "arguments": {"finding": "a verified finding",
                                      "paths": ["a.py"]}}},
            {"QWEN_MSG_DIR": str(M2), "QWEN_MSG_TOOLS": "share",
             "QWEN_MSG_PHASE": "p1"})
        assert reply["result"]["isError"] is True      # write_out still errors
        assert msgdir.share_state(str(M2))["count"] == {"p1": 1}   # state went first


# ----------------------------------------------------------------- item 12
def test_ask_trailing_wait_accepted(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    addr, _inbox = live_agent(cache)
    msgdir.init_msgdir(str(msgdir.single_msgdir(watch.watch_dir(), addr)))
    env = env_for(tmp_path)
    res = run_cli(env, tmp_path, ["ask", addr, "progress?", "--wait", "1"],
                  timeout=60)                            # nobody answers
    assert res.returncode == 10, res.stdout + res.stderr  # not a usage error
    assert res.stdout.startswith("asked ")
    assert res.stderr.strip().startswith("no answer yet: ")
    # the before-TEXT form still works, and --send keeps its strict grammar
    res = run_cli(env, tmp_path, ["ask", addr, "--wait", "1", "progress?"],
                  timeout=60)
    assert res.returncode == 10, res.stdout + res.stderr
    res = run_cli(env, tmp_path, ["send", addr, "text", "--wait", "1"], timeout=60)
    assert res.returncode == 2, res.stdout + res.stderr


# ----------------------------------------------------------------- item 13
def test_report_questions_without_findings(tmp_path, monkeypatch):
    fake, out = _runner_env(tmp_path, monkeypatch)
    behavior(fake, "worker", '''
def _ask_logged():
    try:
        with open(os.path.join(os.environ["QWEN_SHARE_TEST_RUN"], "asks",
                               "log.jsonl"), "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    for ln in data.decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("ev") in ("question_admitted",
                                                       "question_dropped"):
            return True
    return False

def answer(prompt, resumed):
    msgdir.write_out(os.environ["QWEN_MSG_DIR"], "question",
                     {"question": "which fixture owns the port?", "options": []})
    _wait(_ask_logged, "question logged")
    return 0, '```json\\n{"note": "asked"}\\n```'
''')
    wf_script = '''
def run(wf):
    items = [{"id": "I1"}]
    res = wf.fan_out("ask", "worker", items,
                     lambda batch: "ask one",
                     lambda text, batch: [{"id": it["id"]} for it in batch],
                     share=True)
    wf.report("# Ask\\n\\n%d rows\\n" % len(res.rows))
'''
    folder = make_workflow(tmp_path / "wfs", script=wf_script)
    rc = runner.main(agent_args() + [str(folder), "ask only", "--out", str(out)])
    assert rc == 0, (rc, _tail(out / "run.log"))
    report = (out / "report.md").read_text(encoding="utf-8")
    lines = report.splitlines()
    assert "# Ask" in report                             # the workflow's own text
    assert "## Shared findings" not in report            # no findings: no heading
    rows = [i for i, ln in enumerate(lines) if ln.startswith("ask-main: ")
            or ln.startswith("questions: ")]
    assert any(lines[i].startswith("questions: ") for i in rows), report
    assert lines[rows[0] - 1] == ""                      # on its own, after a blank


# ----------------------------------------------------------------- item 15
@pytest.mark.skipif(os.name == "nt", reason="the dir mode and the short-path rule "
                                            "are POSIX's (Windows temp dirs are long "
                                            "and have no st_mode bits)")
def test_confined_unit_private_tmpdir(tmp_path, monkeypatch):
    # the confinement the launcher gives a Bash unit is the settings block it carries:
    # run_unit keys the private Claude temp dir on "sandbox" in those settings, so the
    # unit is built on the swarm rig with exactly that block and nothing else
    sw, run_dir, _relay, bdir = relayed_swarm(tmp_path, monkeypatch, seats=2)
    record = (
        "import json, os\n"
        "def answer(prompt, resumed):\n"
        "    t = os.environ.get('CLAUDE_CODE_TMPDIR')\n"
        "    with open(os.path.join(os.environ['FAKE_SWARM_DIR'],\n"
        "                           %r + '.env.json'), 'w', encoding='utf-8') as fh:\n"
        "        json.dump({'tmp': t,\n"
        "                   'existed': bool(t) and os.path.isdir(t),\n"
        "                   'mode': None if not t else (os.stat(t).st_mode & 0o777),\n"
        "                   'tmpdir': os.environ.get('TMPDIR')}, fh)\n"
        "    return 0, '```json\\n[]\\n```'\n")
    for name in ("prober", "reader"):                    # each records its own env
        (bdir / ("%s.py" % name)).write_text(record % name, encoding="utf-8")
    roles = roles_dir(tmp_path)
    conf = fences.confine_settings(str(run_dir), "u1")
    assert "sandbox" in conf
    u1 = make_unit(roles, "u1", "prober", settings_extra=conf)
    u2 = make_unit(roles, "u2", "reader")                # no settings block at all
    results = sw.run_phase([u1, u2])
    assert all(r["ok"] for r in results), [(r["name"], r["why"]) for r in results]
    rec1 = json.loads((bdir / "prober.env.json").read_text(encoding="utf-8"))
    rec2 = json.loads((bdir / "reader.env.json").read_text(encoding="utf-8"))

    tmp = rec1["tmp"]
    assert tmp and rec1["existed"], rec1                  # set, and a real dir at spawn
    assert rec1["mode"] == 0o700, rec1                    # the unit's own from the start
    assert len(tmp) < 40, tmp                             # SHORT: the bridge sockets
    assert not tmp.startswith(str(run_dir))               # not inside the run folder
    assert rec1["tmpdir"] == rec2["tmpdir"]               # TMPDIR itself untouched
    assert not os.path.isdir(tmp)                         # gone when the unit ended

    assert rec2["tmp"] is None                            # an unconfined unit: none
    # the key is the key without the env: CLAUDE_CODE_TMPDIR rides nowhere near it
    twin = swarm.Unit(name=u1.name, role_file=u1.role_file, prompt=u1.prompt,
                      toolset=u1.toolset, grants=u1.grants, web=u1.web,
                      mcp_config=u1.mcp_config, effort=u1.effort,
                      key_extra=u1.key_extra, deep=u1.deep, browser=u1.browser,
                      browser_eval=u1.browser_eval, claude=u1.claude,
                      settings_extra=u1.settings_extra,
                      env={"CLAUDE_CODE_TMPDIR": tmp})
    assert sw._key(u1) == sw._key(twin)


# ----------------------------------------------------------------- items 16, 17
@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="the bwrap probe is Linux's")
def test_sandbox_block_fails_closed_and_probe_unshares_net(tmp_path, monkeypatch):
    run = tmp_path / "x" / "run"
    run.mkdir(parents=True)
    cfg = fences.confine_settings(str(run), "u1", sandbox=True)
    assert cfg["sandbox"] == {"enabled": True, "failIfUnavailable": True,
                              "allowUnsandboxedCommands": False,
                              "autoAllowBashIfSandboxed": True}

    # the probe runs its `true` through the network namespace too
    bindir = tmp_path / "sandbox-bin"
    bindir.mkdir()
    record = tmp_path / "bwrap.argv"
    bwrap = bindir / "bwrap"
    bwrap.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$WRAP_OUT"\nexit 0\n',
                     encoding="utf-8", newline="\n")
    bwrap.chmod(0o755)
    socat = bindir / "socat"
    socat.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    socat.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("WRAP_OUT", str(record))
    fake_tmp = tmp_path / "fake-tmp"               # keep the run outside the temp
    fake_tmp.mkdir()                               # folder so the TMPDIR rule is silent
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))
    assert fences.confine_probe(str(run)) is None
    argv = record.read_text(encoding="utf-8").splitlines()
    assert "--unshare-net" in argv, argv
    assert argv == ["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-net", "true"]


# ----------------------------------------------------------------- item 14
def test_fake_agent_env_not_leaked(tmp_path):
    script(tmp_path, [{"result": "x"}])                   # sets it for THIS test's fake
    assert os.environ.get("FAKE_AGENT_SCRIPT") == str(tmp_path / "script.json")


def test_fake_agent_env_not_leaked_follows():
    # the following test never sees the previous one's script (conftest's fixture)
    assert "FAKE_AGENT_SCRIPT" not in os.environ
