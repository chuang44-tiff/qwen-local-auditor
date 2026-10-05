import json
import sys
import time

import pytest

from lib import swarm
from lib.swarm_engine import api, manifest, steps
from swarm_fixtures import FAKE, make_workflow, unit_names


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    return d


def workflow(tmp_path, rounds, deadline=None):
    m = manifest.load(make_workflow(tmp_path / "wf"))
    cfg = {"goal": "g", "depth": "quick", "items": 3, "max_agents": 2, "max_items": 10,
           "timeout_per_item": 100, "retries": 0, "effort": None, "role_effort": {},
           "hours": None, "deadline": None, "rounds": rounds, "target": None}
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    sw = swarm.Swarm([sys.executable, str(FAKE)], run, seats=2, timeout=60, backoff=0,
                     deadline=deadline)
    return api.Workflow(m, cfg, run, sw, goal="g")


def one_agent(wf):
    return wf.agent("step", "worker", "round %d" % wf.round, steps.extract_json)


def test_one_round_run_writes_no_round_files(tmp_path, fake):
    wf = workflow(tmp_path, 1)
    assert [r for r in wf.rounds()] == [1]
    assert wf.stop_reason == "rounds" and wf.round == 1
    assert not (tmp_path / "run" / "rounds.json").exists()


def test_rounds_cap_names_units_and_folders(tmp_path, fake):
    wf = workflow(tmp_path, 3)
    seen = []
    for r in wf.rounds():
        seen.append(r)
        one_agent(wf)
        wf.save("state", {"round": r})
        wf.report("# round %d\n" % r)
    assert seen == [1, 2, 3] and wf.stop_reason == "rounds"
    assert unit_names(fake) == ["step-1", "r2-step-1", "r3-step-1"]
    run = tmp_path / "run"
    assert json.loads((run / "state.json").read_text(encoding="utf-8")) == {"round": 1}
    assert json.loads((run / "round-3" / "state.json").read_text(encoding="utf-8")) == {"round": 3}
    assert (run / "report.md").read_text(encoding="utf-8") == "# round 3\n"
    assert (run / "report-round-1.md").read_text(encoding="utf-8") == "# round 1\n"
    assert json.loads((run / "rounds.json").read_text(encoding="utf-8")) == [1, 2, 3]
    assert wf.round == 1                                     # outside the loop again


def test_converged_ends_after_the_current_round(tmp_path, fake):
    wf = workflow(tmp_path, "until", deadline=time.time() + 3600)
    for r in wf.rounds():
        if r == 2:
            wf.converged("nothing new")
            wf.converged("ignored second reason")
    assert wf.round == 1 and wf.stop_reason == "converged: nothing new"


def test_deadline_stops_before_a_new_round_never_before_round_1(tmp_path, fake):
    wf = workflow(tmp_path, "until", deadline=time.time() - 1)
    assert list(wf.rounds()) == [1]
    assert wf.stop_reason == "hours"
    wf2 = workflow(tmp_path / "b", 3, deadline=time.time() - 1)
    for r in wf2.rounds():
        one_agent(wf2)                                       # the deadline skips it
    assert wf2.stop_reason == "deadline" and wf2.not_run == 1


def test_resume_reenters_a_started_round_past_the_deadline(tmp_path, fake):
    run = tmp_path / "run"
    run.mkdir()
    (run / "rounds.json").write_text("[1, 2]", encoding="utf-8")
    wf = workflow(tmp_path, 3, deadline=time.time() - 1)
    assert list(wf.rounds()) == [1, 2]                       # 2 was started: it is finished
    assert wf.stop_reason == "hours"


def test_cached_rounds_replay_without_new_agents(tmp_path, fake):
    wf = workflow(tmp_path, 2)
    for r in wf.rounds():
        one_agent(wf)
    (fake / "calls.jsonl").unlink()
    wf2 = workflow(tmp_path, 2)
    for r in wf2.rounds():
        one_agent(wf2)
    assert unit_names(fake) == []                            # every unit was a cache hit


def test_log_line_tolerates_surrogates(tmp_path, fake):
    # wf.log() may quote a patch with a non-UTF-8 byte (a lone surrogate); run.log is
    # opened with errors="replace", so the line is written instead of raising.
    wf = workflow(tmp_path, 1)
    wf.log("caf\udce9")
    assert "-\tworkflow\t-\t0\t0\tcaf" in (tmp_path / "run" / "run.log").read_text(encoding="utf-8")
