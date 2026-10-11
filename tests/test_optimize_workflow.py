"""The optimize workflow on a fixture repo with a slow test and fake agents."""
import json
import os
import pathlib
import shutil
import sys

import pytest

from lib.swarm_engine import runner
from swarm_fixtures import agent_args, git_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PY = pathlib.Path(sys.executable).as_posix()
SLOW = {"slow.py": "import time\nDELAY = 0.4\n\n\ndef work():\n    time.sleep(DELAY)\n    return 42\n",
        "test_slow.py": "import slow\n\n\ndef test_work():\n    assert slow.work() == 42\n",
        "test_other.py": "def test_other():\n    assert 1 + 1 == 2\n"}
PYTEST = '"%s" -m pytest -q -p no:cacheprovider' % PY
MEASURE = PYTEST + " test_slow.py"
GUARD = PYTEST

CHECKER_OK = r'''
import json
def answer(p, r):
    return 0, "```json\n" + json.dumps({"verdict": "ok", "findings": [], "suggested_bench": None}) + "\n```"
'''
CHECKER_PROBLEM = r'''
import json, re
def answer(p, r):
    line = re.search(r"^\s*[\d.]+\s+test_slow\.py\S*$", p, re.M).group(0).strip()
    return 0, "```json\n" + json.dumps({"verdict": "problem",
        "findings": [{"claim": "time is in test_slow", "evidence": line},
                     {"claim": "made up", "evidence": "this line is nowhere at all"}],
        "suggested_bench": {"measure": "x", "focus": ["slow.py"]}}) + "\n```"
'''
CHECKER_INVENTED = r'''
import json
def answer(p, r):
    return 0, "```json\n" + json.dumps({"verdict": "problem",
        "findings": [{"claim": "made up", "evidence": "this line is nowhere at all"}],
        "suggested_bench": None}) + "\n```"
'''
WRITER = "def answer(p, r):\n    return 0, '# Optimize\\n\\nslow.py sleeps less.\\n'\n"


def write_fakes(d, **roles):
    for name, src in roles.items():
        (d / ("%s.py" % name)).write_text(src, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    write_fakes(d, checker=CHECKER_OK, writer=WRITER)
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    b = os.environ.get("TEST_BASH") or shutil.which("bash")
    if b:
        monkeypatch.setenv("QWEN_SWARM_BASH", b)
    return d


def bench_file(tmp_path, **fields):
    data = {"measure": MEASURE, "guard": GUARD, "focus": ["slow.py", "test_slow.py"]}
    data.update(fields)
    p = tmp_path / "bench.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def optimize(tmp_path, repo, *extra, depth="quick"):
    run = tmp_path / "run"
    rc = runner.main(agent_args() + ["optimize", "make test_slow faster", "--target", str(repo),
                                     "--out", str(run), "--depth", depth, *extra])
    return rc, run


def report(run):
    return (run / "report.md").read_text(encoding="utf-8")


def test_no_bench_and_no_tests_is_exit_2(tmp_path, env, capsys):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo)
    assert rc == 2 and "--set bench=" in capsys.readouterr().err
    assert not run.exists()


def test_a_bad_bench_file_is_exit_2(tmp_path, env, capsys):
    repo = git_repo(tmp_path / "repo", SLOW)
    p = bench_file(tmp_path, speed="fast")
    rc, _ = optimize(tmp_path, repo, "--set", "bench=%s" % p)
    assert rc == 2 and "unknown bench field" in capsys.readouterr().err


def test_failing_measure_stops_before_round_1(tmp_path, env):
    repo = git_repo(tmp_path / "repo", dict(SLOW, **{"test_slow.py": "def test_x():\n    assert False\n"}))
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE))
    assert rc == 4
    first = report(run).splitlines()[0]
    assert first.startswith("bench: problem (see bench-review.md): B1")
    review = (run / "bench-review.md").read_text(encoding="utf-8")
    assert "B1 FAIL" in review and "rc 1" in review
    events = [json.loads(x) for x in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"verdict": "problem", "phase": "start"}.items() <= [e for e in events if e["kind"] == "bench"][0].items()
    assert not (run / "ideas.json").exists()


