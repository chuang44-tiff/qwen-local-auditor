import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import pytest

from lib import swarm
from lib.swarm_engine import api, manifest, steps
from swarm_fixtures import FAKE, calls, git_repo, make_workflow, unit_names

ROLES = {"worker": {"file": "roles/worker.md", "fence": "none"},
         "reader": {"file": "roles/reader.md", "fence": "web", "budget_weight": 2},
         "voter": {"file": "roles/voter.md", "fence": "none", "effort": "low"}}
ECHO = r'''
import json, re
def answer(p, r):
    ids = re.findall(r"^- (\S+):", p, re.M)
    return 0, "```json\n" + json.dumps([{"id": i} for i in ids]) + "\n```"
'''
VOTER = r'''
import json, re
def answer(p, r):
    ids = re.findall(r"^- (\S+):", p, re.M)
    return 0, "```json\n" + json.dumps([{"claim": i, "verdict": "refuted" if i == "C1" else "supported"} for i in ids]) + "\n```"
'''
SLOPPY_VOTER = r'''
import json, re
def answer(p, r):
    rows = []
    for i in re.findall(r"^- (\S+):", p, re.M):
        if i == "C1":
            rows.append({"claim": i, "verdict": "supported"})
        elif i == "C2":
            rows.append({"claim": i})                                    # no verdict at all
        elif i == "C3":
            rows.append({"claim": i, "verdict": None})                   # not a string
        rows.append({"claim": "never-asked-about", "verdict": "refuted"})   # unknown claim
    return 0, "```json\n" + json.dumps(rows) + "\n```"
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    (d / "worker.py").write_text(ECHO, encoding="utf-8")
    (d / "reader.py").write_text(ECHO, encoding="utf-8")
    (d / "voter.py").write_text(VOTER, encoding="utf-8")
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    return d


def workflow(tmp_path, deadline=None, **cfg_changes):
    folder = make_workflow(tmp_path / "wf", {"roles": ROLES}, roles=("worker", "reader", "voter"))
    m = manifest.load(folder)
    cfg = {"goal": "g", "depth": "quick", "items": 3, "max_agents": 3, "max_items": 2,
           "timeout_per_item": 100, "retries": 0, "effort": None, "role_effort": {},
           "hours": None, "deadline": None, "rounds": 1, "target": None}
    cfg.update(cfg_changes)
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    mcp = run / "mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    sw = swarm.Swarm([sys.executable, str(FAKE)], run, seats=2, timeout=60, backoff=0,
                     deadline=deadline)
    return api.Workflow(m, cfg, run, sw, goal="g", mcp=mcp, web_seats=1)


def items(n):
    return [{"id": "I%d" % i} for i in range(1, n + 1)]


def prompt(batch):
    return "items:\n" + "\n".join("- %s:" % it["id"] for it in batch)


def echo_parse(text, batch):
    return [r for r in steps.extract_json(text) if r["id"] in {it["id"] for it in batch}]


def test_fan_out_deals_in_waves_with_the_released_unit_names(tmp_path, fake):
    wf = workflow(tmp_path)
    res = wf.fan_out("work", "worker", items(8), prompt, echo_parse)
    # 3 agents x 2 items = 6 per wave: wave 1 deals 6 over 3 agents, wave 2 deals 2 over 2
    assert sorted(unit_names(fake)) == ["work-1", "work-2", "work-3", "work-w2-1", "work-w2-2"]
    assert sorted(r["id"] for r in res.rows) == ["I%d" % i for i in range(1, 9)]
    assert res.ok and res.dropped_items == [] and res.not_run_items == []
    assert len(res.units) == 5 and all(u["ok"] for u in res.units)
    first = (tmp_path / "run" / "agents" / "work-1.prompt.md").read_text(encoding="utf-8")
    assert re.findall(r"^- (I\d+):", first, re.M) == ["I1", "I4"]      # round-robin deal


def test_fan_out_max_items_lowers_the_per_agent_cap(tmp_path, fake):
    wf = workflow(tmp_path)
    wf.fan_out("work", "worker", items(4), prompt, echo_parse, max_items=1)
    assert sorted(unit_names(fake)) == ["work-1", "work-2", "work-3", "work-w2-1"]


def test_timeouts_effort_and_seats_follow_the_role(tmp_path, fake):
    wf = workflow(tmp_path, role_effort={"worker": "high"})
    wf.fan_out("work", "worker", items(6), prompt, echo_parse)
    wf.fan_out("read", "reader", items(6), prompt, echo_parse)
    wf.agent("one", "voter", "- C9:", lambda text: steps.extract_json(text))
    by = {pathlib.Path(a[a.index("-C") + 1]).name: a for a in calls(fake)}
    # default depth on: every role here is deep, and a review round is two qwen-agent
    # calls, so each unit is handed half its timeout (the budget the unit itself gets is
    # the max(300, ...) figure below, halved on the way to the agent)
    assert by["work-1"][by["work-1"].index("--timeout") + 1] == "150"   # max(300, 2 x 100) / 2
    assert by["read-1"][by["read-1"].index("--timeout") + 1] == "200"   # weight 2: 2 x 2 x 100, / 2
    assert by["one-1"][by["one-1"].index("--timeout") + 1] == "150"     # an agent counts 2 items
    assert by["work-1"][by["work-1"].index("-e") + 1] == "high"          # --role-effort
    assert by["one-1"][by["one-1"].index("-e") + 1] == "low"             # the manifest's effort
    assert "-e" not in by["read-1"]                                      # nothing set: qwen-agent's
    assert "--web" in by["read-1"] and "--mcp-config" in by["read-1"]
    rows = [line.split() for line in (fake / "counts").read_text(encoding="utf-8").splitlines()]
    assert max(int(n) for role, n in rows if role == "reader") <= 1      # web_seats=1


def test_agent_returns_parse_or_none_and_records_the_unit(tmp_path, fake):
    wf = workflow(tmp_path)
    assert wf.agent("one", "worker", "- X:", lambda text: steps.extract_json(text)) == [{"id": "X"}]
    assert wf.last_unit["ok"] and wf.last_unit["name"] == "one-1"
    (fake / "worker.py").write_text("def answer(p, r):\n    return 7, ''\n", encoding="utf-8")
    assert wf.agent("two", "worker", "- Y:", lambda text: steps.extract_json(text)) is None
    assert not wf.last_unit["ok"] and "exit 7" in wf.last_unit["why"]
    assert wf.dropped == 1


def test_dropped_items_are_reported(tmp_path, fake):
    (fake / "worker.py").write_text(
        "import json, re\n"
        "def answer(p, r):\n"
        "    ids = re.findall(r'^- (\\S+):', p, re.M)\n"
        "    if 'I1' in ids: return 7, ''\n"
        "    return 0, '```json\\n' + json.dumps([{'id': i} for i in ids]) + '\\n```'\n",
        encoding="utf-8")
    res = workflow(tmp_path).fan_out("work", "worker", items(3), prompt, echo_parse)
    assert res.dropped_items == [{"id": "I1"}] and not res.ok
    assert sorted(r["id"] for r in res.rows) == ["I2", "I3"]


def test_vote_is_claim_major_and_tallies_the_votes_requested(tmp_path, fake):
    wf = workflow(tmp_path)
    claims = [{"id": "C1"}, {"id": "C2"}]

    def vprompt(batch):
        return "\n".join("- %s:" % c["id"] for c, _ in batch)

    def vparse(text, batch):
        return steps.extract_json(text)
    v = wf.vote("verify", "voter", claims, 3, vprompt, vparse)
    assert dict(v) == {"C1": "refuted", "C2": "supported"}
    assert {k: len(vs) for k, vs in v.cast.items()} == {"C1": 3, "C2": 3}
    assert v.requested == {"C1": 3, "C2": 3}
    for p in (tmp_path / "run" / "agents").glob("verify-*.prompt.md"):
        ids = re.findall(r"^- (C\d+):", p.read_text(encoding="utf-8"), re.M)
        assert len(ids) == len(set(ids))                   # never two votes on one claim
    with pytest.raises(ValueError):
        wf.vote("verify", "voter", claims, 4, vprompt, vparse)   # 4 voters > --max-agents 3


def test_vote_rows_without_verdict_are_ignored(tmp_path, fake):
    # A row with no "verdict" (or one that is not a string) is ignored the way a row
    # naming an unknown claim is: the claim keeps the votes it did get, no KeyError.
    (fake / "voter.py").write_text(SLOPPY_VOTER, encoding="utf-8")
    wf = workflow(tmp_path)
    claims = [{"id": "C1"}, {"id": "C2"}, {"id": "C3"}]

    def vprompt(batch):
        return "\n".join("- %s:" % c["id"] for c, _ in batch)

    def vparse(text, batch):
        return steps.extract_json(text)
    v = wf.vote("verify", "voter", claims, 3, vprompt, vparse)
    assert dict(v) == {"C1": "supported", "C2": "unclear", "C3": "unclear"}
    assert {k: len(vs) for k, vs in v.cast.items()} == {"C1": 3, "C2": 0, "C3": 0}
    assert v.requested == {"C1": 3, "C2": 3, "C3": 3}
    assert v.result.ok and wf.dropped == 0


def test_deadline_logs_unrun_items_and_counts_them(tmp_path, fake):
    wf = workflow(tmp_path, deadline=time.time() - 1)
    res = wf.fan_out("work", "worker", items(2), prompt, echo_parse)
    assert res.rows == [] and len(res.not_run_items) == 2 and wf.not_run == 2
    assert wf.agent("one", "worker", "- X:", steps.extract_json, item="goal") is None
    assert wf.last_unit["deadline"] and wf.not_run == 3
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8").splitlines()
    assert "work-1\tworker\t-\t0\t0\tdeadline: I1 not started" in log
    assert "one-1\tworker\t-\t0\t0\tdeadline: goal not started" in log
    assert calls(fake) == []
    assert wf.agent("late", "worker", "- X:", steps.extract_json, always=True) == [{"id": "X"}]


def test_artifacts_save_load_forget_write_report(tmp_path, fake):
    wf = workflow(tmp_path)
    wf.save("angles", [1, 2])
    assert wf.load("angles") == [1, 2] and wf.exists("angles")
    assert json.loads((tmp_path / "run" / "angles.json").read_text(encoding="utf-8")) == [1, 2]
    wf.forget("angles", "never-saved")
    assert wf.load("angles") is None and not wf.exists("angles")
    assert wf.write("patches/1.diff", "x\n").read_text(encoding="utf-8") == "x\n"
    for bad in ("../x", "/abs", "a/../../b"):
        with pytest.raises(ValueError):
            wf.write(bad, "")
    with pytest.raises(ValueError):
        wf.save("../x", 1)
    path = wf.report("# R\n")
    assert path == tmp_path / "run" / "report.md" and wf.report_path == path
    assert not list((tmp_path / "run").glob("report-round-*.md"))   # one-round: no copies
    wf.log("a\tnote")
    assert "-\tworkflow\t-\t0\t0\ta note" in (tmp_path / "run" / "run.log").read_text(encoding="utf-8")


def without_seconds(cum):
    """totals() without its seconds field: totals() reads the clock (whole seconds), so
    two calls in a row may straddle a second boundary and only that field can move."""
    return {k: v for k, v in cum.items() if k != "seconds"}


def test_totals_add_to_earlier_invocations(tmp_path, fake):
    run = tmp_path / "run"
    run.mkdir()
    (run / "totals.json").write_text(json.dumps({"agents_run": 5, "tokens": 50, "seconds": 7,
                                                 "invocations": 1}), encoding="utf-8")
    wf = workflow(tmp_path)
    wf.agent("one", "worker", "- X:", steps.extract_json)
    t = wf.totals()
    assert t["agents_run"] == 6 and t["tokens"] == 160 and t["invocations"] == 2 and t["seconds"] >= 7
    again = wf.totals()                                    # idempotent within one invocation
    assert without_seconds(again) == without_seconds(t) and again["seconds"] >= t["seconds"]
    saved = json.loads((run / "totals.json").read_text(encoding="utf-8"))
    assert without_seconds(saved) == without_seconds(t) and saved["seconds"] >= t["seconds"]


def test_steps_are_looked_up_at_call_time(tmp_path, fake, monkeypatch):
    wf = workflow(tmp_path)
    monkeypatch.setattr(steps, "merge_claims", lambda rows, cap: "patched")
    assert wf.steps.merge_claims([], 1) == "patched"
    assert wf.knob("items") == 3 and wf.knob("budget") == 100 and wf.knob("rounds") == 1
    with pytest.raises(KeyError):
        wf.knob("colour")
    with pytest.raises(api.Empty):
        wf.fail("nothing")
    wf.goal_unmet("no fix")
    wf.goal_unmet("later reason")
    assert wf.unmet == "no fix"



# ---------------------------------------------------------------- read and sandbox fences
PROBER = r"""
import json, pathlib
def answer_cwd(p, r, cwd):
    pathlib.Path(cwd, "fix.txt").write_text("fixed\n", encoding="utf-8", newline="\n")
    return 0, "```json\n" + json.dumps({"verdict": "confirmed"}) + "\n```"
"""
NONUTF8_PROBER = r"""
import json, pathlib
def answer_cwd(p, r, cwd):
    pathlib.Path(cwd, "latin.txt").write_bytes(b"caf\xe9\n")     # not valid UTF-8
    return 0, "```json\n" + json.dumps({"verdict": "confirmed"}) + "\n```"
"""
TARGET_ROLES = {"prober": {"file": "roles/prober.md", "fence": "sandbox"},
                "looker": {"file": "roles/looker.md", "fence": "read"}}


def target_workflow(tmp_path, fake, keep=False):
    if shutil.which("git") is None:
        pytest.skip("needs git")
    (fake / "prober.py").write_text(PROBER, encoding="utf-8")
    repo = git_repo(tmp_path / "repo", {"main.py": "print(1)\n"})
    folder = make_workflow(tmp_path / "wf", {"target": "required", "roles": TARGET_ROLES},
                           roles=("prober", "looker"))
    m = manifest.load(folder)
    cfg = {"goal": "g", "depth": "quick", "items": 3, "max_agents": 3, "max_items": 2,
           "timeout_per_item": 100, "retries": 0, "effort": None, "role_effort": {},
           "hours": None, "deadline": None, "rounds": 1, "target": str(repo)}
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    sw = swarm.Swarm([sys.executable, str(FAKE)], run, seats=2, timeout=60, backoff=0)
    return api.Workflow(m, cfg, run, sw, goal="g", keep_sandboxes=keep), repo, run


@pytest.fixture
def bash(monkeypatch):
    b = os.environ.get("TEST_BASH") or shutil.which("bash")
    if b:
        monkeypatch.setenv("QWEN_SWARM_BASH", b)


def test_sandbox_units_work_in_a_copy_and_hand_parse_the_patch(tmp_path, fake, bash):
    wf, repo, run = target_workflow(tmp_path, fake)
    res = wf.fan_out("probe", "prober", [{"id": "H1"}, {"id": "H2"}],
                     lambda batch: "probe " + batch[0]["id"],
                     lambda text, batch, patch: [{"id": batch[0]["id"], "patch": patch}],
                     max_items=1)
    assert [r["id"] for r in res.rows] == ["H1", "H2"]
    assert all("+fixed" in r["patch"] for r in res.rows)
    assert "+fixed" in (run / "agents" / "probe-1" / "patch.diff").read_text(encoding="utf-8")
    argv = calls(fake)[0]
    cwd = pathlib.Path(argv[argv.index("-C") + 1])
    assert cwd.parent == run / "sandboxes" and argv[argv.index("--toolset") + 1] == \
        "Read,Edit,Write,Bash,Glob,Grep"
    assert not (repo / "fix.txt").exists()
    assert not (run / "sandboxes" / "probe-1").exists()           # removed when the unit ended
    wt = subprocess.run(["git", "worktree", "list"], cwd=str(repo), capture_output=True,
                        text=True, check=True).stdout
    assert wt.count("\n") == 1
    check = wf.steps.run_cmd("grep -q fixed fix.txt", patch=res.rows[0]["patch"])
    assert check == {"applied": True, "rc": 0, "timed_out": False, "output_tail": ""}
    assert wf.steps.run_cmd("test -f fix.txt")["rc"] != 0          # the target never had it
    assert len(list((run / "cmds").glob("*.json"))) == 2
    assert "\trun_cmd\t0\t" in (run / "run.log").read_text(encoding="utf-8")


def test_run_cmd_results_are_cached(tmp_path, fake, bash):
    wf, repo, run = target_workflow(tmp_path, fake)
    first = wf.steps.run_cmd("echo hi; exit 3")
    (repo / "untracked.txt").write_text("x", encoding="utf-8")       # HEAD did not move
    assert wf.steps.run_cmd("echo hi; exit 3") == first
    assert (run / "run.log").read_text(encoding="utf-8").count("\trun_cmd\t") == 1


def test_non_utf8_patch_flows_through(tmp_path, fake, bash):
    # A patch whose bytes are not valid UTF-8 is carried as lone surrogates by
    # sandbox.diff(); every consumer of it (the unit's rows, an artifact, run_cmd's key
    # and its cache file) takes it, and nothing about a valid-UTF-8 patch changes.
    wf, repo, run = target_workflow(tmp_path, fake)
    (fake / "prober.py").write_text(NONUTF8_PROBER, encoding="utf-8")
    res = wf.fan_out("probe", "prober", [{"id": "H1"}], lambda batch: "probe H1",
                     lambda text, batch, patch: [{"patch": patch}])
    assert res.ok and res.dropped_items == [] and wf.dropped == 0
    (patch,) = [r["patch"] for r in res.rows]
    assert "caf\udce9" in patch                                   # the byte, as a surrogate
    assert b"caf\xe9" in (run / "agents" / "probe-1" / "patch.diff").read_bytes()
    wf.save("rows", res.rows)
    assert b"caf\xe9" in (run / "rows.json").read_bytes()          # the byte, not a substitute
    assert wf.load("rows") == res.rows
    check = wf.steps.run_cmd("exit 0", patch=patch)
    assert check == {"applied": True, "rc": 0, "timed_out": False, "output_tail": ""}
    assert wf.steps.run_cmd("exit 0", patch=patch) == check        # the cache answers it
    assert (run / "run.log").read_text(encoding="utf-8").count("\trun_cmd\t") == 1
    assert len(list((run / "cmds").glob("*.json"))) == 1


def test_keep_sandboxes_leaves_the_copy(tmp_path, fake, bash):
    wf, repo, run = target_workflow(tmp_path, fake, keep=True)
    wf.fan_out("probe", "prober", [{"id": "H1"}], lambda batch: "probe H1",
               lambda text, batch, patch: [{"patch": patch}])
    assert (run / "sandboxes" / "probe-1" / "fix.txt").exists()


def test_read_units_run_in_the_target_and_key_on_its_head(tmp_path, fake, bash):
    wf, repo, run = target_workflow(tmp_path, fake)
    wf.agent("look", "looker", "- X:", steps.extract_json)
    argv = calls(fake)[0]
    assert pathlib.Path(argv[argv.index("-C") + 1]) == repo.resolve()
    assert argv[argv.index("--toolset") + 1] == "Read,Glob,Grep"
    assert wf._fingerprint().startswith("git:")


# ---------------------------------------------------------------- the browser fence
BROWSER_ROLES = {"tester": {"file": "roles/tester.md", "fence": "browser"},
                 "writer": {"file": "roles/writer.md", "fence": "none"}}
SEES_DIR = r"""
import json, os
def answer(p, r):
    return 0, "```json\n" + json.dumps([{"dir": os.environ.get("QWEN_BROWSER_DIR")}]) + "\n```"
"""


def browser_workflow(tmp_path, fake, web_seats=1):
    """A run with one browser role and one plain role, and NO mcp.json at all: the
    browser fence is not a web fence, so such a manifest needs neither mcp nor --target.
    Every role answers with the QWEN_BROWSER_DIR its own session was given."""
    for role in ("tester", "writer"):
        (fake / ("%s.py" % role)).write_text(SEES_DIR, encoding="utf-8")
    folder = make_workflow(tmp_path / "wf", {"roles": BROWSER_ROLES}, roles=("tester", "writer"))
    m = manifest.load(folder)
    cfg = {"goal": "g", "depth": "quick", "items": 3, "max_agents": 3, "max_items": 2,
           "timeout_per_item": 100, "retries": 0, "effort": None, "role_effort": {},
           "hours": None, "deadline": None, "rounds": 1, "target": None}
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    sw = swarm.Swarm([sys.executable, str(FAKE)], run, seats=2, timeout=60, backoff=0)
    return api.Workflow(m, cfg, run, sw, goal="g", web_seats=web_seats), run


def test_a_browser_unit_is_told_its_own_folder_under_the_run(tmp_path, fake):
    wf, run = browser_workflow(tmp_path, fake)
    res = wf.fan_out("ui", "tester", items(2), prompt,
                     lambda text, batch: steps.extract_json(text), max_items=1)
    got = [r["dir"] for r in res.rows]
    assert got == [str(run / "browser" / "ui-1"), str(run / "browser" / "ui-2")]
    for d in got:                                       # one folder per unit, inside RUN
        assert pathlib.Path(d).parent == run / "browser"
    # wf.browser_dir(unit) is the very path the session was handed, so a report can name it
    assert [str(wf.browser_dir(u)) for u in ("ui-1", "ui-2")] == got
    argv = calls(fake)[0]
    assert "--browser" in argv and "--mcp-config" not in argv


def test_only_a_browser_role_is_given_the_variable(tmp_path, fake):
    wf, run = browser_workflow(tmp_path, fake)
    res = wf.fan_out("note", "writer", items(1), prompt,
                     lambda text, batch: steps.extract_json(text))
    assert [r["dir"] for r in res.rows] == [None]       # a plain unit inherits, nothing added


def test_a_browser_role_runs_at_the_seats_and_needs_no_mcp(tmp_path, fake):
    # A UI suite runs against local URLs, so the fence is not a web fence: no search
    # preflight, no --web-seats cap. The run above was built with no mcp.json at all.
    wf, run = browser_workflow(tmp_path, fake)
    assert wf._seats("tester") is None and wf._seats("writer") is None
    assert wf.mcp is None
    assert wf.fan_out("ui", "tester", items(1), prompt,
                      lambda text, batch: steps.extract_json(text)).ok
