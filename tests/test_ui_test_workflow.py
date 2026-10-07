"""The built-in `ui-test` workflow: a scripted UI suite run as a swarm of browser agents.

The suite file comes from the `scenarios` knob, its scenarios are dealt one per unit, and
every answer is scored by lib/scenarios.py -- the one module that also writes the prompt. The
fake tester plays its scenarios back as PASS unless FAKE_UI_FAIL names ids to fail, and for
FAKE_UI_NOBLOCK it answers in prose with no json block at all, which is the scoring module's
own route to BLOCKED: the workflow never has to trust a tester's self-report. The tester also
quotes back the browser folder it was given, so the per-unit evidence root is read off the run
rather than off the source. Two further fakes cover the routes that never reach an answer at
all: CRASHER exits 1 for FAKE_UI_CRASH ids, so the dropped unit's own reason must surface as
the scenario's note, and SLOW sleeps past a tiny --hours deadline, so the scenarios whose
units never start come back NOT RUN -- counted apart from BLOCKED and named for the --resume
that will run them.
"""
import json
import pathlib
import re
import sys

import test_swarm_runner
from lib import swarm
from lib.swarm_engine import fences, runner
from swarm_fixtures import FAKE, agent_args, calls, unit_names

fake = test_swarm_runner.fake
_restore_signal_handlers = test_swarm_runner._restore_signal_handlers
GOAL = "the cart suite"

TESTER = r'''
import json, os, re

def answer(prompt, resumed):
    ids = re.findall(r"^Scenario (\S+):", prompt, re.M)
    if any(sid in os.environ.get("FAKE_UI_NOBLOCK", "").split(",") for sid in ids):
        return 0, "The browser tool kept timing out, so nothing could be observed."
    out = []
    for sid in ids:
        failed = sid in os.environ.get("FAKE_UI_FAIL", "").split(",")
        out.append({"id": sid, "status": "FAIL" if failed else "PASS",
                    "failed_expectations": ["the cart badge shows 1"] if failed else [],
                    "evidence": ["cart-open.png"],
                    "notes": "browser folder %s" % os.environ.get("QWEN_BROWSER_DIR")})
    return 0, "I ran the scenario.\n\n```json\n" + json.dumps({"results": out}) + "\n```"
'''

# a tester that dies instead of answering: no json block, no result, just exit 1
CRASHER = r'''
import json, os, re

def answer(prompt, resumed):
    ids = re.findall(r"^Scenario (\S+):", prompt, re.M)
    if any(sid in os.environ.get("FAKE_UI_CRASH", "").split(",") for sid in ids):
        return 1, ""
    out = [{"id": sid, "status": "PASS", "failed_expectations": [], "evidence": ["cart-open.png"],
            "notes": "browser folder %s" % os.environ.get("QWEN_BROWSER_DIR")} for sid in ids]
    return 0, "I ran the scenario.\n\n```json\n" + json.dumps({"results": out}) + "\n```"
'''

# a tester slower than a tiny --hours deadline: the first scenario is still running when the
# deadline passes, so every unit queued behind it never starts
SLOW = r'''
import json, os, re, time

def answer(prompt, resumed):
    time.sleep(float(os.environ.get("FAKE_UI_SLOW", "2")))
    ids = re.findall(r"^Scenario (\S+):", prompt, re.M)
    out = [{"id": sid, "status": "PASS", "failed_expectations": [], "evidence": ["cart-open.png"],
            "notes": "browser folder %s" % os.environ.get("QWEN_BROWSER_DIR")} for sid in ids]
    return 0, "I ran the scenario.\n\n```json\n" + json.dumps({"results": out}) + "\n```"
'''