def test_focus_outside_the_profile_is_a_note_not_a_stop(tmp_path, env):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo,
                       "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE, focus=["slow.py"]))
    assert report(run).splitlines()[0] == "bench: ok"
    assert "B3 skipped: note: no profiled file is inside focus" in (run / "bench-review.md").read_text(encoding="utf-8")


def test_target_inside_the_noise_stops(tmp_path, env):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE, target="-0.1%"))
    assert rc == 4 and "B4" in report(run).splitlines()[0]


def test_checker_problem_with_real_evidence_stops_and_suggests(tmp_path, env):
    write_fakes(env, checker=CHECKER_PROBLEM)
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE))
    assert rc == 4 and "checker: time is in test_slow" in report(run).splitlines()[0]
    review = (run / "bench-review.md").read_text(encoding="utf-8")
    assert "time is in test_slow" in review
    assert "Dropped (evidence not found" in review and "made up" in review
    assert json.loads((run / "bench-suggested.json").read_text(encoding="utf-8"))["focus"] == ["slow.py"]


def test_checker_finding_without_evidence_does_not_stop(tmp_path, env):
    write_fakes(env, checker=CHECKER_INVENTED)
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE))
    assert report(run).splitlines()[0] == "bench: ok"
    assert "made up" in (run / "bench-review.md").read_text(encoding="utf-8")


def test_tests_knob_alone_times_the_whole_suite(tmp_path, env):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "tests=%s" % GUARD)
    saved = json.loads((run / "bench.json").read_text(encoding="utf-8"))
    assert saved["measure"] == GUARD and saved["focus"] == [] and saved["target"] is None
    assert report(run).splitlines()[0] == "bench: ok"


def test_profile_falls_back_when_flags_break_the_command(tmp_path, env):
    # appending the flags to `... && exit 0` gives `exit 0 --durations=0 ...`, which bash
    # refuses (too many arguments, rc 1): the plain baseline output is then the profile
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(
        tmp_path, measure=MEASURE + " && exit 0", guard=MEASURE + " && exit 0", focus=[]))
    assert report(run).splitlines()[0] == "bench: ok"


def test_resume_uses_the_saved_bench(tmp_path, env):
    repo = git_repo(tmp_path / "repo", SLOW)
    p = bench_file(tmp_path)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % p)
    p.write_text("{", encoding="utf-8")                     # now unreadable
    rc2 = runner.main(agent_args() + ["optimize", "--resume", str(run)])
    assert rc2 == rc


