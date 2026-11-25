# ruff: noqa: F811
"""Stage 1.5 (spec 10.4): the until-done supervisor APPLIES the checklist amendments the
main session queued with qwen-agent --amend (spec 10.4). apply_pending runs at TWO
points of every round -- right before the round prompt is assembled (before the
notes are drained) and immediately before the checks_start emit, so a check run
always uses the newest task and an amendment that lands mid-check-run waits for the
next point. An applied amendment swaps task_text/task, rewrites RUN/task.md (and the
--plan copy while the plan call is still unspent), clears the no-progress signature,
tells the coder in the NEXT resumed prompt (once, next to the drained notes), emits
the amend line to events.jsonl and lands in report.md's ## Checklist amendments
section; a rejected one says only `until-done: amendment rejected (<reason>)` and the
enum reason, and the run goes on.

Driven on test_supervisor's harness (env/script/sup) with the page on --
test_supervisor_asks' `page` fixture, XDG_CACHE_HOME under tmp_path for every test
(autouse below), messaging on unless a test says otherwise. The amendment is planted
by the fake agent's run_write of amend/pending.md; a live lease is not needed for
amendments. The mid-check-run cases plant during a monkeypatched checks.run_checks,
which starts only after that round's checks_start line."""
import json
import os
import re
from pathlib import Path

import pytest

from lib import supervisor
from test_supervisor import calls, env, script, sup  # noqa: E402,F401
from test_supervisor_asks import (log_events, loop_events, page,  # noqa: E402,F401
                                  report_of, run_dir_of)
from test_supervisor_rescue import _sup_audited  # noqa: E402,F401

# The task file the env fixture writes (test 6 pins that), and the amendment bodies.
TASK_ONE = "# Goal\nMake the value good.\n\n- [ ] value is good -- check: test ALL\n"
AMEND_ADDED = TASK_ONE + "- [ ] the follow-up is noted -- check: none\n"
AMEND_GOAL = ("# Goal\nZZZAMENDEDGOALZZZ make it shine.\n\n"
              "- [ ] value is good -- check: test ALL\n")