def suite_file(tmp_path, n=3, name="suite.md"):
    """A suite of `n` scenarios (ids s1..sn) written at tmp_path/<name>; its path."""
    text = "# Suite: Cart page\nbase: http://127.0.0.1:8501\n"
    for i in range(1, n + 1):
        text += ("\n## Scenario: Scenario %d\nid: s%d\nsteps:\n1. Open the page\n"
                 "2. Click thing %d\nexpect:\n- Thing %d happened\n" % (i, i, i, i))
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def ui_run(fake, suite, out, *extra, behavior=TESTER):
    """`qwen-swarm ui-test GOAL --set scenarios=SUITE --out OUT` with the fake tester."""
    (fake / "tester.py").write_text(behavior, encoding="utf-8")
    return runner.main(agent_args() + ["ui-test", GOAL, "--set", "scenarios=" + suite,
                                       "--out", str(out)] + list(extra))


def prompts(out):
    """{unit: the prompt its agent was handed} of one run."""
    return {p.name.split(".")[0]: p.read_text(encoding="utf-8")
            for p in (pathlib.Path(out) / "agents").glob("*.prompt.md")}


# ------------------------------------------------------------------ the browser fence

def test_browser_fence_flags_and_env(tmp_path, fake, capsys):
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 0
    assert "roles: tester=browser" in capsys.readouterr().err
    argv = calls(fake)
    assert len(argv) == 2
    for a in argv:
        assert "--browser" in a                                  # the fence's one flag
        assert "--mcp-config" not in a                           # qwen-agent writes its own
        assert a[a.index("--toolset") + 1] == "none"             # the browser tools are --browser's
        assert a[a.index("-C") + 1].startswith(str(out / "agents"))
    assert not (out / "mcp.json").exists()                       # not a web fence: no search config
    # every unit was given its own evidence root under the run folder, and its answer says so
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [r["unit"] for r in rows] == ["scenario-1", "scenario-2"]
    assert [r["notes"] for r in rows] == [
        "browser folder %s" % (out / "browser" / r["unit"]) for r in rows]


def test_browser_fence_key_differs_and_other_keys_unchanged(tmp_path, fake):
    role = tmp_path / "tester.md"
    role.write_text("role", encoding="utf-8")
    sw = swarm.Swarm([sys.executable, str(FAKE)], tmp_path / "keys", seats=1, timeout=60,
                     backoff=0)

    def unit(fence, **over):
        return swarm.Unit(name="u-1", role_file=role, prompt="p",
                          **dict(fences.unit_fields(fence), **over))

    assert "browser" not in fences.unit_fields("none")           # no other fence carries the flag
    plain, browsed = sw._key(unit("none")), sw._key(unit("browser"))
    assert plain != browsed                                      # the flag moved this unit's key
    assert browsed == sw._key(unit("none", browser=True))        # browser is exactly none + --browser
    assert plain == sw._key(unit("none", env={"QWEN_BROWSER_DIR": "/somewhere/else"}))
    for fence in ("read", "sandbox"):
        assert sw._key(unit(fence)) != browsed                   # and no other fence lands on it
    # hashed the same way every time: a resume of a browser suite re-runs no agent at all
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 0
    first = calls(fake)
    assert len(first) == 2
    assert runner.main(agent_args() + ["--resume", str(out)]) == 0
    assert calls(fake)[len(first):] == []


# -------------------------------------------------------------------- the workflow

def test_ui_test_check_passes(capsys):
    assert "ui-test" in runner.builtin_names()                   # --list and --check know it
    assert runner.main(agent_args() + ["--check", "ui-test"]) == 0
    assert capsys.readouterr().out.startswith("ok: ui-test: manifest valid")


def test_ui_test_report_title(tmp_path, fake):
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 1), out) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# UI test: Cart page\n")           # the suite names the report
    assert "UI test report" not in report