PROFILER = r'''
import json, re
def answer(p, r):
    m = (re.search(r"^\s*[\d.]+\s+test_slow\.py::test_work\s*$", p, re.M)
         or re.search(r"^OUTPUT TAIL:$", p, re.M))         # unknown runner: no per-test lines
    line = m.group(0).strip()
    n = len(re.findall(r"^- I\d+ at ", p, re.M))
    ideas = [] if n else [
        {"location": "slow.py:2", "profile_line": line, "change": "lower DELAY", "why": "sleep", "saving": "0.5s"},
        {"location": "test_slow.py:5", "profile_line": line, "change": "drop the assert", "why": "x", "saving": "0"},
        {"location": "nope.py:1", "profile_line": line, "change": "ghost file", "why": "x", "saving": "0"},
        {"location": "slow.py:2", "profile_line": "invented profile line", "change": "other", "why": "x", "saving": "0"}]
    return 0, "```json\n" + json.dumps({"ideas": ideas, "outside_focus":
        {"location": "test_other.py:1", "profile_line": line, "why": "also slow"}}) + "\n```"
'''
IMPLEMENTER = r'''
import json, pathlib, re
def answer_cwd(p, r, cwd):
    iid = re.search(r"^- (I\d+):", p, re.M).group(1)
    d = pathlib.Path(cwd)
    if "lower DELAY" in p:
        s = (d / "slow.py").read_text(encoding="utf-8").replace("DELAY = 0.4", "DELAY = 0.01")
        (d / "slow.py").write_text(s, encoding="utf-8", newline="\n")
    elif "drop the assert" in p:
        s = (d / "test_slow.py").read_text(encoding="utf-8").replace("assert slow.work() == 42", "pass")
        s = s.replace("import slow\n", "")
        (d / "test_slow.py").write_text(s, encoding="utf-8", newline="\n")
    return 0, "```json\n" + json.dumps({"id": iid, "done": True, "summary": "did it", "note": ""}) + "\n```"
'''
REVIEWER = r'''
import json, re
def answer(p, r):
    rows = []
    for pid, body in re.findall(r"^- (P\d+) .*?\n(.*?)(?=^- P\d+ |\Z)", p, re.M | re.S):
        if "pass" in body and "-    assert" in body:
            rows.append({"patch": pid, "verdict": "weakens", "lines": ["-    assert slow.work() == 42"], "reason": "assert gone"})
        else:
            rows.append({"patch": pid, "verdict": "keeps", "lines": ["+DELAY = 0.01"], "reason": "same test"})
    return 0, "```json\n" + json.dumps(rows) + "\n```"
'''
REVIEWER_FAKE_CITE = r'''
import json, re
def answer(p, r):
    return 0, "```json\n" + json.dumps([{"patch": i, "verdict": "keeps", "lines": ["+not in the patch"], "reason": "r"}
        for i in re.findall(r"^- (P\d+) ", p, re.M)]) + "\n```"
'''


@pytest.fixture
def team(env):
    write_fakes(env, profiler=PROFILER, implementer=IMPLEMENTER, reviewer=REVIEWER)
    return env


def test_a_slow_sleep_gets_a_checked_stacked_patch(tmp_path, team):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, target="-30%"))
    text = report(run)
    assert rc == 0, text
    assert text.splitlines()[0] == "bench: ok"
    stacked = (run / "patches" / "stacked.diff").read_text(encoding="utf-8")
    assert "+DELAY = 0.01" in stacked and "assert" not in stacked
    assert "guard: passed" in text
    assert "test_other.py:1" in text                     # the outside-focus note, report only
    assert (repo / "slow.py").read_text(encoding="utf-8").count("DELAY = 0.4") == 1   # target untouched
    ideas = json.loads((run / "ideas.json").read_text(encoding="utf-8"))
    assert [i["location"] for i in ideas] == ["slow.py:2", "test_slow.py:5"]   # ghost + invented dropped


def test_a_weakening_patch_is_never_approved(tmp_path, team):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE))
    cands = json.loads((run / "candidates.json").read_text(encoding="utf-8"))
    weak = [c for c in cands if c["idea"] == "I2"][0]
    assert not weak["approved"]


def test_a_citation_not_in_the_patch_counts_as_unclear(tmp_path, team):
    write_fakes(team, reviewer=REVIEWER_FAKE_CITE)
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE))
    assert rc == 4 and not (run / "patches" / "stacked.diff").exists()
    assert "no approved patch" in report(run)


def test_patch_outside_focus_is_not_measured(tmp_path, team):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo,
                       "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE, focus=["slow.py"]))
    cands = json.loads((run / "candidates.json").read_text(encoding="utf-8"))
    assert [c["location"] for c in cands] == ["slow.py:2"]   # test_slow.py:5 dropped as an idea
    assert rc == 0


def test_target_reached_stops_rounds_early(tmp_path, team):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, target="-20%"), depth="deep")
    assert rc == 0
    assert not (run / "round-2").exists()
    assert "target reached" in (run / "run.log").read_text(encoding="utf-8")


def test_target_not_reached_is_exit_4_with_the_stack(tmp_path, team):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE, target="0.01s"))
    assert rc == 4 and (run / "patches" / "stacked.diff").exists()
    assert "target 0.01s not reached" in report(run)