# The same single item with its check relaxed to none: an UNVERIFIED item is not a
# FAIL, so a round judged on THIS checklist is done where the original one fails.
AMEND_RELAX = "# Goal\nMake the value good.\n\n- [ ] value is good -- check: none\n"
BAD_TASK = "# Goal\nprose only, no checklist anywhere\n"
CODER_HEAD = "The main session changed the task's checklist (amendment 1):"


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path, monkeypatch):
    """The file's rules (test_supervisor_asks' twin): XDG_CACHE_HOME at tmp_path/cache
    -- the loop's pointer never reaches the real watch registry -- and a developer's
    QWEN_STREAM or QWEN_ASK_LEASE never decides what the test sees (messaging on:
    apply_amend acts only when the asks layout is open and the loop is on the page).
    The register() ownership trio goes too: register() writes QWEN_WATCH_CHILD/
    PARENT/ID straight into os.environ (lib/watch.py), a previous test's run may have
    left them there, and a stale child switch makes register() answer None -- which
    would silently turn asks_on (and with it every amendment) off. Every module that
    NEEDS a registration wipes these at setup (test_watch, test_watch_hooks,
    test_watch_titles); the page fixture's teardown pop alone does not cover a
    registration that never happened, nor a poison from an earlier module."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)
    for _own in ("QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT", "QWEN_WATCH_ID"):
        monkeypatch.delenv(_own, raising=False)


# ------------------------------------------------------------------- helpers

def prompt_of(call):
    with open(call[call.index("-f") + 1], encoding="utf-8") as fh:
        return fh.read()


def amend_dir(run, leaf=None):
    p = os.path.join(str(run), "amend")
    return p if leaf is None else os.path.join(p, leaf)


def listed(path):
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def task_md(run):
    with open(os.path.join(str(run), "task.md"), encoding="utf-8") as fh:
        return fh.read()


def events_named(run, kind):
    return [e for e in loop_events(run) if e["kind"] == kind]


def raw(path):
    with open(path, "rb") as fh:
        return fh.read()


def plan_records(tmp):
    p = tmp / "record.jsonl.plan"
    return ([json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]
            if p.exists() else [])


def plant_during_checks(tmp, monkeypatch, text):
    """A checks.run_checks that lands amend/pending.md in the RUN folder the FIRST
    time the checks run -- after that round's checks_start line was already emitted,
    so the amendment arrives mid-check-run and can only be applied at the NEXT
    point, never to the running check run."""
    real = supervisor.checks.run_checks
    fired = []

    def wrapper(items, repo, **kw):
        if not fired:
            fired.append(1)
            run = next((tmp / "state").rglob("round-1.json")).parent
            d = amend_dir(run)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "pending.md"), "w", encoding="utf-8",
                      newline="\n") as fh:     # no \r\n on Windows
                fh.write(text)
        return real(items, repo, **kw)
    monkeypatch.setattr(supervisor.checks, "run_checks", wrapper)


# ------------------------------------------------------------------- the apply

def test_amend_applied_at_next_check_run(env, page, tmp_path, capsys):
    # The amendment the round plants is applied at the check-run point: THIS
    # round's checks already run on the amended checklist (the second item shows
    # up in the round's own events), the applied bytes and the rewritten task.md
    # stand under RUN/amend/ and RUN/, and report.md and stdout both say so.
    repo, task, tmp = env
    script(tmp, [{"result": "planted", "write": {"value.txt": "good\n"},
                  "run_write": {"amend/pending.md": AMEND_ADDED}}])
    assert sup(repo, task) == 0
    assert len(calls(tmp)) == 1
    run = run_dir_of(tmp)
    assert listed(amend_dir(run, "applied")) == ["1.md"]
    assert listed(amend_dir(run, "rejected")) == []
    assert task_md(run) == AMEND_ADDED                    # temp + os.replace rewrite
    assert log_events(run) == ["amend_applied"]
    amends = events_named(run, "amend")
    assert len(amends) == 1
    a = amends[0]
    assert (a["k"], a["round"], a["added"], a["removed"], a["changed"]) == (1, 1, 1, 0, 0)
    assert [e["total"] for e in events_named(run, "checks_start")] == [2]
    ck = events_named(run, "checks")[0]
    assert (ck["total"], ck["passed"], ck["failed"], ck["unverified"]) == (2, 1, 0, 1)
    report = report_of(run)
    assert "## Checklist amendments" in report
    assert "### Amendment 1 (" in report
    assert "```diff\n--- task.md (before)\n+++ task.md (amendment 1)" in report
    assert "+- [ ] the follow-up is noted -- check: none" in report
    assert "| 2 | the follow-up is noted | UNVERIFIED |" in report   # the amended list
    assert "until-done: checklist amended 1 time(s); see report" in capsys.readouterr().out


def test_amend_diff_reaches_next_prompt(env, page, tmp_path):
    # The coder is told what changed exactly once: the applied diff block rides
    # into the NEXT resumed prompt (after the NOT_DONE feedback, where the drained
    # notes ride too) and is gone from every prompt after that.
    repo, task, tmp = env
    script(tmp, [{"result": "a", "run_write": {"amend/pending.md": AMEND_ADDED}},
                 {"result": "b", "write": {"value.txt": "meh\n"}},
                 {"result": "c", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    c = calls(tmp)
    assert len(c) == 3
    p1, p2, p3 = (prompt_of(x) for x in c)
    assert CODER_HEAD not in p1                           # FIRST: whole task, no block
    assert "Not done" in p2 and p2.count(CODER_HEAD) == 1
    assert p2.index("Not done") < p2.index(CODER_HEAD)    # appended, next to the notes
    assert "+- [ ] the follow-up is noted -- check: none" in p2
    assert "Not done" in p3 and CODER_HEAD not in p3      # once, then cleared


def test_amend_resets_no_progress(env, page, tmp_path):
    # Two rounds that change nothing and fail the same item stop the loop with 12
    # (test_supervisor.test_same_failures_twice_is_no_progress). An amendment that
    # lands in between -- and changes only the goal line, so the failing set and
    # the tree signature are IDENTICAL -- clears prev: the stop cannot fire across
    # it, and the run goes on to finish.
    repo, task, tmp = env
    script(tmp, [{"result": "a"},
                 {"result": "b", "run_write": {"amend/pending.md": AMEND_GOAL}},
                 {"result": "c", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0                           # not the 12 of no progress
    assert len(calls(tmp)) == 3
    run = run_dir_of(tmp)
    assert [(e["k"], e["round"]) for e in events_named(run, "amend")] == [(1, 2)]
    report = report_of(run)
    assert "no progress" not in report
    assert "### Amendment 1 (" in report


def test_amend_during_last_round_judges_it(env, page, tmp_path, capsys):
    # --max-rounds 1: the only round is the last one. It fails the original item
    # (value.txt stays bad) and plants an amendment that relaxes the check -- an
    # UNVERIFIED item is no FAIL, so the last round is DONE only because the
    # check-run point applied the amendment before the checks judged it. Without
    # the apply this run ends with 11, the round limit.
    repo, task, tmp = env
    script(tmp, [{"result": "planted", "run_write": {"amend/pending.md": AMEND_RELAX}}])
    assert sup(repo, task, "--max-rounds", "1") == 0
    run = run_dir_of(tmp)
    assert (repo / "value.txt").read_text() == "bad\n"    # never fixed: it passed
    assert [e["total"] for e in events_named(run, "checks_start")] == [1]
    ck = events_named(run, "checks")[0]
    assert (ck["total"], ck["passed"], ck["failed"], ck["unverified"]) == (1, 0, 0, 1)
    amends = events_named(run, "amend")
    assert [(e["k"], e["round"]) for e in amends] == [(1, 1)]
    assert (amends[0]["added"], amends[0]["removed"], amends[0]["changed"]) == (0, 0, 1)
    report = report_of(run)
    assert "stop: done (exit 0)" in report
    assert "| 1 | value is good | UNVERIFIED |" in report
    assert "### Amendment 1 (" in report
    assert "until-done: checklist amended 1 time(s); see report" in capsys.readouterr().out


def test_amend_mid_check_run_applies_to_next_check_run_only(env, page, tmp_path,
                                                             monkeypatch):
    # The pending.md lands AFTER round 1's checks_start line, inside the check run:
    # that run judged the OLD checklist (one item, its own line and events), and
    # the amendment is applied at the next point -- round 2's prompt-assembly one --
    # so round 2's checks_start carries the new count and the coder is told first.
    repo, task, tmp = env
    plant_during_checks(tmp, monkeypatch, AMEND_ADDED)
    script(tmp, [{"result": "a"},
                 {"result": "b", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    run = run_dir_of(tmp)
    assert [e["total"] for e in events_named(run, "checks_start")] == [1, 2]
    ck1, ck2 = events_named(run, "checks")
    assert (ck1["total"], [i["text"] for i in ck1["items"]]) == (1, ["value is good"])
    assert ck2["total"] == 2
    assert [(e["k"], e["round"]) for e in events_named(run, "amend")] == [(1, 2)]
    assert CODER_HEAD in prompt_of(calls(tmp)[1])         # told BEFORE the amended run
    assert task_md(run) == AMEND_ADDED
    assert log_events(run) == ["amend_applied"]


# ------------------------------------------------------------------- rejections

def test_amend_rejected_noop_and_parse_error_continue(env, page, tmp_path, capsys):
    # A byte-identical pending.md is a noop and a checklist-less one a parse_error:
    # each costs one stderr line naming the enum reason and one amend_rejected
    # event, and the run goes on (the third round fixes the value: exit 0). The
    # rejection never repeats parser text or task lines.
    repo, task, tmp = env
    assert task.read_text(encoding="utf-8") == TASK_ONE   # the noop's exact twin
    script(tmp, [{"result": "a", "run_write": {"amend/pending.md": TASK_ONE}},
                 {"result": "b", "write": {"value.txt": "meh\n"},
                  "run_write": {"amend/pending.md": BAD_TASK}},
                 {"result": "c", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0                           # two rejections, run goes on
    assert len(calls(tmp)) == 3
    cap = capsys.readouterr()
    assert cap.err.count("until-done: amendment rejected (noop)") == 1
    assert cap.err.count("until-done: amendment rejected (parse_error)") == 1
    assert "checklist amended" not in cap.out             # amend_k stayed 0
    run = run_dir_of(tmp)
    rej = events_named(run, "amend_rejected")
    assert [(e["round"], e["reason"]) for e in rej] == [(1, "noop"), (2, "parse_error")]
    assert all(sorted(e) == ["kind", "reason", "round", "t"] for e in rej)
    assert not events_named(run, "amend")
    assert listed(amend_dir(run, "applied")) == []
    assert len(listed(amend_dir(run, "rejected"))) == 2   # the bytes filed aside
    assert not os.path.lexists(amend_dir(run, "unapplied.md"))
    assert log_events(run) == ["amend_rejected", "amend_rejected"]
    # never parser text, never task lines: not on the loop's events, not on the log
    log_raw = raw(os.path.join(str(run), "asks", "log.jsonl"))
    rej_raw = "".join(json.dumps(e) for e in rej).encode("utf-8")
    for blob in (log_raw, rej_raw):
        assert b"prose only" not in blob
        assert b"value is good" not in blob
    report = report_of(run)
    assert "## Checklist amendments" in report
    assert "### Amendment " not in report and "### Unapplied" not in report
    assert re.search(r"^- \d{2}:\d{2}:\d{2}: noop$", report, re.M)
    assert re.search(r"^- \d{2}:\d{2}:\d{2}: parse_error$", report, re.M)


def test_unapplied_amend_listed(env, page, tmp_path, monkeypatch, capsys):
    # An amendment that lands during the LAST round's check run has no apply
    # point left: leftover_pending moves it whole to amend/unapplied.md before the
    # report, which lists it under ### Unapplied with the path it returned.
    repo, task, tmp = env
    plant_during_checks(tmp, monkeypatch, AMEND_ADDED)
    script(tmp, [{"result": "a", "write": {"value.txt": "meh\n"}}])
    assert sup(repo, task, "--max-rounds", "1") == 11     # round limit
    run = run_dir_of(tmp)
    un = amend_dir(run, "unapplied.md")
    assert os.path.isfile(un)
    assert raw(un) == AMEND_ADDED.encode("utf-8")
    assert not os.path.lexists(amend_dir(run, "pending.md"))
    assert not events_named(run, "amend") and not events_named(run, "amend_rejected")
    assert log_events(run) == []
    report = report_of(run)
    assert "## Checklist amendments" in report
    assert "### Unapplied" in report and un in report
    assert "### Amendment " not in report and "### Rejected" not in report
    assert "checklist amended" not in capsys.readouterr().out


# ------------------------------------------------------------------- plan + audit

def test_amend_with_plan_rewrites_plan_task_before_taken(env, page, tmp_path):
    # --plan hands the hook the run's task. While the one plan call is still
    # unspent (no hook record ever lands), an applied amendment rewrites
    # plan_dir/task.md too: round 2's call finds the amended copy, where round
    # 1's found the original.
    repo, task, tmp = env
    script(tmp, [{"result": "a", "run_write": {"amend/pending.md": AMEND_ADDED}},
                 {"result": "b", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--plan", "opus") == 0
    recs = plan_records(tmp)
    assert [r["round"] for r in recs] == ["1", "2"] and all(r["task"] for r in recs)
    assert recs[0]["task_text"] == TASK_ONE               # written before round 1
    assert recs[1]["task_text"] == AMEND_ADDED            # rewritten: not taken yet
    run = run_dir_of(tmp)
    assert [(e["k"], e["round"]) for e in events_named(run, "amend")] == [(1, 1)]
    assert task_md(run) == AMEND_ADDED


def test_deviation_audit_gets_amended_task(env, page, tmp_path):
    # The audit reads the task the loop is actually running: the amendment the
    # round planted (a distinct goal line) is in the audit prompt of that very
    # round -- and the superseded one is not.
    repo, task, tmp = env
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"},
                  "run_write": {"amend/pending.md": AMEND_GOAL}},
                 {"result": "NO CONTRADICTIONS"}])
    assert _sup_audited(repo, task) == 0
    run = run_dir_of(tmp)
    assert log_events(run) == ["amend_applied"]
    prompts = sorted(Path(str(run)).glob("audit-*.prompt.md"))
    assert len(prompts) == 1                              # the audit ran once
    text = prompts[0].read_text(encoding="utf-8")
    assert "ZZZAMENDEDGOALZZZ" in text                    # the amended task was sent
    assert "Make the value good." not in text             # not the superseded one
    assert "### Amendment 1 (" in report_of(run)