def test_ui_test_one_unit_per_scenario_and_report(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")                     # the tester's own FAIL
    monkeypatch.setenv("FAKE_UI_NOBLOCK", "s3")                  # no report at all: scored BLOCKED
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 3), out) == 4       # not every scenario passed
    assert sorted(unit_names(fake)) == ["scenario-1", "scenario-2", "scenario-3"]
    got = prompts(out)
    assert sorted(got) == ["scenario-1", "scenario-2", "scenario-3"]
    # one scenario per agent: its prompt carries that scenario's id, title, steps and
    # expectations, the suite's base URL and the reporting contract, and no other scenario
    assert [re.findall(r"^Scenario (\S+):", t, re.M) for _, t in sorted(got.items())] \
        == [["s1"], ["s2"], ["s3"]]
    assert "Base URL: http://127.0.0.1:8501" in got["scenario-1"]
    assert "Steps:" in got["scenario-1"] and "Expectations:" in got["scenario-1"]
    assert "one entry per scenario id" in got["scenario-1"]
    # the role is qwen-agent's own tester method, with the one-scenario rule stated outright
    role = (runner.BUILTIN / "ui-test" / "roles" / "tester.md").read_text(encoding="utf-8")
    assert "You run exactly one scenario." in role
    assert "repeat a key flow with any dark or alternate theme the app offers" in role
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [(r["id"], r["status"]) for r in rows] == [("s1", "PASS"), ("s2", "FAIL"),
                                                      ("s3", "BLOCKED")]
    # s3's agent answered without any json block: scenarios.py scores that BLOCKED, whatever
    # the tester said about it, and its unit is still the one that ran it
    assert rows[2]["notes"] == "no result block" and rows[2]["unit"] == "scenario-3"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "PASS 1 / FAIL 1 / BLOCKED 1" in report
    for unit in ("scenario-1", "scenario-2", "scenario-3"):
        assert str(out / "browser" / unit) in report             # each unit's evidence folder
    assert "the cart badge shows 1" in report                    # what the tester said broke
    assert "no result block" in report
    assert "finished without its goal: 2 of 3 scenarios did not pass" in capsys.readouterr().err


def test_ui_test_all_pass_exit_0(tmp_path, fake, capsys):
    out = tmp_path / "run"
    suite = suite_file(tmp_path, 3)
    assert ui_run(fake, suite, out, "--set", "base=http://localhost:9999") == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == str(out / "report.md")
    assert "without its goal" not in captured.err and "dropped" not in captured.err
    # the goal only names the run; --set base points the suite at another deployment, and
    # prompt and report both say so
    assert (out / "goal.md").read_text(encoding="utf-8") == GOAL + "\n"
    assert "Base URL: http://localhost:9999" in prompts(out)["scenario-1"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "PASS 3 / FAIL 0 / BLOCKED 0 / NOT RUN 0" in report
    assert "`http://localhost:9999`" in report
    # a PASS is evidence-backed in the report too, not just its status
    assert report.count("evidence: cart-open.png") == 3
    assert suite in report and json.loads((out / "suite.json").read_text(encoding="utf-8"))[
        "suite"] == "Cart page"


def test_ui_test_one_agent_per_scenario_pinned(tmp_path, fake):
    # --max-agents 1 with three scenarios: one agent per wave, and every prompt still holds
    # exactly one scenario -- a unit ever handed two would score one answer against both
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 3), out, "--max-agents", "1") == 0
    assert unit_names(fake) == ["scenario-1", "scenario-w2-1", "scenario-w3-1"]
    assert {u: re.findall(r"^Scenario (\S+):", t, re.M) for u, t in prompts(out).items()} == {
        "scenario-1": ["s1"], "scenario-w2-1": ["s2"], "scenario-w3-1": ["s3"]}
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [(r["id"], r["status"], r["unit"]) for r in rows] == [
        ("s1", "PASS", "scenario-1"), ("s2", "PASS", "scenario-w2-1"),
        ("s3", "PASS", "scenario-w3-1")]


def test_ui_test_failed_unit_reason(tmp_path, fake, monkeypatch, capsys):
    # a tester that dies with exit 1 is dropped without retry and without a verdict: its
    # scenario is BLOCKED with the very reason the swarm recorded for the unit
    monkeypatch.setenv("FAKE_UI_CRASH", "s2")
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 3), out, behavior=CRASHER) == 4
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [r["status"] for r in rows] == ["PASS", "BLOCKED", "PASS"]
    s2 = rows[1]
    assert s2["notes"].startswith("agent failed: ")
    assert "exit 1" in s2["notes"]                               # the unit's own why, not a generic line
    assert s2["unit"] == "scenario-2"                             # it ran -- and died -- in its own unit
    report = (out / "report.md").read_text(encoding="utf-8")
    assert s2["notes"] in report                                  # and the report repeats it
    assert "1 agent(s) dropped" in capsys.readouterr().err