def test_unknown_runner_still_optimizes(tmp_path, team):
    files = dict(SLOW, **{"check.sh": "%s -c 'import slow; assert slow.work() == 42'\n" % PY})
    repo = git_repo(tmp_path / "repo", files)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(
        tmp_path, measure="bash check.sh", guard="bash check.sh", focus=["slow.py"]))
    text = report(run)
    assert rc == 0, text
    assert "pass count not checked" in text


def _wf_module():
    import importlib.util
    src = pathlib.Path(__file__).resolve().parent.parent / "skill" / "local-auditor" / "lib" / \
        "workflows" / "optimize" / "workflow.py"
    spec = importlib.util.spec_from_file_location("optimize_wf_t", str(src))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeWF:
    """Just enough of wf for _recheck and _grow_stack: run_cmd answers from a queue."""
    def __init__(self, walls):
        self.walls, self.logs = list(walls), []
        self.steps = self

    def run_cmd(self, cmd, patch=None, timeout=0):
        if patch and "CONFLICT" in patch:              # does not apply: no command runs
            return {"applied": False, "rc": None, "timed_out": False, "output_tail": "error: patch failed"}
        w = self.walls.pop(0)
        ok = w is not None
        res = {"rc": 0 if ok else 1, "wall": w if ok else 0.1, "runner": "pytest", "files": None,
               "passed": 3, "failed": 0 if ok else 1, "skipped": 0}
        return {"applied": True, "rc": res["rc"], "timed_out": False,
                "output_tail": "QLA_RESULT " + json.dumps(res) + "\n"}

    def log(self, msg):
        self.logs.append(msg)


def test_one_drifting_recheck_does_not_stop_the_run():
    W = _wf_module()
    bl = {"base": 10.0, "noise": 0.2, "passed": 3}
    b = {"measure": "m"}
    assert W._recheck(FakeWF([12.0, 10.1]), b, bl, 2) is None            # drift, then fine
    assert "drifted" in W._recheck(FakeWF([12.0, 12.5]), b, bl, 2)        # drift twice
    assert W._recheck(FakeWF([None, 10.0]), b, bl, 2) is None             # one failure
    assert "fails on the unchanged tree" in W._recheck(FakeWF([None, None]), b, bl, 2)


def test_conflicting_patches_stack_only_the_first():
    W = _wf_module()
    bl = {"base": 10.0, "noise": 0.2, "passed": 3}
    st = {"stack": "", "best": 10.0, "kept": [], "patches": {"P1": "diff A\n", "P2": "CONFLICT\n"}}
    new = [{"id": "P1", "approved": True, "seconds": 5.0}, {"id": "P2", "approved": True, "seconds": 6.0}]
    fake = FakeWF([5.0, 5.0])                     # P1 stacked: two runs at 5.0; P2 never applies
    added = W._grow_stack(fake, {"measure": "m"}, bl, st, new)
    assert added and st["kept"] == ["P1"]
    assert any("dropped P2" in m for m in fake.logs)


def test_a_dropped_patch_falls_back_when_there_is_no_why():
    W = _wf_module()
    bl = {"base": 10.0, "noise": 0.2, "passed": 4}      # the stack trial passes 3 of the 4
    st = {"stack": "", "best": 10.0, "kept": [], "patches": {"P1": "diff A\n"}}
    new = [{"id": "P1", "approved": True, "seconds": 5.0}]
    assert not W._grow_stack(FakeWF([5.0, 5.0]), {"measure": "m"}, bl, st, new)
    assert new[0]["why"] == ("approved; not added to the stack: not faster than "
                             "the stack by more than the noise")


