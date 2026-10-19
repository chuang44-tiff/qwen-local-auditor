# ruff: noqa: F811
# The loop tests import the `env` fixture from test_supervisor (below) for pytest to
# find BY NAME, so every test's `env` parameter re-binds a name ruff reads as an unused
# import -- the fixture use is pytest's, invisible to a linter. F811 is off for this file.
"""The --until-done plan trigger: the supervisor hands --plan MODEL --plan-state DIR to every
coder round, copies the hook's record into the run, and reports it. The hook is
test_advisor_hook's; here tests/fake_agent.py writes the record a hook that fired would have."""
import json
import os

from lib import supervisor

from test_supervisor import (PY, FAKE, _raise_on_call, _report_text, _run_dir, calls,  # noqa: E402,F401
                             env, script, sup)
from test_supervisor_rescue import _calls_log, _sup_audited, stub_advice  # noqa: E402,F401

REC = {"called": True, "skipped_reason": None, "cost_usd": 0.12, "tool": "Edit",
       "path": "value.txt", "digest_chars": 900, "cut": False, "advice": "Use the word good.",
       "round": 1}


def plan_calls(tmp):
    p = tmp / "record.jsonl.plan"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def test_every_coder_round_gets_the_plan_and_the_audit_does_not(env):
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "plan_record": REC,
                  "plan_files": {"plan.md": "THE BRIEF", "plan-digest.md": "THE DIGEST"}},
                 {"result": "fixed", "write": {"value.txt": "good\n"}},
                 {"result": "NO CONTRADICTIONS"}])
    assert _sup_audited(repo, task, "--plan", "opus", "--max-rounds", "2") == 0
    c = calls(tmp)
    coder = [a for a in c if a[a.index("-r") + 1] == "coder"]
    audit = [a for a in c if a[a.index("-r") + 1] == "auditor"]
    assert len(coder) == 2 and len(audit) == 1
    dirs = {flag(a, "--plan-state") for a in coder}
    assert len(dirs) == 1 and None not in dirs                 # one dir for the whole run
    for a in coder:
        assert flag(a, "--plan") == "opus"
    assert "--plan" not in audit[0] and "--plan-state" not in audit[0]
    rounds = plan_calls(tmp)
    assert [r["round"] for r in rounds] == ["1", "2"] and all(r["task"] for r in rounds)
    [rec] = _calls_log(tmp)
    assert (rec["call"], rec["round"], rec["reply"], rec["called"]) == ("plan", 1, "tried", True)
    text = _report_text(tmp)
    assert "plan call: round 1, Edit value.txt, digest 900 chars (uncut), $0.12" in text
    assert "## Plan" in text and "Use the word good." in text and "switches: plan" in text
    assert "## Claude calls" not in text                           # --plan alone is not --advisor
    run_dir = _run_dir(tmp)
    assert (run_dir / "plan.md").read_text() == "THE BRIEF"
    assert (run_dir / "plan-digest.md").read_text() == "THE DIGEST"
    assert not os.path.exists(next(iter(dirs)))                # the plan dir went with the run


def test_plan_record_comes_before_the_failure_record_and_is_not_counted_as_one(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "plan_record": REC},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--rescue", "opus", "--max-rounds", "2") == 0
    log = _calls_log(tmp)
    assert [(r["call"], r["round"]) for r in log] == [("plan", 1), ("rescue", 1)]
    assert log[0]["reply"] == "tried" and log[1]["reply"] == "fixed"
    c = calls(tmp)
    assert "--advisor-state" not in c[0] and flag(c[0], "--plan-state")
    text = _report_text(tmp)
    assert "rescue calls: 1" in text and "rescue skipped: 0" in text
    assert text.count("- round 1: asked") == 1                 # the plan call is not listed there
    assert "## Plan" in text and "## Claude calls" in text


