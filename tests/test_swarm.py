import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

import lib.swarm as sw

FAKE = pathlib.Path(__file__).resolve().parent / "fake_swarm_agent.py"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    return d


def role(tmp_path, name):
    p = tmp_path / ("%s.md" % name)
    p.write_text("role %s" % name, encoding="utf-8")
    return p


def unit(tmp_path, name, rolename="worker", parse=sw.extract_json):
    return sw.Unit(name=name, role_file=role(tmp_path, rolename), prompt="do %s" % name,
                   toolset="none", grants="", web=False, mcp_config=None, parse=parse)


def swarm(tmp_path, seats=4, **kw):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    return sw.Swarm([sys.executable, str(FAKE)], run, seats=seats, timeout=60, backoff=0, **kw)


def test_deal_round_robin_and_never_cuts_work():
    assert sw.deal(list(range(15)), 8) == [[0, 8], [1, 9], [2, 10], [3, 11], [4, 12], [5, 13], [6, 14], [7]]
    assert sw.deal([1, 2], 8) == [[1], [2]]
    assert sw.deal([], 8) == []
    with pytest.raises(ValueError):
        sw.deal([1], 0)


def test_deal_never_gives_one_agent_two_votes_on_a_claim():
    for claims in (1, 2, 7, 25):
        for voters in (1, 3):
            for max_agents in (voters, 4, 8):
                slots = [(c, v) for c in range(claims) for v in range(voters)]
                for batch in sw.deal(slots, max_agents):
                    ids = [c for c, _ in batch]
                    assert len(ids) == len(set(ids)), (claims, voters, max_agents)


def test_extract_json():
    assert sw.extract_json('x\n```json\n[1]\n```\nthen\n```json\n{"a": 2}\n```') == {"a": 2}
    assert sw.extract_json("```\n[3]\n```") == [3]
    assert sw.extract_json(' [4] ') == [4]
    with pytest.raises(ValueError):
        sw.extract_json("no json here")


@pytest.mark.parametrize("votes,voters,want", [
    (["refuted", "refuted", "supported"], 3, "refuted"),
    (["supported", "supported", "refuted"], 3, "supported"),
    (["supported", "unclear", "refuted"], 3, "unclear"),
    (["supported"], 3, "unclear"),                 # two votes missing -> unclear
    (["refuted", "refuted"], 3, "refuted"),
    (["refuted"], 1, "refuted"),
    (["supported"], 1, "supported"),
    ([], 1, "unclear"),
])
def test_tally(votes, voters, want):
    assert sw.tally(votes, voters) == want