PROFILER_TWO_EDITS = r'''
import json, re
def answer(p, r):
    line = re.search(r"^\s*[\d.]+\s+test_slow\.py::test_work\s*$", p, re.M).group(0).strip()
    return 0, "```json\n" + json.dumps({"ideas": [
        {"location": "slow.py:2", "profile_line": line, "change": "lower DELAY to 0.01", "why": "sleep", "saving": "0.4s"},
        {"location": "slow.py:2", "profile_line": line, "change": "lower DELAY to 0.02", "why": "sleep", "saving": "0.4s"}],
        "outside_focus": None}) + "\n```"
'''
IMPLEMENTER_DELAY = r'''
import json, pathlib, re
def answer_cwd(p, r, cwd):
    iid = re.search(r"^- (I\d+):", p, re.M).group(1)
    new = re.search(r"lower DELAY to ([0-9.]+)", p).group(1)
    d = pathlib.Path(cwd)
    s = (d / "slow.py").read_text(encoding="utf-8").replace("DELAY = 0.4", "DELAY = %s" % new)
    (d / "slow.py").write_text(s, encoding="utf-8", newline="\n")
    return 0, "```json\n" + json.dumps({"id": iid, "done": True, "summary": "DELAY %s" % new, "note": ""}) + "\n```"
'''
REVIEWER_KEEPS_DELAY = r'''
import json, re
def answer(p, r):
    return 0, "```json\n" + json.dumps([{"patch": pid, "verdict": "keeps", "lines": ["-DELAY = 0.4"],
                                         "reason": "only the sleep constant changes"}
                                        for pid in re.findall(r"^- (P\d+) ", p, re.M)]) + "\n```"
'''


def test_a_patch_left_out_of_the_stack_says_why(tmp_path, team):
    # two approved patches that each speed measure up edit the same line: only the faster
    # one stacks; the other's diff cannot apply on top of it, and its row says why
    write_fakes(team, profiler=PROFILER_TWO_EDITS, implementer=IMPLEMENTER_DELAY,
                reviewer=REVIEWER_KEEPS_DELAY)
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path))
    assert rc == 0, report(run)
    rows = [line.split("|") for line in report(run).splitlines() if line.startswith("| P")]
    assert len(rows) == 2
    left = [r for r in rows if r[6].strip() == "-"]                     # not stacked
    assert len(left) == 1, report(run)
    assert left[0][7].strip().startswith("approved; not added to the stack:")
    cands = {c["id"]: c for c in json.loads((run / "candidates.json").read_text(encoding="utf-8"))}
    assert cands[left[0][1].strip()]["approved"] and cands[left[0][1].strip()]["seconds"] is not None


def test_measurements_section_has_no_repeats(tmp_path, team):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, target="-30%"))
    assert rc == 0, report(run)
    after = report(run).split("## Measurements", 1)[1]
    assert after.count("guard: passed on the stack") == 1
    assert after.count("Stacked:") == 1


def test_writer_role_has_no_review_round():
    data = json.loads((pathlib.Path(__file__).resolve().parent.parent / "skill" / "local-auditor" /
                       "lib" / "workflows" / "optimize" / "workflow.json").read_text(encoding="utf-8"))
    assert data["roles"]["writer"]["deep"] is False


def test_parse_review_downgrades_uncited_verdicts():
    W = _wf_module()
    patches = {"P1": "+++ b/x.py\n+DELAY = 0.1\n"}
    parse = W.parse_review(patches)
    rows = parse(json.dumps([{"patch": "P1", "verdict": "keeps", "lines": ["+DELAY = 0.1"], "reason": "r"}]),
                 [({"id": "P1"}, 0)])
    assert rows[0]["verdict"] == "supported"
    rows = parse(json.dumps([{"patch": "P1", "verdict": "keeps", "lines": ["+other"], "reason": "r"}]),
                 [({"id": "P1"}, 0)])
    assert rows[0]["verdict"] == "unclear"