def test_plan_only_run_has_no_rescue_lines(env):
    repo, task, tmp = env
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}, "plan_record": REC}])
    assert sup(repo, task, "--plan", "opus") == 0
    text = _report_text(tmp)
    assert "rescue calls" not in text and "### Rescue after failing rounds" not in text


def test_run_that_never_writes_reports_the_plan_as_not_called(env):
    repo, task, tmp = env
    script(tmp, [{"result": "a"}, {"result": "b", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--max-rounds", "2") == 0
    text = _report_text(tmp)
    assert "plan: not called (no write)" in text and "## Plan" in text and "called: no" in text
    assert _calls_log(tmp) == []
    state = flag(calls(tmp)[0], "--plan-state")
    assert state and not os.path.exists(state)                 # removed after the report


def test_skipped_plan_call_is_noted_and_the_run_goes_on(env):
    repo, task, tmp = env
    skip = dict(REC, called=False, advice=None, cost_usd=None,
                skipped_reason="Unavailable: no answer within 600s")
    script(tmp, [{"result": "tried", "plan_record": skip},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--max-rounds", "2") == 0
    text = _report_text(tmp)
    assert "plan skipped: Unavailable: no answer within 600s" in text
    assert "plan: not called (Unavailable: no answer within 600s)" in text
    [rec] = _calls_log(tmp)
    assert rec["called"] is False and rec["reply"] == "tried"


def test_round_that_times_out_in_the_plan_call_is_noted_and_resumed(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 5, "result": "", "plan_record": REC},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--max-rounds", "2") == 0
    c = calls(tmp)
    assert len(c) == 2 and flag(c[1], "--resume") == "s1"
    assert "plan call ran into the round timeout" in _report_text(tmp)
    assert len(_calls_log(tmp)) == 1                         # no second plan record


def test_plan_hook_killed_mid_call_is_noted_and_reported(env):
    # advisor_mcp.ask writes the counter before it launches claude: a hook killed by the
    # round timeout leaves a count and no record.
    repo, task, tmp = env
    script(tmp, [{"rc": 5, "result": "", "plan_count": 1},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--max-rounds", "2") == 0
    [rec] = _calls_log(tmp)
    assert rec["called"] is False and "timed out inside the plan call" in rec["skipped_reason"]
    text = _report_text(tmp)
    assert "plan: not called (the round timed out inside the plan call)" in text
    assert "plan skipped: the round timed out inside the plan call" in text


def test_plan_hook_that_left_no_record_is_reported(env):
    repo, task, tmp = env
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}, "plan_count": 1}])
    assert sup(repo, task, "--plan", "opus") == 0
    [rec] = _calls_log(tmp)
    assert "left no record" in rec["skipped_reason"]


def test_probe_rounds_get_a_plan_dir_outside_the_sandbox(env):
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "plan_record": REC},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--probe", "--max-rounds", "2") == 0
    c = calls(tmp)
    assert all("--probe-here" in a and "--plan-state" in a for a in c)
    state = os.path.realpath(flag(c[0], "--plan-state"))
    work = os.path.realpath(flag(c[0], "-C"))
    run_dir = os.path.realpath(str(_run_dir(tmp)))
    for inside in (work, run_dir, os.path.realpath(str(repo))):
        assert not (state + os.sep).startswith(inside + os.sep)


def test_plan_dir_is_removed_on_keyboard_interrupt(env, monkeypatch):
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "plan_record": REC}, {"result": "fixed"}])
    _raise_on_call(monkeypatch, 2, KeyboardInterrupt())
    assert sup(repo, task, "--plan", "opus") == 130
    state = flag(calls(tmp)[0], "--plan-state")
    assert not os.path.exists(state), "the interrupted run left its plan dir behind"
    text = _report_text(tmp)
    assert "stop: interrupted (exit 130)" in text and "plan call: round 1, Edit value.txt" in text