def test_ui_test_scenarios_the_deadline_left_unrun_are_not_run(tmp_path, fake):
    # an --hours so short the run is already past its deadline when the suite is dealt: no
    # unit starts, and every scenario is still reported, NOT RUN with that reason
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out, "--hours", "1e-9") == 4
    assert calls(fake) == []
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [(r["id"], r["status"], r["unit"]) for r in rows] == [("s1", "NOT RUN", None),
                                                                 ("s2", "NOT RUN", None)]
    assert rows[0]["notes"] == "deadline: not started; --resume runs it"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "PASS 0 / FAIL 0 / BLOCKED 0 / NOT RUN 2" in report
    assert "deadline: not started; --resume runs it" in report and "*no unit ran it*" in report


def test_ui_test_deadline_scenarios_are_not_run(tmp_path, fake, monkeypatch, capsys):
    # one seat and a slow tester: the first scenario is still running when the tiny --hours
    # deadline passes, so the units queued behind it never start -- NOT RUN, counted apart
    # from BLOCKED, with no browser folder for a unit that never ran, and the unmet goal
    # names how many the deadline left
    monkeypatch.setenv("FAKE_UI_SLOW", "2")
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 3), out, "--seats", "1", "--hours", "0.0005",
                  behavior=SLOW) == 4
    assert unit_names(fake) == ["scenario-1"]                    # only the first unit ever started
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [(r["id"], r["status"], r["unit"]) for r in rows] == [
        ("s1", "PASS", "scenario-1"), ("s2", "NOT RUN", None), ("s3", "NOT RUN", None)]
    assert rows[1]["notes"] == "deadline: not started; --resume runs it"
    assert rows[2]["notes"] == "deadline: not started; --resume runs it"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "PASS 1 / FAIL 0 / BLOCKED 0 / NOT RUN 2" in report   # NOT RUN counted separately
    assert "deadline: not started; --resume runs it" in report
    assert str(out / "browser" / "scenario-1") in report         # the unit that ran names its folder
    assert str(out / "browser" / "scenario-2") not in report     # the ones that never ran name none
    assert "*no unit ran it*" in report
    assert "finished without its goal: 2 of 3 scenarios did not pass" in capsys.readouterr().err


def test_ui_test_relative_scenarios_path(tmp_path, fake, monkeypatch):
    # the knob given relative to the cwd: what config.json stores and the report prints is
    # the absolute file, so both name it from whatever cwd a later --resume runs from
    monkeypatch.chdir(tmp_path)
    suite = str((tmp_path / "suite.md").resolve())
    suite_file(tmp_path, 2)
    out = tmp_path / "run"
    (fake / "tester.py").write_text(TESTER, encoding="utf-8")
    assert runner.main(agent_args() + ["ui-test", GOAL, "--set", "scenarios=suite.md",
                                       "--out", str(out)]) == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["scenarios"] == suite                             # absolute, the file that ran
    assert cfg["summary"]["knobs"]["scenarios"] == suite
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "Scenario file: `%s`" % suite in report


def test_ui_test_bad_file_fails_cleanly(tmp_path, fake, capsys):
    bad = tmp_path / "bad.md"
    bad.write_text("# Suite: Broken\n\n## Scenario: No steps\nexpect:\n- a thing\n",
                   encoding="utf-8")
    out = tmp_path / "run"
    for spec in ([GOAL, "--set", "scenarios=" + str(bad)],                 # does not parse
                 [GOAL, "--set", "scenarios=" + str(tmp_path / "nope.md")],  # not there
                 [GOAL]):                                             # no knob at all
        assert runner.main(agent_args() + ["ui-test"] + spec + ["--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "line 3: scenario 'No steps' has no steps" in err      # the suite's own line
    assert "cannot read" in err and "no scenario file given" in err
    assert not out.exists() and calls(fake) == []                 # refused before anything ran