def test_reviewer_prompt_treats_a_described_loosening_as_weakens():
    # a live run: three reviewers read "Loosened the one input that sets the floor:
    # target -20% -> -30%" in a summary and voted keeps. The prompt and the role text
    # must call such a patch weakens, rest it on a diff line (an uncited verdict counts
    # as unclear) and hold the summary's own words in "reason".
    W = _wf_module()
    role = (pathlib.Path(__file__).resolve().parent.parent / "skill" / "local-auditor" /
            "lib" / "workflows" / "optimize" / "roles" / "reviewer.md").read_text(encoding="utf-8")
    for text in (W.REVIEW_P, role):
        for needle in ("loosens", "skips", "removes", "widens", "a check", "threshold",
                       "target", "expected value", "weakens"):
            assert needle in text, needle
        assert "summary" in text and "diff line" in text and "reason" in text, needle


def test_checker_and_reviewer_default_to_medium_effort():
    # the bench checker and the reviewers only read and judge; they run at medium so
    # a round does not spend top effort on every review vote (--effort/--role-effort
    # still beat the manifest default, as the engine tests pin)
    from lib.swarm_engine import manifest
    m = manifest.load(pathlib.Path(__file__).resolve().parent.parent / "skill" /
                      "local-auditor" / "lib" / "workflows" / "optimize")
    assert {r: m.roles[r].effort for r in m.roles} == {
        "checker": "medium", "profiler": None, "implementer": None,
        "reviewer": "medium", "writer": None}


def test_impl_summary_keeps_1500_characters():
    W = _wf_module()
    rows = W.parse_impl(json.dumps({"id": "I1", "done": True, "summary": "s" * 2000,
                                    "note": ""}), [{"id": "I1"}], "")
    assert rows[0]["summary"] == "s" * 1500
    rows = W.parse_impl(json.dumps({"id": "I1", "done": True, "summary": "s" * 1400,
                                    "note": ""}), [{"id": "I1"}], "")
    assert rows[0]["summary"] == "s" * 1400
    for n, want in ((1500, 1500), (1501, 1500)):                # the boundary itself
        rows = W.parse_impl(json.dumps({"id": "I1", "done": True, "summary": "s" * n,
                                        "note": ""}), [{"id": "I1"}], "")
        assert len(rows[0]["summary"]) == want
    # whitespace is squashed before the clip, so newlines cost nothing to the 1500
    rows = W.parse_impl(json.dumps({"id": "I1", "done": True,
                                    "summary": "Loosened the  one\ninput", "note": ""}),
                        [{"id": "I1"}], "")
    assert rows[0]["summary"] == "Loosened the one input"
    # a long summary and a long note clip on their own limits: 1500 and 500
    rows = W.parse_impl(json.dumps({"id": "I1", "done": True,
                                    "summary": "Loosened the floor " + "y" * 1600,
                                    "note": "n" * 600}), [{"id": "I1"}], "patch")
    assert len(rows[0]["summary"]) == 1500 and len(rows[0]["note"]) == 500


def test_review_p_formats_with_a_fenced_patch():
    # the new rule text adds no braces: the prompt still formats, with a fenced
    # patch item inside
    out = _wf_module().REVIEW_P.format(
        goal="g", keep="keep", measure="m", fence="```",
        items="- P1 (idea I1, 1.00s -> 0.50s): loosened\n````diff\n+x\n````")
    assert "loosens, skips, removes or widens" in out and "quote the summary" in out


PROFILER_OUTSIDE = r'''
import json, re
def answer(p, r):
    line = re.search(r"^\s*[\d.]+\s+test_slow\.py\S*\s*$", p, re.M).group(0).strip()
    return 0, "```json\n" + json.dumps({"ideas": [
        {"location": "slow.py:2", "profile_line": line, "change": "lower DELAY", "why": "s", "saving": "1"}],
        "outside_focus": None}) + "\n```"
'''


def test_every_idea_outside_focus_stops_the_run(tmp_path, team):
    write_fakes(team, profiler=PROFILER_OUTSIDE)
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo,
                       "--set", "bench=%s" % bench_file(tmp_path, guard=MEASURE, focus=["test_slow.py"]))
    assert rc == 4
    assert report(run).splitlines()[0].startswith(
        "bench: problem (see bench-review.md): round 1: every idea was outside focus")
    assert "round 1" in (run / "bench-review.md").read_text(encoding="utf-8")