def test_interrupt_during_the_round_that_made_the_plan_call_still_reports_it(env, monkeypatch):
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "plan_record": REC}])
    real = supervisor.call_agent

    def interrupted(*a, **k):
        real(*a, **k)                      # the round ran and the hook wrote its record ...
        raise KeyboardInterrupt            # ... then Ctrl-C landed before the supervisor read it
    monkeypatch.setattr(supervisor, "call_agent", interrupted)
    assert sup(repo, task, "--plan", "opus") == 130
    text = _report_text(tmp)
    assert "plan call: round 1, Edit value.txt" in text
    [rec] = _calls_log(tmp)
    assert rec["reply"] is None
    assert not os.path.exists(flag(calls(tmp)[0], "--plan-state"))


def test_plan_dir_is_removed_when_a_round_raises(env, monkeypatch):
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "plan_record": REC}, {"result": "fixed"}])
    _raise_on_call(monkeypatch, 2, RuntimeError("the round died"))
    assert sup(repo, task, "--plan", "opus", "--probe") == 8
    assert not os.path.exists(flag(calls(tmp)[0], "--plan-state"))
    assert "error: RuntimeError" in _report_text(tmp)


def test_first_round_timeout_is_raised_by_the_advisor_timeout_and_round_two_is_not(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.delenv("QWEN_TIMEOUT", raising=False)
    script(tmp, [{"result": "tried", "plan_record": REC},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--max-rounds", "2") == 0
    c = calls(tmp)
    assert flag(c[0], "--timeout") == str(1800 + 600)
    assert "--timeout" not in c[1]                  # the plan call is spent: the round's own timeout


def test_raised_timeout_adds_a_typed_timeout_and_the_advisor_timeout_env(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_ADVISOR_TIMEOUT", "100")
    script(tmp, [{"result": "tried", "plan_record": REC},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--max-rounds", "2", "--", "--timeout", "900") == 0
    c = calls(tmp)
    assert c[0].count("--timeout") == 2 and c[0][-2:] == ["--timeout", "1000"]    # raised: last, wins
    assert c[1].count("--timeout") == 1 and flag(c[1], "--timeout") == "900"      # the typed one


def test_timeout_stays_raised_while_nothing_has_been_written(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_TIMEOUT", "3600")
    script(tmp, [{"result": "a"}, {"result": "b", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--max-rounds", "2") == 0
    assert [flag(a, "--timeout") for a in calls(tmp)] == ["4200", "4200"]


def test_no_timeout_runs_are_left_alone(env):
    repo, task, tmp = env
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus", "--", "--no-timeout") == 0
    assert "--timeout" not in calls(tmp)[0]


def test_round_timeout_reads_typed_then_environment_then_default(monkeypatch):
    monkeypatch.delenv("QWEN_TIMEOUT", raising=False)
    assert supervisor._round_timeout([]) == 1800
    monkeypatch.setenv("QWEN_TIMEOUT", "3600")
    assert supervisor._round_timeout(["--shallow"]) == 3600
    assert supervisor._round_timeout(["--timeout", "900", "--timeout=700"]) == 700
    assert supervisor._round_timeout(["--timeout", "900", "--no-timeout"]) is None
    assert supervisor._round_timeout(["--timeout", "soon"]) is None


def test_without_plan_nothing_about_plan_is_passed(env):
    repo, task, tmp = env
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    assert all("--plan" not in a and "--plan-state" not in a and "--timeout" not in a for a in calls(tmp))
    assert plan_calls(tmp) == []
    text = _report_text(tmp)
    assert "## Plan" not in text and "plan:" not in text


def test_audit_round_gets_no_advisor_flag(env):
    repo, task, tmp = env
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}},
                 {"result": "NO CONTRADICTIONS"}])
    assert _sup_audited(repo, task, "--advisor", "opus", "--max-rounds", "2") == 0
    audit = [a for a in calls(tmp) if a[a.index("-r") + 1] == "auditor"]
    assert len(audit) == 1 and "--advisor" not in audit[0]