def test_swarm_runs_units_and_writes_run_folder(tmp_path, fake):
    (fake / "worker.py").write_text("def answer(p, r):\n    return 0, '```json\\n{\"echo\": \"%s\"}\\n```' % p\n")
    s = swarm(tmp_path)
    res = s.run_phase([unit(tmp_path, "search-1"), unit(tmp_path, "search-2")])
    assert [r["ok"] for r in res] == [True, True]
    assert res[0]["data"] == {"echo": "do search-1"}
    run = tmp_path / "run"
    assert (run / "agents" / "search-1").is_dir() and not any((run / "agents" / "search-1").iterdir())
    assert json.loads((run / "agents" / "search-1.json").read_text(encoding="utf-8"))["data"]["echo"] == "do search-1"
    log = (run / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 2 and all(line.endswith("\tok") for line in log)
    assert s.agents_run == 2 and s.dropped == 0 and s.tokens == 220


def test_swarm_argv_is_fenced(tmp_path, fake):
    s = swarm(tmp_path)
    u = unit(tmp_path, "fetch-1")
    u.toolset, u.grants, u.web, u.mcp_config = "none", "mcp__search__search", True, tmp_path / "mcp.json"
    s.run_phase([u])
    argv = json.loads((fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
    for a, b in (("--toolset", "none"), ("-t", "mcp__search__search"), ("--permission-mode", "dontAsk"),
                 ("--timeout", "60")):
        assert argv[argv.index(a) + 1] == b
    assert "--web" in argv and "--json" in argv
    assert argv[argv.index("--mcp-config") + 1].endswith("mcp.json")
    assert argv[argv.index("-C") + 1].endswith(os.path.join("agents", "fetch-1"))


def test_unit_timeout_overrides_swarm_timeout(tmp_path, fake):
    def timed(name, timeout):
        return sw.Unit(name=name, role_file=role(tmp_path, "worker"), prompt="do %s" % name,
                       toolset="none", grants="", web=False, mcp_config=None,
                       parse=sw.extract_json, timeout=timeout)
    swarm(tmp_path).run_phase([timed("a", 77), timed("b", None)])
    calls = [json.loads(x) for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    got = {pathlib.Path(c[c.index("-C") + 1]).name: c[c.index("--timeout") + 1] for c in calls}
    assert got == {"a": "77", "b": "60"}        # timeout=None falls back to the Swarm's timeout


def test_swarm_repairs_bad_json_once_via_resume(tmp_path, fake):
    (fake / "worker.py").write_text(
        "def answer(p, resumed):\n"
        "    return (0, '```json\\n[1]\\n```') if resumed else (0, 'oops not json')\n")
    s = swarm(tmp_path)
    res = s.run_phase([unit(tmp_path, "a")])
    assert res[0]["ok"] and res[0]["data"] == [1]
    calls = [json.loads(x) for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "--resume" not in calls[0] and "--resume" in calls[1]
    repair = (tmp_path / "run" / "agents" / "a.repair.md").read_text(encoding="utf-8")
    assert "could not be used" in repair
    assert (tmp_path / "run" / "run.log").read_text(encoding="utf-8").rstrip().endswith("\trepaired")


def test_swarm_drops_after_second_bad_answer_and_on_failure(tmp_path, fake):
    (fake / "worker.py").write_text("def answer(p, r):\n    return 0, 'never json'\n")
    (fake / "broken.py").write_text("def answer(p, r):\n    return 5, ''\n")
    s = swarm(tmp_path)
    res = s.run_phase([unit(tmp_path, "a"), unit(tmp_path, "b", "broken")])
    assert [r["ok"] for r in res] == [False, False]
    assert "JSON" in res[0]["why"] or "json" in res[0]["why"]
    assert "exit 5" in res[1]["why"]
    assert s.dropped == 2
    assert not (tmp_path / "run" / "agents" / "a.json").exists()


def test_swarm_retries_server_errors_once(tmp_path, fake):
    flag = tmp_path / "seen"
    (fake / "worker.py").write_text(
        "import pathlib\n"
        "def answer(p, r):\n"
        "    f = pathlib.Path(%r)\n"
        "    if not f.exists():\n"
        "        f.write_text('1'); return 4, ''\n"
        "    return 0, '```json\\n[2]\\n```'\n" % str(flag))
    res = swarm(tmp_path).run_phase([unit(tmp_path, "a")])
    assert res[0]["ok"] and res[0]["data"] == [2]


def counts(fake):
    """The concurrency counts recorded by the fake (one per agent, last field of its line)."""
    return [int(line.split()[-1]) for line in (fake / "counts").read_text(encoding="utf-8").splitlines()]


def test_swarm_never_exceeds_seats(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0.6")
    s = swarm(tmp_path, seats=3)
    res = s.run_phase([unit(tmp_path, "u%d" % i) for i in range(9)])
    assert all(r["ok"] for r in res)
    assert 2 <= max(counts(fake)) <= 3


def test_run_phase_seats_override(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0.3")
    s = swarm(tmp_path, seats=4)
    res = s.run_phase([unit(tmp_path, "u%d" % i) for i in range(8)], seats=1)
    assert all(r["ok"] for r in res)
    assert max(counts(fake)) == 1                          # one agent at a time, not four


def test_swarm_resume_skips_done_units(tmp_path, fake):
    s = swarm(tmp_path)
    s.run_phase([unit(tmp_path, "a"), unit(tmp_path, "b")])
    (fake / "calls.jsonl").unlink()
    s2 = swarm(tmp_path)
    res = s2.run_phase([unit(tmp_path, "a"), unit(tmp_path, "b")])
    assert all(r["cached"] for r in res)
    assert not (fake / "calls.jsonl").exists()
    assert s2.agents_run == 0


def test_swarm_uncached_unit_always_runs(tmp_path, fake):
    swarm(tmp_path).run_phase([unit(tmp_path, "a")])
    u = unit(tmp_path, "a")
    u.cache = False
    res = swarm(tmp_path).run_phase([u])
    assert res[0]["ok"] and not res[0]["cached"]


def test_swarm_cached_unit_that_no_longer_parses_reruns(tmp_path, fake):
    s = swarm(tmp_path)
    s.run_phase([unit(tmp_path, "a")])
    (tmp_path / "run" / "agents" / "a.json").write_text("{broken", encoding="utf-8")
    res = swarm(tmp_path).run_phase([unit(tmp_path, "a")])
    assert res[0]["ok"] and not res[0]["cached"]


@pytest.mark.skipif(os.name != "posix", reason="signals")
def test_swarm_interrupt_stops_live_agents(tmp_path, fake):
    # A child Python process runs a phase of slow agents; SIGINT must stop them all
    # and exit 130 without leaving fake agents alive (their markers disappear).
    script = tmp_path / "drive.py"
    script.write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, %r)\n"
        "import lib.swarm as sw\n"
        "run = pathlib.Path(%r); run.mkdir(exist_ok=True)\n"
        "rf = run / 'w.md'; rf.write_text('r')\n"
        "s = sw.Swarm([sys.executable, %r], run, seats=4, timeout=60, backoff=0)\n"
        "us = [sw.Unit(name='u%%d' %% i, role_file=rf, prompt='p', toolset='none', grants='', web=False,"
        " mcp_config=None, parse=sw.extract_json) for i in range(8)]\n"
        "try:\n    s.run_phase(us)\nexcept KeyboardInterrupt:\n    sys.exit(130)\n"
        % (str(pathlib.Path(sw.__file__).resolve().parents[1]), str(tmp_path / "run"), str(FAKE)))
    env = dict(os.environ, FAKE_SWARM_SLEEP="30")
    p = subprocess.Popen([sys.executable, str(script)], env=env)
    live = fake / "live"
    deadline = time.time() + 20
    while time.time() < deadline and not (live.exists() and len(list(live.iterdir())) >= 4):
        time.sleep(0.1)
    p.send_signal(signal.SIGINT)
    assert p.wait(timeout=40) == 130
    time.sleep(0.5)
    assert not list(live.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="signals")
def test_sigterm_stops_live_agents(tmp_path, fake):
    # Same as the SIGINT test, but the driver installs the SIGTERM/SIGHUP handler and
    # the test sends SIGTERM: the swarm must stop the same way on all stop signals.
    script = tmp_path / "drive.py"
    script.write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, %r)\n"
        "import lib.swarm as sw\n"
        "run = pathlib.Path(%r); run.mkdir(exist_ok=True)\n"
        "rf = run / 'w.md'; rf.write_text('r')\n"
        "s = sw.Swarm([sys.executable, %r], run, seats=4, timeout=60, backoff=0)\n"
        "us = [sw.Unit(name='u%%d' %% i, role_file=rf, prompt='p', toolset='none', grants='', web=False,"
        " mcp_config=None, parse=sw.extract_json) for i in range(8)]\n"
        "sw.install_stop_signals()\n"
        "try:\n    s.run_phase(us)\nexcept KeyboardInterrupt:\n    sys.exit(130)\n"
        % (str(pathlib.Path(sw.__file__).resolve().parents[1]), str(tmp_path / "run"), str(FAKE)))
    env = dict(os.environ, FAKE_SWARM_SLEEP="30")
    p = subprocess.Popen([sys.executable, str(script)], env=env)
    live = fake / "live"
    deadline = time.time() + 20
    while time.time() < deadline and not (live.exists() and len(list(live.iterdir())) >= 4):
        time.sleep(0.1)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=40) == 130
    time.sleep(0.5)
    assert not list(live.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="signals")
def test_interrupted_units_are_logged_as_interrupted(tmp_path, fake):
    # After a SIGTERM stop, the units that were live when the signal arrived must
    # log "interrupted": they were stopped, not dropped, and must not inflate the
    # dropped count an exit 4 reports.
    script = tmp_path / "drive.py"
    script.write_text(
        "import sys, pathlib, time\n"
        "sys.path.insert(0, %r)\n"
        "import lib.swarm as sw\n"
        "run = pathlib.Path(%r); run.mkdir(exist_ok=True)\n"
        "rf = run / 'w.md'; rf.write_text('r')\n"
        "s = sw.Swarm([sys.executable, %r], run, seats=4, timeout=60, backoff=0)\n"
        "us = [sw.Unit(name='u%%d' %% i, role_file=rf, prompt='p', toolset='none', grants='', web=False,"
        " mcp_config=None, parse=sw.extract_json) for i in range(8)]\n"
        "sw.install_stop_signals()\n"
        "try:\n    s.run_phase(us)\nexcept KeyboardInterrupt:\n"
        "    time.sleep(1)       # let the workers whose agents just died write their log lines\n"
        "    sys.exit(130)\n"
        % (str(pathlib.Path(sw.__file__).resolve().parents[1]), str(tmp_path / "run"), str(FAKE)))
    env = dict(os.environ, FAKE_SWARM_SLEEP="30")
    p = subprocess.Popen([sys.executable, str(script)], env=env)
    live = fake / "live"
    deadline = time.time() + 20
    while time.time() < deadline and not (live.exists() and len(list(live.iterdir())) >= 4):
        time.sleep(0.1)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=40) == 130
    log_file = tmp_path / "run" / "run.log"
    deadline = time.time() + 10
    log = []
    while time.time() < deadline and len(log) < 4:
        if log_file.exists():
            log = log_file.read_text(encoding="utf-8").splitlines()
        time.sleep(0.1)
    assert log, "the live units left no run.log lines"
    assert all(line.endswith("\tinterrupted") for line in log), log
    assert not any("dropped" in line for line in log), log


def test_run_phase_after_stop_raises(tmp_path, fake):
    s = swarm(tmp_path)
    s.stop_all()
    with pytest.raises(KeyboardInterrupt):
        s.run_phase([unit(tmp_path, "a")])
    assert not (fake / "calls.jsonl").exists()


def test_non_json_python_literals_are_rejected(tmp_path, fake):
    lits = {"bad1": "{1, 2}", "bad2": "[...]", "bad3": "1+2j"}
    for rolename, lit in lits.items():
        with pytest.raises(ValueError):
            sw.extract_json("```json\n%s\n```" % lit)
        (fake / ("%s.py" % rolename)).write_text(
            "def answer(p, r):\n    return 0, '```json\\n%s\\n```'\n" % lit)
    s = swarm(tmp_path, seats=2)
    res = s.run_phase([unit(tmp_path, "u1", "bad1"), unit(tmp_path, "u2", "bad2"),
                       unit(tmp_path, "u3", "bad3")])
    assert len(res) == 3 and all(isinstance(r, dict) for r in res)
    assert all(r["ok"] is False and "still unusable" in r["why"] for r in res)
    calls = [json.loads(x) for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 6 and sum("--resume" in c for c in calls) == 3   # one repair round each
    assert s.dropped == 3


def test_deep_nesting_is_a_parse_error():
    with pytest.raises(ValueError):
        sw.extract_json("[" * 100000 + "]" * 100000)


def _crash_parse(text):
    raise KeyError("boom")


def test_crashing_parse_is_dropped_not_fatal(tmp_path, fake):
    s = swarm(tmp_path, seats=1)                 # one worker: it must survive and carry on
    res = s.run_phase([unit(tmp_path, "a", parse=_crash_parse), unit(tmp_path, "b"),
                       unit(tmp_path, "c", parse=_crash_parse)])
    assert [r["ok"] for r in res] == [False, True, False]
    assert res[0]["why"].startswith("internal error: KeyError")
    assert res[2]["why"].startswith("internal error: KeyError")
    assert s.dropped == 2


def test_no_session_no_repair(tmp_path, fake):
    # rc 6 prints no record, so nothing names a session: the unit is dropped instead
    # of a fresh session being started to repair it.
    (fake / "worker.py").write_text("def answer(p, r):\n    return 6, ''\n")
    s = swarm(tmp_path)
    res = s.run_phase([unit(tmp_path, "a")])
    assert not res[0]["ok"]
    assert res[0]["why"].startswith("answer unusable and no session to repair:")
    assert len((fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert s.dropped == 1


def test_warn_denials_is_passed(tmp_path, fake):
    swarm(tmp_path).run_phase([unit(tmp_path, "a"), unit(tmp_path, "b")])
    for line in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        assert "--warn-denials" in json.loads(line)


def test_empty_result_is_repaired(tmp_path, fake, monkeypatch):
    (fake / "worker.py").write_text(
        "def answer(p, resumed):\n"
        "    return (0, '```json\\n[9]\\n```') if resumed else (6, 'ignored')\n")
    monkeypatch.setenv("FAKE_SWARM_RC_RECORD", "6")
    s = swarm(tmp_path)
    res = s.run_phase([unit(tmp_path, "a")])
    assert res[0]["ok"] and res[0]["data"] == [9]
    calls = [json.loads(x) for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "--resume" not in calls[0] and "--resume" in calls[1]
    assert (tmp_path / "run" / "run.log").read_text(encoding="utf-8").rstrip().endswith("\trepaired")
    assert s.tokens == 220                      # the rc-6 record's usage counted too


def test_repair_failure_still_counts_tokens(tmp_path, fake, monkeypatch):
    (fake / "worker.py").write_text("def answer(p, resumed):\n    return 6, 'ignored'\n")
    monkeypatch.setenv("FAKE_SWARM_RC_RECORD", "6")
    s = swarm(tmp_path)
    res = s.run_phase([unit(tmp_path, "a")])
    assert not res[0]["ok"] and "exit 6 on repair" in res[0]["why"]
    assert res[0]["tokens"] == 220 and s.tokens == 220   # the repair record counts on any rc too


def test_cache_miss_when_prompt_changes(tmp_path, fake):
    swarm(tmp_path).run_phase([unit(tmp_path, "a")])
    calls = fake / "calls.jsonl"
    cache = tmp_path / "run" / "agents" / "a.json"

    def key(p):
        # mcp: no file -> ""; effort: None -> "" (the 7th field is the reasoning effort)
        blob = "%s\n%s\n%s\n%s\n%s\n%s\n%s" % ("role worker", p, "none", "", False, "", "")
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    rec = json.loads(cache.read_text(encoding="utf-8"))
    assert rec["key"] == key("do a") and rec["data"] == []
    assert not (tmp_path / "run" / "agents" / "a.json.tmp").exists()   # written atomically
    cache.write_text(json.dumps({"data": None, "key": key("do a")}), encoding="utf-8")
    res = swarm(tmp_path).run_phase([unit(tmp_path, "a")])
    assert res[0]["cached"] and res[0]["ok"] and res[0]["data"] is None   # null is a valid hit
    u = unit(tmp_path, "a")
    u.prompt = "a completely different question"
    res = swarm(tmp_path).run_phase([u])         # key changed -> cache miss, the unit runs
    assert res[0]["ok"] and not res[0]["cached"]
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 2
    res = swarm(tmp_path).run_phase([u])         # same prompt -> cached again
    assert res[0]["cached"]
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden"])
def test_unit_name_must_be_safe(tmp_path, name):
    with pytest.raises(ValueError):
        unit(tmp_path, name)
    unit(tmp_path, "ok.name-1_2")                # safe names still build


def test_repair_answer_kept_separately(tmp_path, fake):
    (fake / "worker.py").write_text(
        "def answer(p, resumed):\n"
        "    return (0, '```json\\n[1]\\n```') if resumed else (0, 'oops not json')\n")
    swarm(tmp_path).run_phase([unit(tmp_path, "a")])
    out = (tmp_path / "run" / "agents" / "a.out").read_text(encoding="utf-8")
    repair_out = (tmp_path / "run" / "agents" / "a.repair.out").read_text(encoding="utf-8")
    assert out == "oops not json" and repair_out == "```json\n[1]\n```"
    assert out != repair_out


def test_names_with_newline_or_repair_suffix_are_refused(tmp_path):
    for name in ("a\n", "a.repair"):               # fullmatch: a trailing newline fails;
        with pytest.raises(ValueError):            # ".repair" would collide with repair files
            unit(tmp_path, name)
    unit(tmp_path, "a.repair1")                    # merely containing ".repair" still builds


def test_duplicate_names_in_one_phase_are_refused(tmp_path, fake):
    s = swarm(tmp_path)
    with pytest.raises(ValueError):
        s.run_phase([unit(tmp_path, "a"), unit(tmp_path, "a")])
    assert not (fake / "calls.jsonl").exists()     # refused before anything ran
    res = swarm(tmp_path).run_phase([unit(tmp_path, "a"), unit(tmp_path, "b")])
    assert [r["ok"] for r in res] == [True, True]  # unique names still run


def test_parse_crash_still_counts_tokens(tmp_path, fake):
    s = swarm(tmp_path)
    res = s.run_phase([unit(tmp_path, "a", parse=_crash_parse)])
    assert not res[0]["ok"] and s.dropped == 1
    assert s.tokens == 110                         # the call's usage is not lost to the crash
    lines = (tmp_path / "run" / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "\t110\t" in lines[0]
    assert lines[0].endswith("dropped: internal error: KeyError")


def test_cache_miss_when_role_text_or_tools_change(tmp_path, fake):
    calls = fake / "calls.jsonl"

    def ncalls():
        return 0 if not calls.exists() else len(calls.read_text(encoding="utf-8").splitlines())

    def a_unit(**kw):                              # like unit() but never rewrites the role file
        return sw.Unit(name="a", role_file=tmp_path / "worker.md", prompt="do a",
                       toolset=kw.get("toolset", "none"), grants=kw.get("grants", ""),
                       web=kw.get("web", False), mcp_config=None, parse=sw.extract_json)

    swarm(tmp_path).run_phase([unit(tmp_path, "a")])
    assert ncalls() == 1
    assert swarm(tmp_path).run_phase([unit(tmp_path, "a")])[0]["cached"]
    assert ncalls() == 1                           # nothing changed -> cached
    role(tmp_path, "worker").write_text("a different role text", encoding="utf-8")
    res = swarm(tmp_path).run_phase([a_unit()])    # role text edited -> the cached answer is stale
    assert res[0]["ok"] and not res[0]["cached"] and ncalls() == 2
    res = swarm(tmp_path).run_phase([a_unit(grants="mcp__search__search")])   # tools widened
    assert res[0]["ok"] and not res[0]["cached"] and ncalls() == 3
    res = swarm(tmp_path).run_phase([a_unit(grants="mcp__search__search")])
    assert res[0]["cached"] and ncalls() == 3      # unchanged again -> cached


def test_cache_miss_when_mcp_config_changes(tmp_path, fake):
    # The cache key covers the mcp_config file's CONTENTS: an answer earned against
    # one server list must not be reused when the file (or its absence) changes.
    calls = fake / "calls.jsonl"

    def ncalls():
        return 0 if not calls.exists() else len(calls.read_text(encoding="utf-8").splitlines())

    role(tmp_path, "worker")
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": {"search": {"args": ["one"]}}}', encoding="utf-8")

    def a_unit(cfg):
        return sw.Unit(name="a", role_file=tmp_path / "worker.md", prompt="do a", toolset="none",
                       grants="", web=False, mcp_config=cfg, parse=sw.extract_json)

    swarm(tmp_path).run_phase([a_unit(cfg)])
    assert ncalls() == 1
    assert swarm(tmp_path).run_phase([a_unit(cfg)])[0]["cached"]
    assert ncalls() == 1                                        # unchanged -> cached
    cfg.write_text('{"mcpServers": {"search": {"args": ["two"]}}}', encoding="utf-8")
    res = swarm(tmp_path).run_phase([a_unit(cfg)])              # same name, new contents
    assert res[0]["ok"] and not res[0]["cached"] and ncalls() == 2
    assert swarm(tmp_path).run_phase([a_unit(cfg)])[0]["cached"]
    res = swarm(tmp_path).run_phase([a_unit(None)])             # None (no config) is its own key
    assert res[0]["ok"] and not res[0]["cached"] and ncalls() == 3
    res = swarm(tmp_path).run_phase([a_unit(cfg)])              # that run replaced the cache
    assert res[0]["ok"] and not res[0]["cached"] and ncalls() == 4


def test_timeout_is_retried_with_double_budget(tmp_path, fake):
    # a unit whose run times out (qwen-agent exit 5) gets its retries again, each try
    # with double the previous timeout; the answer on the second attempt wins
    counter = tmp_path / "attempts"
    (fake / "worker.py").write_text(
        "import pathlib\n"
        "def answer(p, r):\n"
        "    f = pathlib.Path(%r)\n"
        "    n = int(f.read_text()) if f.exists() else 0\n"
        "    f.write_text(str(n + 1))\n"
        "    if n == 0:\n"
        "        return 5, ''\n"
        "    return 0, '```json\\n[7]\\n```'\n" % str(counter))
    u = unit(tmp_path, "a")
    u.retries = 1
    res = swarm(tmp_path).run_phase([u])          # the Swarm's timeout t = 60
    assert res[0]["ok"] and res[0]["data"] == [7]
    calls = [json.loads(x) for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert [c[c.index("--timeout") + 1] for c in calls] == ["60", "120"]    # t, then 2t
    assert "retry 1" in (tmp_path / "run" / "run.log").read_text(encoding="utf-8")


def test_retries_exhausted_counts_one_drop(tmp_path, fake):
    (fake / "worker.py").write_text("def answer(p, r):\n    return 5, ''\n")
    u = unit(tmp_path, "a")
    u.retries = 2
    s = swarm(tmp_path)
    res = s.run_phase([u])
    assert not res[0]["ok"] and "exit 5" in res[0]["why"]
    calls = [json.loads(x) for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 3                                                # first try + 2 retries
    assert [c[c.index("--timeout") + 1] for c in calls] == ["60", "120", "240"]
    assert s.dropped == 1                                                 # only the final failure
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8").splitlines()
    assert [l.split("\t")[-1] for l in log] == ["retry 1: " + res[0]["why"],
                                                "retry 2: " + res[0]["why"],
                                                "dropped: " + res[0]["why"]]


def test_stopping_unit_is_not_retried(tmp_path, fake):
    # a failed attempt is never retried once the swarm is stopping: the unit is
    # interrupted, not dropped, and no further agent is started
    (fake / "worker.py").write_text("def answer(p, r):\n    return 5, ''\n")
    s = swarm(tmp_path, seats=1)
    u = unit(tmp_path, "a")
    u.retries = 3
    spawn = s._spawn

    def spawn_and_stop(argv):                        # the stop lands just after the first try
        out = spawn(argv)
        s.stop_all()
        return out
    s._spawn = spawn_and_stop
    res = s.run_unit(u)
    assert not res["ok"]
    assert len((fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert s.dropped == 0
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1 and log[0].endswith("\tinterrupted")


def test_usage_and_denied_exits_are_not_retried(tmp_path, fake):
    # only a timeout or a server error can retrying fix: an exit that says the call
    # itself was wrong (2 = usage) or denied (7) gives the same answer next time
    (fake / "usage.py").write_text("def answer(p, r):\n    return 2, ''\n")
    (fake / "denied.py").write_text("def answer(p, r):\n    return 7, ''\n")
    us = [unit(tmp_path, "a", "usage"), unit(tmp_path, "b", "denied")]
    for u in us:
        u.retries = 3
    s = swarm(tmp_path)
    res = s.run_phase(us)
    assert all(not r["ok"] for r in res)
    calls = (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2                                   # one call each, retries unused
    assert s.dropped == 2


def test_unusable_answer_is_retried_with_fresh_session(tmp_path, fake):
    # bad JSON twice (the call and its repair), good on the next attempt: the retry
    # starts a fresh session, and the repair kept its attempt's timeout
    counter = tmp_path / "n"
    (fake / "worker.py").write_text(
        "import pathlib\n"
        "def answer(p, r):\n"
        "    f = pathlib.Path(%r)\n"
        "    n = int(f.read_text()) if f.exists() else 0\n"
        "    f.write_text(str(n + 1))\n"
        "    if n < 2:\n"
        "        return 0, 'not json at all'\n"
        "    return 0, '```json\\n[5]\\n```'\n" % str(counter))
    u = unit(tmp_path, "a")
    u.retries = 1
    res = swarm(tmp_path).run_phase([u])
    assert res[0]["ok"] and res[0]["data"] == [5]
    calls = [json.loads(x) for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [("--resume" in c) for c in calls] == [False, True, False]
    assert [c[c.index("--timeout") + 1] for c in calls] == ["60", "60", "120"]


def test_empty_repair_is_retried(tmp_path, fake, monkeypatch):
    # a repair call that exits 6 (an empty answer) is as retryable as an unusable one:
    # the unit gets its retry, with a fresh session, instead of dropping at once
    (fake / "worker.py").write_text("def answer(p, r):\n    return 6, 'ignored'\n")
    monkeypatch.setenv("FAKE_SWARM_RC_RECORD", "6")
    u = unit(tmp_path, "a")
    u.retries = 1
    s = swarm(tmp_path)
    res = s.run_phase([u])
    assert not res[0]["ok"] and "exit 6 on repair" in res[0]["why"]
    parsed = [json.loads(c) for c in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(parsed) == 4                                   # (call + repair) x (first try + retry)
    assert [("--resume" in c) for c in parsed] == [False, True, False, True]
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8").splitlines()
    assert log[0].split("\t")[-1].startswith("retry 1: qwen-agent exit 6 on repair")
    assert log[-1].split("\t")[-1].startswith("dropped: ")
    assert s.dropped == 1


def test_tokens_count_both_spawns(tmp_path, fake, monkeypatch):
    # a unit re-spawned after a server error spent tokens on both spawns: every spawn's
    # usage record counts toward the unit and the Swarm, not just the last spawn's
    flag = tmp_path / "seen"
    (fake / "worker.py").write_text(
        "import pathlib\n"
        "def answer(p, r):\n"
        "    f = pathlib.Path(%r)\n"
        "    if not f.exists():\n"
        "        f.write_text('1'); return 4, 'early'\n"
        "    return 0, '```json\\n[3]\\n```'\n" % str(flag))
    monkeypatch.setenv("FAKE_SWARM_RC_RECORD", "4")
    s = swarm(tmp_path)
    res = s.run_phase([unit(tmp_path, "a")])
    assert res[0]["ok"] and res[0]["data"] == [3]
    assert res[0]["tokens"] == 220 and s.tokens == 220        # both spawns' usage counted
    calls = (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2                                    # the exit-4 spawn re-spawned once
    lines = (tmp_path / "run" / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "\t220\t" in lines[0]          # and the log line sums to it


def test_log_tokens_sum_to_total(tmp_path, fake, monkeypatch):
    # every run.log line reports the attempt it belongs to: failed attempts write
    # their own retry lines, and the unit's final line reports only its last
    # attempt, so the token column of run.log sums to the Swarm's tokens total
    monkeypatch.setenv("FAKE_SWARM_RC_RECORD", "5")
    counter = tmp_path / "n"
    (fake / "worker.py").write_text(
        "import pathlib\n"
        "def answer(p, r):\n"
        "    f = pathlib.Path(%r)\n"
        "    n = int(f.read_text()) if f.exists() else 0\n"
        "    f.write_text(str(n + 1))\n"
        "    if n < 2:\n"
        "        return 5, ''\n"
        "    return 0, '```json\\n[5]\\n```'\n" % str(counter))
    (fake / "doomed.py").write_text("def answer(p, r):\n    return 5, ''\n")
    u, d = unit(tmp_path, "a"), unit(tmp_path, "d", "doomed")
    u.retries = d.retries = 2
    s = swarm(tmp_path)
    res = s.run_phase([u, d])
    assert res[0]["ok"] and not res[1]["ok"]
    fields = [l.split("\t") for l in (tmp_path / "run" / "run.log").read_text(
        encoding="utf-8").splitlines()]
    assert len(fields) == 6                                  # 3 attempts per unit
    assert [f[3] for f in fields] == ["110"] * 6             # each line only its own call
    assert sum(int(f[3]) for f in fields) == s.tokens == 660


@pytest.mark.skipif(os.name != "posix", reason="signals")
def test_install_stop_signals_returns_previous_handlers(tmp_path):
    # Run in a subprocess so the test process keeps its own handlers.
    script = tmp_path / "drive.py"
    script.write_text(
        "import sys, signal\n"
        "sys.path.insert(0, %r)\n"
        "import lib.swarm as sw\n"
        "prev = sw.install_stop_signals()\n"
        "assert isinstance(prev, dict)\n"
        "assert prev[signal.SIGTERM] == signal.SIG_DFL\n"
        "assert prev[signal.SIGHUP] == signal.SIG_DFL\n"
        "assert signal.getsignal(signal.SIGTERM) is sw._stop_handler\n"
        "for signum, handler in prev.items():\n"
        "    signal.signal(signum, handler)\n"        # the returned dict restores the old set
        "assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL\n"
        % str(pathlib.Path(sw.__file__).resolve().parents[1]))
    p = subprocess.Popen([sys.executable, str(script)])
    assert p.wait(timeout=30) == 0


def test_no_retry_after_the_deadline(tmp_path, fake, monkeypatch):
    # a retry is new work: once the run's deadline has passed during the first attempt,
    # the unit is dropped instead of starting a second attempt with a doubled budget
    import time
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0.5")
    (fake / "worker.py").write_text("def answer(p, r):\n    return 5, ''\n")
    u = unit(tmp_path, "a")
    u.retries = 3
    res = swarm(tmp_path, deadline=time.time() + 0.2).run_phase([u])
    assert not res[0]["ok"]
    assert len((fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 1
