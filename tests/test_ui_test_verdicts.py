"""ui-test's last word on a non-PASS row: the confirmer's verdict, or the main session's.

The live run builds a `final` artifact once, and the pure functions apply_verdicts and render
turn it, plus every verdict file under RUN/verdicts/, into results.json and report.md -- the
same functions the runner's post-release re-list and `qwen-swarm --record-verdict` use, so a
report re-rendered later is the one the run itself would have written.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys

import test_swarm_runner
import test_ui_test_workflow as uit
from lib.swarm_engine import runner, runlock
from swarm_fixtures import agent_args
from test_ui_test_workflow import suite_file, ui, ui_run, verdict_block

fake = test_swarm_runner.fake
claude = uit.claude      # the autouse FakeClaude fixture, by assignment (pytest collects it)
_restore_signal_handlers = test_swarm_runner._restore_signal_handlers


def write_verdict(out, sid, verdict, *evidence, vid=None):
    """A session verdict file as --record-verdict writes it (vid: a different "id" field)."""
    d = pathlib.Path(out) / "verdicts"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.json" % sid)).write_text(json.dumps(
        {"id": vid or sid, "verdict": verdict, "evidence": list(evidence) or ["probe ok"],
         "by": "session", "t": 1.0}), encoding="utf-8")


def load(out, name):
    return json.loads((pathlib.Path(out) / name).read_text(encoding="utf-8"))


def report(out):
    return (pathlib.Path(out) / "report.md").read_text(encoding="utf-8")


def section(text, title):
    """The body of one '## title' section of a report ('' when it is not there)."""
    head = "## %s\n" % title
    if head not in text:
        return ""
    return text.split(head, 1)[1].split("\n## ", 1)[0]


def _final(rows, mode="claude"):
    return {"rows": rows, "suite_title": "Cart page", "base": None,
            "scenarios": [{"id": r["id"], "title": r["id"].upper()} for r in rows],
            "scenario_file": "/suites/cart.md", "browser_root": "/run/browser",
            "confirm": {"mode": mode, "model": "opus"}, "dropped": 0, "not_run": 0,
            "totals": {"agents_run": 1, "tokens": 0, "seconds": 0, "invocations": 1},
            "claude_calls": 0, "claude_cost_usd": 0.0, "verdicts_applied": [],
            "invalid_verdicts": []}


def _sv(sid, verdict, *evidence):
    return {"id": sid, "verdict": verdict, "evidence": list(evidence), "by": "session",
            "t": 1.0, "mtime_ns": 5}


# ------------------------------------------------------------------ pure functions

def test_apply_verdicts_is_pure_and_the_session_has_the_final_say():
    mod = ui()

    def conf(v):
        return {"verdict": v, "evidence": ["probe"], "by": "claude:opus", "notes": ""}

    rows = [{"id": "s1", "status": "PASS", "notes": "", "unit": "scenario-1"},
            {"id": "s2", "status": "FAIL", "notes": "x", "unit": "scenario-2",
             "confirmation": conf("CONFIRMED")},
            {"id": "s3", "status": "BLOCKED", "notes": "y", "unit": "scenario-3",
             "confirmation": conf("FALSE_ALARM")},
            {"id": "s4", "status": "NOT RUN", "notes": "deadline", "unit": None}]
    final = _final(rows)
    before = json.dumps(final, sort_keys=True)
    sv = {"s1": _sv("s1", "CONFIRMED", "a"), "s2": _sv("s2", "FALSE_ALARM", "canvas hash moved"),
          "s4": _sv("s4", "FALSE_ALARM", "b")}
    out, unmet = mod.apply_verdicts(final, sv)
    assert json.dumps(final, sort_keys=True) == before          # nothing it was handed changed
    assert "final" not in out[0] and "session" not in out[0]     # PASS: not overridable
    assert out[1]["final"] == {"verdict": "FALSE_ALARM", "by": "session"}
    assert out[1]["session"] == {"verdict": "FALSE_ALARM", "evidence": ["canvas hash moved"]}
    assert out[1]["confirmation"]["verdict"] == "CONFIRMED"      # both opinions are kept
    assert out[2]["final"] == {"verdict": "FALSE_ALARM", "by": "claude:opus"}
    assert "final" not in out[3]                                 # NOT RUN: not overridable
    assert unmet is True                                         # ... and it stays unmet
    assert mod.apply_verdicts(_final(rows[:3]), sv)[1] is False
    out3, unmet3 = mod.apply_verdicts(_final(rows[:3]), {})
    assert out3[1]["final"] == {"verdict": "CONFIRMED", "by": "claude:opus"} and unmet3 is True
    plain = [{"id": "s2", "status": "FAIL", "notes": "x", "unit": "scenario-2"}]
    assert mod.apply_verdicts(_final(plain, "none"), {}) == (plain, True)


def test_read_verdicts_validates_every_file(tmp_path):
    mod = ui()
    assert mod.read_verdicts(tmp_path) == {}                     # no verdicts/ folder at all
    write_verdict(tmp_path, "s1", "FALSE_ALARM", "probe ok")
    write_verdict(tmp_path, "s2", "FALSE_ALARM", vid="s9")       # id does not match the name
    write_verdict(tmp_path, "s3", "MAYBE")
    (tmp_path / "verdicts" / "s4.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "verdicts" / "s5.json.tmp").write_text("{}", encoding="utf-8")  # half-written
    invalid = []
    got = mod.read_verdicts(tmp_path, invalid=invalid)
    assert list(got) == ["s1"]
    assert got["s1"]["verdict"] == "FALSE_ALARM" and got["s1"]["evidence"] == ["probe ok"]
    assert got["s1"]["mtime_ns"] == (tmp_path / "verdicts" / "s1.json").stat().st_mtime_ns
    assert [name for name, _ in invalid] == ["s2.json", "s3.json", "s4.json"]
    assert "'s9' does not match the file name" in invalid[0][1]
    assert "'MAYBE' is not one of" in invalid[1][1]


# ------------------------------------------------------------------ the live run

def test_final_artifact_and_render_golden(tmp_path, fake, monkeypatch, claude):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2,s3")
    claude.answers.update(s2=verdict_block("s2", "FALSE_ALARM", "probe: badge text is 1"),
                          s3=verdict_block("s3", "CONFIRMED", "badge shows 0"))
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 3), out) == 4
    final = load(out, "final.json")
    for key in ("rows", "suite_title", "base", "scenarios", "dropped", "not_run", "totals",
                "claude_calls", "claude_cost_usd", "verdicts_applied"):
        assert key in final, key
    assert final["totals"] == load(out, "totals.json")           # the one snapshot
    # the totals accounting: two claude calls were made (FakeClaude bumps wf.claude_calls and
    # wf.claude_cost_usd, wf.totals() adds both to totals.json, `final` copies them)
    assert (final["claude_calls"], final["claude_cost_usd"]) == (2, 0.5)
    assert final["totals"]["claude_calls"] == 2
    assert final["verdicts_applied"] == [] and final["confirm"]["mode"] == "claude"
    rows = load(out, "results.json")
    assert [r.get("final") for r in rows] == [
        None, {"verdict": "FALSE_ALARM", "by": "claude:opus"},
        {"verdict": "CONFIRMED", "by": "claude:opus"}]
    # the pure render of what the run saved IS the report the run wrote
    assert ui().render(final, rows) == report(out)


def test_report_sections_with_the_confirm_pass(tmp_path, fake, monkeypatch, claude):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2,s3")
    claude.answers.update(s2=verdict_block("s2", "FALSE_ALARM", "probe: badge text is 1"),
                          s3=verdict_block("s3", "CONFIRMED", "badge shows 0"))
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 3), out) == 4
    text = report(out)
    assert "| id | status | confirmed | notes |" in text
    assert "| s2 | FAIL | FALSE_ALARM |" in text and "| s1 | PASS |  |" in text
    alarms = section(text, "False alarms")
    assert "**s2** Scenario 2 (tester: FAIL; false alarm by claude:opus)" in alarms
    assert "check: probe: badge text is 1" in alarms and "s3" not in alarms
    bad = section(text, "What did not pass")
    assert "**s3** Scenario 3 (FAIL, CONFIRMED by claude:opus)" in bad and "**s2**" not in bad
    run = section(text, "Run")
    for row in ("| confirmed | 1 |", "| false alarms | 1 |", "| needs human | 0 |",
                "| claude calls | 2 |", "| claude cost | $0.50 |"):
        assert row in run, row
    assert "Session verdicts" not in text


def test_confirm_none_report_says_no_confirm_pass_ran(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out, "--set", "confirm=none") == 4
    text = report(out)
    assert "| id | status | notes |" in text                     # no confirmed column
    bad = section(text, "What did not pass")
    assert "- **s2** Scenario 2 (FAIL): browser folder" in bad
    assert ("No confirm pass ran: check each FAIL by hand or with a scripted probe before "
            "treating it as a regression.") in bad
    assert "| confirmed |" not in section(text, "Run")
    assert all("final" not in r for r in load(out, "results.json"))


def test_a_session_verdict_before_the_confirm_pass_skips_the_confirmer(tmp_path, fake,
                                                                       monkeypatch, claude):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2,s3")
    out = tmp_path / "run"
    write_verdict(out, "s2", "FALSE_ALARM", "canvas hash changed")   # recorded before it ran
    claude.answers["s3"] = verdict_block("s3", "FALSE_ALARM", "probe ok")
    assert ui_run(fake, suite_file(tmp_path, 3), out) == 0
    assert [c["id"] for c in claude.calls] == ["s3"]             # no call, no cap use for s2
    rows = load(out, "results.json")
    assert "confirmation" not in rows[1]
    assert rows[1]["final"] == {"verdict": "FALSE_ALARM", "by": "session"}
    by = [(e["id"], e["by"]) for e in uit.events(out, "verdict")]
    assert by == [("s2", "session"), ("s3", "claude:opus")]
    assert [e["item"] for e in uit.events(out, "attention")] == ["s3"]
    text = report(out)
    assert "- **s2** Scenario 2: session: FALSE_ALARM — canvas hash changed" in section(
        text, "Session verdicts")
    assert load(out, "final.json")["verdicts_applied"][0][0] == "s2"


def test_a_session_verdict_during_the_confirm_pass_shows_both_opinions(tmp_path, fake,
                                                                       monkeypatch, claude):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2,s3")
    claude.answers.update(s2=verdict_block("s2", "CONFIRMED", "badge shows 0"),
                          s3=verdict_block("s3", "CONFIRMED", "badge shows 0"))
    out = tmp_path / "run"

    def session_weighs_in(iid, wf):
        if iid == "s3":                                          # s2 was already confirmed
            write_verdict(out, "s2", "FALSE_ALARM", "the badge updates after a reload")
    claude.on_call = session_weighs_in
    assert ui_run(fake, suite_file(tmp_path, 3), out) == 4       # s3 is still CONFIRMED
    rows = load(out, "results.json")
    assert rows[1]["confirmation"]["verdict"] == "CONFIRMED"
    assert rows[1]["final"] == {"verdict": "FALSE_ALARM", "by": "session"}
    assert ("- **s2** Scenario 2: confirmer: CONFIRMED · session: FALSE_ALARM — the badge "
            "updates after a reload") in section(report(out), "Session verdicts")
    assert ("s2", "session") in [(e["id"], e["by"]) for e in uit.events(out, "verdict")]


def test_an_invalid_verdict_file_is_ignored_and_reported(tmp_path, fake, monkeypatch, claude):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    out = tmp_path / "run"
    write_verdict(out, "s2", "FALSE_ALARM", vid="s1")
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 4
    assert [c["id"] for c in claude.calls] == ["s2"]             # not settled: still asked
    assert "final" in load(out, "results.json")[1]
    assert load(out, "results.json")[1]["final"]["by"] == "claude:opus"
    assert "- ignored `verdicts/s2.json`: its id 's1' does not match the file name" in \
        section(report(out), "Session verdicts")
    assert "ignored verdicts/s2.json" in (out / "run.log").read_text(encoding="utf-8")


def test_ignored_session_verdicts_are_listed(tmp_path, fake, monkeypatch, capsys):
    # a VALID verdict file that attaches to no FAIL/BLOCKED row -- for a PASS row, a NOT RUN
    # row, or an id that is no scenario -- is read and applied to nothing: the report lists
    # each under "Session verdicts" as ignored, with the reason
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 3), out, "--set", "confirm=none") == 4
    write_verdict(out, "s1", "FALSE_ALARM", "the badge is fine")        # a PASS row
    write_verdict(out, "s9", "FALSE_ALARM", "no scenario s9 here")      # no such scenario
    assert rec(out, "s2", "FALSE_ALARM", "probe ok") == 0               # a FAIL row: applies
    capsys.readouterr()
    text = section(report(out), "Session verdicts")
    assert "- **s2** Scenario 2: session: FALSE_ALARM — probe ok" in text
    assert "- ignored `verdicts/s1.json`: its row is PASS; only a FAIL or BLOCKED row " \
        "takes a verdict" in text
    assert "- ignored `verdicts/s9.json`: not a scenario of this run" in text
    out2 = tmp_path / "run2"                                            # a NOT RUN row
    assert ui_run(fake, suite_file(tmp_path, 2), out2, "--set", "confirm=none",
                  "--hours", "1e-9") == 4
    write_verdict(out2, "s1", "FALSE_ALARM", "would have passed")
    assert runner.rerender(out2, ui())[0] == 4
    assert "- ignored `verdicts/s1.json`: its row is NOT RUN; only a FAIL or BLOCKED row " \
        "takes a verdict" in section(report(out2), "Session verdicts")


def test_no_confirm_sentence_only_when_a_row_is_unsettled(tmp_path, fake, monkeypatch):
    # the "no confirm pass ran" sentence asks the reader to check FAILs by hand: it belongs
    # in a confirm=none report only while some non-PASS row really has no final verdict
    sentence = "No confirm pass ran: check each FAIL by hand"
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    out = tmp_path / "run"                                   # nothing decided s2
    assert ui_run(fake, suite_file(tmp_path, 2), out, "--set", "confirm=none") == 4
    assert sentence in section(report(out), "What did not pass")
    out2 = tmp_path / "run2"                                 # s2 settled by the session
    write_verdict(out2, "s2", "CONFIRMED", "badge shows 0")
    assert ui_run(fake, suite_file(tmp_path, 2), out2, "--set", "confirm=none") == 4
    bad = section(report(out2), "What did not pass")
    assert "**s2**" in bad and sentence not in bad           # a checked row is not unsettled
    monkeypatch.delenv("FAKE_UI_FAIL")                       # a clean run: nothing to check
    out3 = tmp_path / "run3"
    assert ui_run(fake, suite_file(tmp_path, 2), out3, "--set", "confirm=none") == 0
    assert sentence not in report(out3)


def test_false_alarm_count_excludes_advisory_local_ones(tmp_path, fake, monkeypatch, claude):
    # "false alarms" in the Run table counts rows the check CLEARED; a local FALSE_ALARM is
    # advisory -- its row still counts -- so it is not one, in the table or in the section
    monkeypatch.setenv("FAKE_UI_FAIL", "s1,s2")
    (fake / "confirmer.py").write_text(uit.CONFIRMER, encoding="utf-8")
    out = tmp_path / "run"
    write_verdict(out, "s2", "FALSE_ALARM", "probe: window.appState says it works")
    assert ui_run(fake, suite_file(tmp_path, 2), out, "--set", "confirm=local") == 4
    rows = load(out, "results.json")
    assert rows[0]["final"] == {"verdict": "FALSE_ALARM", "by": "local"}
    assert rows[1]["final"] == {"verdict": "FALSE_ALARM", "by": "session"}
    assert "| false alarms | 1 |" in section(report(out), "Run")
    alarms = section(report(out), "False alarms")
    assert "**s2**" in alarms and "**s1**" not in alarms
    log = (out / "run.log").read_text(encoding="utf-8")
    assert "s1: FAIL -- browser folder" in log and \
        "[confirm: FALSE_ALARM by local (advisory, still counts)]" in log
    assert "[final: FALSE_ALARM by session]" in log


def test_verdict_files_survive_a_resume(tmp_path, fake, monkeypatch, claude):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 4       # s2: NEEDS_HUMAN (no login)
    write_verdict(out, "s2", "FALSE_ALARM", "probe ok")
    claude.calls.clear()
    assert runner.main(agent_args() + ["--resume", str(out)]) == 0
    assert claude.calls == []
    assert load(out, "results.json")[1]["final"] == {"verdict": "FALSE_ALARM", "by": "session"}


def test_a_resume_that_cannot_reach_its_rows_leaves_no_final(tmp_path, fake):
    # a resume that ends before its final rows (here suite.json and the scenario file are
    # both gone: exit 5) must not leave the last run's final.json for --record-verdict
    out = tmp_path / "run"
    suite = suite_file(tmp_path, 1)
    assert ui_run(fake, suite, out) == 0
    assert (out / "final.json").exists()
    (out / "suite.json").unlink()
    pathlib.Path(suite).unlink()
    assert runner.main(agent_args() + ["--resume", str(out)]) == 5
    assert not (out / "final.json").exists()


# ------------------------------------------------------------------ the gap after the run

def test_a_verdict_written_in_the_gap_is_applied_by_the_runner(tmp_path, fake, monkeypatch,
                                                               claude, capsys):
    # the session records its verdict after the run built its final rows: the runner's
    # re-list after releasing the run lock applies it, and the process exit is the new one
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    claude.answers["s2"] = verdict_block("s2", "CONFIRMED", "badge shows 0")
    mod = ui()
    real = mod.render
    out = tmp_path / "run"

    def render(final, rows):
        if not (out / "verdicts" / "s2.json").exists():
            write_verdict(out, "s2", "FALSE_ALARM", "the badge updates after a reload")
        return real(final, rows)
    monkeypatch.setattr(mod, "render", render)
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 0
    # run_end was written inside _start, before the gap verdict: it keeps the run's own exit
    assert uit.events(out, "run_end")[-1]["exit"] == 4
    assert load(out, "results.json")[1]["final"] == {"verdict": "FALSE_ALARM", "by": "session"}
    assert [vid for vid, _ in load(out, "final.json")["verdicts_applied"]] == ["s2"]
    assert "confirmer: CONFIRMED · session: FALSE_ALARM" in report(out)
    err = capsys.readouterr().err
    assert ("applied 1 session verdict(s) recorded as the run ended; exit 0: nothing left "
            "that counts as a failure") in err
    assert not (out / ".render.lock").exists() and not (out / ".lock").exists()


def test_a_busy_render_lock_keeps_the_run_exit_and_says_so(tmp_path, fake, monkeypatch,
                                                           claude, capsys):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    claude.answers["s2"] = verdict_block("s2", "CONFIRMED", "badge shows 0")
    mod = ui()
    real = mod.render
    out = tmp_path / "run"

    def render(final, rows):
        if not (out / "verdicts" / "s2.json").exists():
            write_verdict(out, "s2", "FALSE_ALARM", "the badge updates after a reload")
        return real(final, rows)

    def busy(run_dir, *a, **k):
        raise TimeoutError("%s/.render.lock is held by another process (waited 30s)" % run_dir)
    monkeypatch.setattr(mod, "render", render)
    monkeypatch.setattr(runlock, "render_lock", busy)
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 4       # the run's own exit stands
    err = capsys.readouterr().err
    assert "a session verdict was recorded as the run ended but" in err
    assert "is held by another process" in err and "--record-verdict" in err
    assert (out / "verdicts" / "s2.json").exists()


def test_no_new_verdict_no_re_render(tmp_path, fake, monkeypatch, claude, capsys):
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    out = tmp_path / "run"
    write_verdict(out, "s2", "FALSE_ALARM", "probe ok")          # picked up by the live run
    calls = []
    monkeypatch.setattr(runner, "rerender", lambda *a: calls.append(a))
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 0
    assert calls == [] and "applied" not in capsys.readouterr().err


def test_a_drop_is_not_cleared_by_a_verdict_on_re_render(tmp_path, fake, monkeypatch):
    # the exit line and the code of a re-rendered run: a FALSE_ALARM verdict settles the
    # dropped tester's row (unmet False), but the drop itself is not cleared by a verdict
    monkeypatch.setenv("FAKE_UI_CRASH", "s2")
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out, behavior=uit.CRASHER) == 4
    assert load(out, "final.json")["dropped"] == 1
    write_verdict(out, "s2", "FALSE_ALARM", "the page works")
    code, final, unmet = runner.rerender(out, ui())
    assert code == 4 and unmet is False
    assert "1 agent(s) dropped (a verdict does not clear a drop)" in \
        runner._exit_line(code, final, unmet)


def test_a_not_run_row_is_not_cleared_by_a_verdict_on_re_render(tmp_path, fake):
    # a verdict attaches to no NOT RUN row and does not clear the not_run count either:
    # the row still counts (unmet stays True -- no verdict can settle a NOT RUN row), the
    # re-rendered run still exits 4, and the exit line names the not-run item
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 1), out, "--hours", "1e-9") == 4
    assert load(out, "final.json")["not_run"] == 1
    write_verdict(out, "s1", "FALSE_ALARM", "the page works")
    code, final, unmet = runner.rerender(out, ui())
    assert code == 4 and unmet is True
    assert "1 item(s) not run (a verdict does not clear that" in \
        runner._exit_line(code, final, unmet)


def test_busy_render_lock_after_the_run_never_reports_a_clean_pass(tmp_path, fake, monkeypatch,
                                                                   claude, capsys):
    # the run ends 0, a verdict lands in the gap and the render lock is busy: the verdict
    # did not land, so the process must exit 4 -- never 0 -- and say to record it again
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    claude.answers["s2"] = verdict_block("s2", "FALSE_ALARM", "probe: badge text is 1")
    mod = ui()
    real = mod.render
    out = tmp_path / "run"

    def render(final, rows):
        if not (out / "verdicts" / "s2.json").exists():
            write_verdict(out, "s2", "FALSE_ALARM", "the badge updates after a reload")
        return real(final, rows)

    def busy(run_dir, *a, **k):
        raise TimeoutError("%s/.render.lock is held by another process (waited 30s)" % run_dir)
    monkeypatch.setattr(mod, "render", render)
    monkeypatch.setattr(runlock, "render_lock", busy)
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 4       # the run itself ended 0
    err = capsys.readouterr().err
    assert "--record-verdict" in err and "again" in err
    assert uit.events(out, "run_end")[-1]["exit"] == 0           # run_end keeps the run's own
    assert load(out, "results.json")[1]["final"]["by"] == "claude:opus"     # left unapplied


def test_a_fault_reading_the_verdicts_after_the_run_keeps_the_exit(tmp_path, fake, monkeypatch,
                                                                   capsys):
    # anything else the post-release read can hit (not a busy lock) is an err() note, and
    # the run's own exit stands: a clean run still reports 0, just with the note on stderr
    mod = ui()
    real = mod.read_verdicts

    def flaky(run_dir, *a, **k):
        if (pathlib.Path(run_dir) / "final.json").exists():     # only the post-run read fails
            raise OSError("verdicts/ unreadable")
        return real(run_dir, *a, **k)
    monkeypatch.setattr(mod, "read_verdicts", flaky)
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 0
    err = capsys.readouterr().err
    assert "could not read the run's session verdicts after the run: OSError: verdicts/" in err
    assert "the run's exit stands" in err


def test_a_fault_applying_the_gap_verdict_keeps_the_run_exit(tmp_path, fake, monkeypatch,
                                                             claude, capsys):
    # the run ends 4, a verdict lands in the gap and the re-render itself faults (not the
    # lock): an err() note, the run's exit stands, and the note says to record again once fixed
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    claude.answers["s2"] = verdict_block("s2", "CONFIRMED", "badge shows 0")
    mod = ui()
    real = mod.render
    out = tmp_path / "run"

    def render(final, rows):
        if not (out / "verdicts" / "s2.json").exists():
            write_verdict(out, "s2", "FALSE_ALARM", "the badge updates after a reload")
        return real(final, rows)
    monkeypatch.setattr(mod, "render", render)

    def boom(run_dir, *a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(runner, "rerender", boom)
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 4       # the run's own exit stands
    err = capsys.readouterr().err
    assert ("could not apply a session verdict recorded as the run ended: RuntimeError: "
            "disk gone" in err and "the run's exit stands" in err and "--record-verdict" in err)
    assert load(out, "results.json")[1]["final"]["by"] == "claude:opus"


def test_a_verdict_gone_at_the_final_read_is_logged(tmp_path, fake, monkeypatch, claude):
    # it skipped the confirm pass on the verdict's word, and the word broke before the
    # final read: run.log must name the row that was not confirmed and now has no verdict
    monkeypatch.setenv("FAKE_UI_FAIL", "s2")
    out = tmp_path / "run"
    write_verdict(out, "s2", "FALSE_ALARM", "probe ok")          # settled: no confirmer call
    mod = ui()
    real = mod.read_verdicts
    reads = []

    def flaky(run_dir, *a, **k):
        reads.append(1)
        if len(reads) == 2:                                      # gone by the final read
            (out / "verdicts" / "s2.json").write_text("{not json", encoding="utf-8")
        return real(run_dir, *a, **k)
    monkeypatch.setattr(mod, "read_verdicts", flaky)
    assert ui_run(fake, suite_file(tmp_path, 2), out) == 4       # s2 counts with nothing behind it
    assert claude.calls == []                                    # the pass never asked about s2
    log = (out / "run.log").read_text(encoding="utf-8")
    assert ("the session verdict for s2 seen before the confirm pass is gone or invalid at "
            "the final read: the row was not confirmed and now has no verdict") in log


def test_local_confirm_results_are_matched_by_unit_name(tmp_path, fake, monkeypatch, claude):
    # every checked row gets a confirm-<id> unit and the results are paired to rows by
    # that name: three rows, three verdicts, each on its own row
    monkeypatch.setenv("FAKE_UI_FAIL", "s1,s2,s3")
    monkeypatch.setenv("FAKE_CONFIRM_s1", "FALSE_ALARM")
    monkeypatch.setenv("FAKE_CONFIRM_s2", "CONFIRMED")
    monkeypatch.setenv("FAKE_CONFIRM_s3", "NEEDS_HUMAN")
    (fake / "confirmer.py").write_text(uit.CONFIRMER, encoding="utf-8")
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 3), out, "--set", "confirm=local") == 4
    assert sorted(uit.unit_names(fake)) == ["confirm-s1", "confirm-s2", "confirm-s3",
                                            "scenario-1", "scenario-2", "scenario-3"]
    rows = load(out, "results.json")
    assert [(r["id"], r["confirmation"]["verdict"], r["confirmation"]["by"]) for r in rows] == [
        ("s1", "FALSE_ALARM", "local"), ("s2", "CONFIRMED", "local"),
        ("s3", "NEEDS_HUMAN", "local")]


def test_a_workflow_without_verdicts_is_never_re_rendered(tmp_path, fake, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "rerender", lambda *a: calls.append(a))
    wf = test_swarm_runner.echo(tmp_path)
    assert test_swarm_runner.swarm_main(wf, "say hi", "--out", str(tmp_path / "run")) == 0
    assert calls == []


# ------------------------------------------------------------------ --record-verdict

def rec(out, sid, verdict, *evidence):
    args = ["--record-verdict", str(out), "--id", sid, "--verdict", verdict]
    for e in evidence:
        args += ["--evidence", e]
    return runner.main(agent_args() + args)


def failed_run(tmp_path, fake, monkeypatch, fail="s2", n=3, *extra, **kw):
    monkeypatch.setenv("FAKE_UI_FAIL", fail)
    out = tmp_path / "run"
    code = ui_run(fake, suite_file(tmp_path, n), out, *extra, **kw)
    return out, code


def test_record_verdict_refuses_what_it_cannot_apply(tmp_path, fake, monkeypatch, capsys):
    out, code = failed_run(tmp_path, fake, monkeypatch)
    assert code == 4
    capsys.readouterr()
    assert rec(out, "s9", "FALSE_ALARM", "x") == 2
    assert "no scenario 's9' in this run (scenarios: s1, s2, s3)" in capsys.readouterr().err
    assert rec(out, "s1", "FALSE_ALARM", "x") == 2
    assert "scenario s1 is PASS; only a FAIL or BLOCKED scenario takes a verdict" in \
        capsys.readouterr().err
    assert rec(out, "s2", "MAYBE", "x") == 2
    assert "--verdict must be one of CONFIRMED, FALSE_ALARM, NEEDS_HUMAN" in capsys.readouterr().err
    assert rec(out, "s2", "FALSE_ALARM") == 2
    assert "needs at least one --evidence TEXT" in capsys.readouterr().err
    assert runner.main(agent_args() + ["--id", "s2"]) == 2
    assert "--id, --verdict and --evidence go with --record-verdict" in capsys.readouterr().err
    assert not (out / "verdicts").exists()                       # nothing was written


def test_record_verdict_refuses_a_not_run_row(tmp_path, fake, capsys):
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out, "--hours", "1e-9") == 4
    assert rec(out, "s1", "FALSE_ALARM", "x") == 2
    assert "scenario s1 is NOT RUN" in capsys.readouterr().err


def test_record_verdict_refuses_a_workflow_without_verdicts(tmp_path, fake, capsys):
    wf = test_swarm_runner.echo(tmp_path)
    out = tmp_path / "run"
    assert test_swarm_runner.swarm_main(wf, "say hi", "--out", str(out)) == 0
    assert rec(out, "I1", "FALSE_ALARM", "x") == 2
    assert "is a echo run; that workflow takes no verdicts" in capsys.readouterr().err
    assert rec(tmp_path / "nowhere", "s1", "FALSE_ALARM", "x") == 2
    assert "is not a run folder" in capsys.readouterr().err


def test_record_verdict_on_a_finished_run_re_renders_it(tmp_path, fake, monkeypatch, capsys):
    out, code = failed_run(tmp_path, fake, monkeypatch)
    assert code == 4                                             # s2: NEEDS_HUMAN (no login)
    totals = (out / "totals.json").read_bytes()
    capsys.readouterr()
    assert rec(out, "s2", "FALSE_ALARM", "probe: badge text is 1", "screenshot badge.png") == 0
    captured = capsys.readouterr()
    assert "exit 0: nothing left that counts as a failure" in captured.err
    assert captured.out.splitlines()[-1] == str(out / "report.md")
    v = load(out, "verdicts/s2.json")
    assert (v["id"], v["verdict"], v["by"]) == ("s2", "FALSE_ALARM", "session")
    assert v["evidence"] == ["probe: badge text is 1", "screenshot badge.png"]
    assert isinstance(v["t"], float)
    assert load(out, "results.json")[1]["final"] == {"verdict": "FALSE_ALARM", "by": "session"}
    text = report(out)
    assert ("confirmer: NEEDS_HUMAN · session: FALSE_ALARM — probe: badge text is 1; "
            "screenshot badge.png") in section(text, "Session verdicts")
    assert "**s2**" in section(text, "False alarms")
    assert (out / "totals.json").read_bytes() == totals          # untouched
    assert ui().render(load(out, "final.json"), load(out, "results.json")) == text
    # a later call for the same id replaces it
    assert rec(out, "s2", "CONFIRMED", "badge shows 0 after a reload") == 4
    assert load(out, "results.json")[1]["final"] == {"verdict": "CONFIRMED", "by": "session"}
    assert "exit 4: rows that still count as failures remain" in capsys.readouterr().err


def test_record_verdict_leaves_events_jsonl_untouched(tmp_path, fake, monkeypatch):
    # the re-render rewrites results.json and report.md, and --record-verdict applies a
    # verdict without emitting verdict events (the monitor has already been told) -- the
    # event stream of a finished run never grows by a byte
    out, _ = failed_run(tmp_path, fake, monkeypatch)
    before = (out / "events.jsonl").read_bytes()
    assert rec(out, "s2", "FALSE_ALARM", "probe: badge text is 1") == 0
    assert (out / "events.jsonl").read_bytes() == before


def test_record_verdict_does_not_clear_a_dropped_tester(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_UI_CRASH", "s2")
    out = tmp_path / "run"
    assert ui_run(fake, suite_file(tmp_path, 2), out, behavior=uit.CRASHER) == 4
    capsys.readouterr()
    assert rec(out, "s2", "FALSE_ALARM", "the page works") == 4
    assert ("exit 4: 1 agent(s) dropped (a verdict does not clear a drop)"
            in capsys.readouterr().err)
    assert load(out, "results.json")[1]["final"]["by"] == "session"


def test_record_verdict_on_a_live_run_only_saves_the_file(tmp_path, fake, monkeypatch, capsys):
    out, _ = failed_run(tmp_path, fake, monkeypatch)
    before = report(out)
    runlock.acquire(out)                                         # this process is "the run"
    try:
        assert rec(out, "s2", "FALSE_ALARM", "probe ok") == 0
    finally:
        runlock.release(out)
    assert "recorded; the running workflow will apply it" in capsys.readouterr().out
    assert (out / "verdicts" / "s2.json").exists() and report(out) == before


def test_record_verdict_on_an_unfinished_run_exits_5(tmp_path, fake, monkeypatch, capsys):
    out, _ = failed_run(tmp_path, fake, monkeypatch)
    (out / "final.json").unlink()                                # as a killed run leaves it
    capsys.readouterr()
    assert rec(out, "s2", "FALSE_ALARM", "probe ok") == 5
    assert "run did not finish; verdict saved, applied on --resume" in capsys.readouterr().err
    assert (out / "verdicts" / "s2.json").exists()
    assert runner.main(agent_args() + ["--resume", str(out)]) == 0   # ... and it is
    assert load(out, "results.json")[1]["final"]["by"] == "session"


def test_record_verdict_clears_a_stale_lock(tmp_path, fake, monkeypatch, capsys):
    out, _ = failed_run(tmp_path, fake, monkeypatch)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (out / ".lock").write_text(json.dumps({"pid": dead.pid, "host": socket.gethostname(),
                                           "started": 1.0}), encoding="utf-8")
    capsys.readouterr()
    assert rec(out, "s2", "FALSE_ALARM", "probe ok") == 0
    assert "removed stale lock (pid %d)" % dead.pid in capsys.readouterr().err
    assert not (out / ".lock").exists()


def test_record_verdict_with_a_busy_render_lock_saves_the_file_and_exits_8(
        tmp_path, fake, monkeypatch, capsys):
    out, _ = failed_run(tmp_path, fake, monkeypatch)

    def busy(run_dir, *a, **k):
        raise TimeoutError("%s/.render.lock is held by another process (waited 30s)" % run_dir)
    monkeypatch.setattr(runlock, "render_lock", busy)
    capsys.readouterr()
    assert rec(out, "s2", "FALSE_ALARM", "probe ok") == 8
    err = capsys.readouterr().err
    assert "verdict saved to" in err and "was not applied" in err and "again" in err
    assert (out / "verdicts" / "s2.json").exists()


def test_two_parallel_record_verdicts_both_land(tmp_path, fake, monkeypatch):
    out, code = failed_run(tmp_path, fake, monkeypatch, "s2,s3")
    assert code == 4
    script = str(pathlib.Path(runner.__file__).resolve())
    procs = [subprocess.Popen([sys.executable, script] + agent_args() + [
                 "--record-verdict", str(out), "--id", sid, "--verdict", "FALSE_ALARM",
                 "--evidence", "probe ok for %s" % sid],
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))
             for sid in ("s2", "s3")]
    codes = [p.wait(timeout=60) for p in procs]
    assert set(codes) <= {0, 4} and 0 in codes                   # the later render sees both
    rows = load(out, "results.json")
    assert [r["final"] for r in rows[1:]] == [{"verdict": "FALSE_ALARM", "by": "session"}] * 2
    sessions = section(report(out), "Session verdicts")
    assert "probe ok for s2" in sessions and "probe ok for s3" in sessions
    assert not (out / ".render.lock").exists()
