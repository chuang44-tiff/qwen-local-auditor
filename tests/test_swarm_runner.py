import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

from lib.swarm_engine import runner, runlock
from swarm_fixtures import ECHO_SCRIPT, agent_args, git_repo, make_workflow, unit_names


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    for k in list(os.environ):
        if k.startswith(("QWEN_SWARM_", "QWEN_DR_")):
            monkeypatch.delenv(k)
    return d


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """A run installs stop handlers for the whole process through
    swarm.install_stop_signals(); every test gets the SIGINT/SIGTERM/SIGHUP handlers it
    found (pytest's or the defaults) back so none of them leaks into another test."""
    saved = {}
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)              # SIGHUP is not everywhere
        if sig is not None:
            saved[sig] = signal.getsignal(sig)
    yield
    for sig, handler in saved.items():
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):                   # only the main thread may change them
            pass


def swarm_main(*args):
    return runner.main(agent_args() + list(args))


def echo(tmp_path, **kw):
    return str(make_workflow(tmp_path / "wfs", **kw))


def test_a_run_writes_the_run_folder_and_prints_the_report_last(tmp_path, fake, capsys):
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "say hi", "--out", str(out)) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [str(out / "report.md")]
    assert "workflow echo (depth quick)" in captured.err and "roles: worker=none" in captured.err
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["goal"] == "say hi" and cfg["workflow"] == "echo" and cfg["items"] == 3
    assert cfg["rounds"] == 1 and cfg["timeout_per_item"] == 100 and cfg["target"] is None
    assert cfg["workflow_dir"] == str((tmp_path / "wfs" / "echo").resolve())
    assert cfg["summary"]["roles"] == {"worker": "none"}
    assert (out / "goal.md").read_text(encoding="utf-8") == "say hi\n"
    assert not (out / "question.md").exists() and not (out / "mcp.json").exists()
    assert sorted(json.loads((out / "totals.json").read_text(encoding="utf-8"))) == [
        "agents_run", "invocations", "seconds", "tokens"]
    assert sorted(unit_names(fake)) == ["work-1", "work-2", "work-3"]


def test_default_run_folder(tmp_path, fake, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert swarm_main(echo(tmp_path), "Say Hi, Now") == 0
    (d,) = list((tmp_path / "swarm" / "echo").iterdir())
    assert d.name.endswith("-say-hi-now")


def test_set_and_flags_resolve_knobs(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("QWEN_SWARM_TIMEOUT", "150")
    monkeypatch.setenv("QWEN_DR_RETRIES", "4")        # QWEN_DR_* names: the research workflow only
    assert swarm_main(echo(tmp_path), "g", "--out", str(tmp_path / "a"), "--set", "items=1",
                      "--set", "retries=2", "--depth", "standard") == 0
    cfg = json.loads((tmp_path / "a" / "config.json").read_text(encoding="utf-8"))
    assert cfg["items"] == 1 and cfg["retries"] == 2 and cfg["timeout_per_item"] == 150
    assert cfg["depth"] == "standard"
    assert swarm_main(echo(tmp_path), "g", "--out", str(tmp_path / "b"), "--set", "budget=120",
                      "--timeout", "90") == 0               # the flag beats --set
    assert json.loads((tmp_path / "b" / "config.json").read_text(
        encoding="utf-8"))["timeout_per_item"] == 90


@pytest.mark.parametrize("args,needle", [
    ([], "no workflow given"),
    (["nosuch", "g"], "no built-in workflow named 'nosuch'"),
    (["./nowhere", "g"], "not a workflow folder"),
    (["WF"], "no goal given (the thing to echo)"),
    (["WF", "g", "--set", "items=x"], "--set items=x"),
    (["WF", "g", "--set", "colour=1"], "no such knob"),
    (["WF", "g", "--depth", "huge"], "--depth must be one of quick, standard"),
    (["WF", "g", "--rounds", "0"], "--rounds"),
    (["WF", "g", "--rounds", "until"], "needs --hours"),
    (["WF", "g", "--target", "."], "takes no --target"),
    (["WF", "g", "--role-effort", "boss=high"], "--role-effort"),
    (["WF", "g", "extra"], "one goal only"),
    (["WF", "g", "--bogus"], "usage: qwen-swarm"),
])
def test_usage_errors_exit_2_before_any_run_folder(tmp_path, fake, capsys, args, needle):
    wf = echo(tmp_path)
    args = [wf if a == "WF" else a for a in args]
    assert swarm_main(*args, "--out", str(tmp_path / "run")) == 2
    assert needle in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


def test_manifest_error_names_the_field(tmp_path, fake, capsys):
    wf = echo(tmp_path, manifest={"target": "maybe"})
    assert swarm_main(wf, "g") == 2
    assert "workflow.json: target must be" in capsys.readouterr().err


def test_validate_hook_refuses_before_the_run(tmp_path, fake, capsys):
    script = "def validate(cfg):\n    return 'items must be odd' if cfg['items'] % 2 == 0 else None\n" \
             "def run(wf):\n    wf.report('ok\\n')\n"
    wf = echo(tmp_path, script=script)
    assert swarm_main(wf, "g", "--set", "items=2", "--out", str(tmp_path / "run")) == 2
    assert "items must be odd" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


def test_fail_exits_5_and_prints_the_run_folder(tmp_path, fake, capsys):
    wf = echo(tmp_path, script="def run(wf):\n    wf.fail('nothing found')\n")
    assert swarm_main(wf, "g", "--out", str(tmp_path / "run")) == 5
    captured = capsys.readouterr()
    assert "nothing found" in captured.err
    assert captured.out.splitlines()[-1] == str(tmp_path / "run")
    wf2 = echo(tmp_path / "x", script="def run(wf):\n    pass\n")
    assert swarm_main(wf2, "g", "--out", str(tmp_path / "run2")) == 5
    assert "without writing a report" in capsys.readouterr().err


def test_goal_unmet_exits_4_with_the_report(tmp_path, fake, capsys):
    wf = echo(tmp_path, script="def run(wf):\n    wf.report('# no fix\\n')\n    wf.goal_unmet('no passing patch')\n")
    assert swarm_main(wf, "g", "--out", str(tmp_path / "run")) == 4
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == str(tmp_path / "run" / "report.md")
    assert "finished without its goal: no passing patch" in captured.err


def test_script_exception_is_exit_8_and_resume_reuses_finished_units(tmp_path, fake, capsys):
    broken = '''
def run(wf):
    items = [{"id": "I%d" % i} for i in range(1, 4)]
    wf.fan_out("work", "worker", items, lambda b: "\\n".join("- %s:" % i["id"] for i in b),
               lambda text, b: [])
    raise RuntimeError("script bug")
'''
    folder = make_workflow(tmp_path / "wfs", script=broken)
    run = tmp_path / "run"
    assert swarm_main(str(folder), "g", "--out", str(run)) == 8
    err = capsys.readouterr().err
    assert "internal error: RuntimeError: script bug" in err and "error.log" in err
    assert "RuntimeError: script bug" in (run / "error.log").read_text(encoding="utf-8")
    (folder / "workflow.py").write_text(broken.replace('raise RuntimeError("script bug")',
                                                       'wf.report("fixed\\n")'), encoding="utf-8")
    runner._MODULES.clear()                       # a new process would import the fixed file
    (fake / "calls.jsonl").unlink()
    assert swarm_main("--resume", str(run)) == 0
    assert unit_names(fake) == []                 # every finished unit came from the cache


def test_resume_rules(tmp_path, fake, capsys):
    wf = echo(tmp_path)
    run = tmp_path / "run"
    assert swarm_main(wf, "g", "--out", str(run)) == 0
    assert swarm_main("research", "--resume", str(run)) == 2
    assert "is a echo run, not research" in capsys.readouterr().err
    assert swarm_main("--resume", str(run), "--set", "items=1") == 2
    assert "only --seats" in capsys.readouterr().err
    assert swarm_main("--resume", str(run), "--rounds", "2", "--seats", "1") == 0
    assert json.loads((run / "config.json").read_text(encoding="utf-8"))["rounds"] == 2
    assert swarm_main(wf, "g", "--out", str(run)) == 2
    assert "already holds a run" in capsys.readouterr().err


def test_a_resume_that_fails_on_the_workflow_leaves_the_config_alone(tmp_path, fake, capsys):
    """config.json is rewritten only once the workflow imported: a resume that dies on
    workflow.py must not leave a merged config behind that a later resume would trust."""
    folder = make_workflow(tmp_path / "wfs")
    run = tmp_path / "run"
    assert swarm_main(str(folder), "g", "--out", str(run)) == 0
    before = (run / "config.json").read_bytes()
    (folder / "workflow.py").write_text("this is not python(\n", encoding="utf-8")
    runner._MODULES.clear()                        # a new process would import the broken file
    assert swarm_main("--resume", str(run), "--rounds", "2") == 8
    assert "workflow.py does not import" in capsys.readouterr().err
    assert (run / "config.json").read_bytes() == before


def test_an_open_ended_run_needs_hours_whatever_names_it(tmp_path, fake, capsys):
    # --set rounds=until is as open-ended as --rounds until, so it needs a deadline from
    # somewhere: a flag, another --set, or the depth preset
    wf = echo(tmp_path)
    run = tmp_path / "run"
    assert swarm_main(wf, "g", "--set", "rounds=until", "--out", str(run)) == 2
    assert "--rounds until needs --hours" in capsys.readouterr().err
    assert not run.exists()
    assert swarm_main(wf, "g", "--set", "rounds=until", "--set", "hours=2",
                      "--out", str(run)) == 0
    cfg = runner.load_json(run / "config.json")
    assert cfg["rounds"] == "until" and cfg["hours"] == 2
    preset = str(make_workflow(tmp_path / "wfs2", manifest={
        "presets": {"quick": {"items": 1, "budget": 100, "retries": 0, "rounds": 1,
                              "hours": 8}}}))
    assert swarm_main(preset, "g", "--set", "rounds=until", "--out", str(tmp_path / "r2")) == 0
    assert runner.load_json(tmp_path / "r2" / "config.json")["hours"] == 8


def test_list_and_preflight(tmp_path, fake, capsys, monkeypatch):
    assert swarm_main("--list") == 0          # the built-ins arrive with their own tasks
    capsys.readouterr()
    assert swarm_main("--preflight", echo(tmp_path)) == 0
    assert capsys.readouterr().out.strip() == "ok: model reachable"
    monkeypatch.setenv("FAKE_PREFLIGHT_RC", "3")
    assert swarm_main(echo(tmp_path / "b"), "g", "--out", str(tmp_path / "run")) == 3
    assert "preflight: model" in capsys.readouterr().err


SANDBOX_SCRIPT = '''
def run(wf):
    wf.fan_out("probe", "prober", [{"id": "H1"}], lambda batch: "probe H1",
               lambda text, batch, patch: [{"patch": patch}])
    wf.report("done\\n")
'''
PROBER = r'''
import pathlib
def answer_cwd(p, r, cwd):
    pathlib.Path(cwd, "fix.txt").write_text("fixed\n", encoding="utf-8")
    return 0, "```json\n{}\n```"
'''


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_target_runs_summarise_the_target_and_clean_leftover_sandboxes(tmp_path, fake, capsys):
    (fake / "prober.py").write_text(PROBER, encoding="utf-8")
    repo = git_repo(tmp_path / "repo", {"a.txt": "1\n"})
    (repo / "a.txt").write_text("2\n", encoding="utf-8")                # a dirty target
    wf = echo(tmp_path, manifest={"target": "required",
                                  "roles": {"prober": {"file": "roles/prober.md", "fence": "sandbox"}}},
              script=SANDBOX_SCRIPT, roles=("prober",))
    run = tmp_path / "run"
    assert swarm_main(wf, "g", "--target", str(repo), "--out", str(run), "--keep-sandboxes") == 0
    err = capsys.readouterr().err
    assert "target: %s" % repo.resolve() in err and "NOT in the sandboxes" in err
    cfg = json.loads((run / "config.json").read_text(encoding="utf-8"))
    assert cfg["target"] == str(repo.resolve()) and cfg["summary"]["target_dirty"] is True
    assert (run / "sandboxes" / "probe-1" / "fix.txt").exists()          # kept for inspection
    runner._MODULES.clear()
    assert swarm_main("--resume", str(run)) == 0                         # a resume cleans up
    assert not (run / "sandboxes" / "probe-1").exists()
    out = subprocess.run(["git", "worktree", "list"], cwd=str(repo), capture_output=True,
                         text=True, check=True).stdout
    assert out.count("\n") == 1
    assert swarm_main(wf, "g", "--out", str(tmp_path / "r2")) == 2      # --target is required
    assert "needs --target" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_run_folder_may_not_hold_one_inside_the_other(tmp_path, fake, capsys):
    """A run folder inside --target would write the run's prompts, patches, logs and
    sandboxes into the user's tree (and a copy-mode sandbox would copy the target into a
    folder inside itself), so the runner refuses before creating anything."""
    wf = echo(tmp_path, manifest={"target": "required"})
    repo = git_repo(tmp_path / "repo", {"a.txt": "1\n", "sub/keep.txt": "k\n"})
    plain = tmp_path / "plain"
    (plain / "sub").mkdir(parents=True)
    (plain / "f.txt").write_text("x\n", encoding="utf-8")
    outside = tmp_path / "run"
    nest = tmp_path / "nest"                                    # a target nested in a run folder
    (nest / "code").mkdir(parents=True)
    for target, run in ((repo, repo / "swarm-run"),            # the run inside a git target
                        (plain, plain / "run"),                 # ... or inside a plain folder
                        (nest / "code", nest),                  # the target inside the run folder
                        (repo, repo),                           # and one and the same directory
                        ):
        assert swarm_main(wf, "g", "--target", str(target), "--out", str(run)) == 2
        err = capsys.readouterr().err
        assert "use --out DIR outside the target" in err, (target, run)
        assert str(pathlib.Path(target).resolve()) in err and str(run) in err, err
    assert not (repo / "swarm-run").exists() and not (plain / "run").exists()
    assert not outside.exists()                    # the refused run folder was never created
    out = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo), capture_output=True,
                         text=True, check=True).stdout
    assert out == ""                               # and nothing was written into the target
    assert swarm_main(wf, "g", "--target", str(repo), "--out", str(outside)) == 0   # the fix


def test_a_default_run_folder_inside_the_target_is_refused_too(tmp_path, fake, monkeypatch,
                                                              capsys):
    """Without --out the run folder lands under the current directory: from inside the
    target that is the same nesting, and it is refused the same way."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    wf = echo(tmp_path, manifest={"target": "required"})
    assert swarm_main(wf, "g", "--target", str(plain)) == 2
    assert "use --out DIR outside the target" in capsys.readouterr().err
    assert not (plain / "swarm").exists()


def test_paths_with_spaces_and_a_non_ascii_goal(tmp_path, fake, capsys):
    out = tmp_path / "my runs" / "first run"
    wf = echo(tmp_path / "wf dir")
    assert swarm_main(wf, "Grüße, wörld?", "--out", str(out)) == 0
    assert (out / "goal.md").read_text(encoding="utf-8") == "Grüße, wörld?\n"
    assert capsys.readouterr().out.splitlines()[-1] == str(out / "report.md")
    assert runner.steps.slug("Grüße, wörld?") == "gr-e-w-rld"


def test_a_relative_workflow_path_resumes_from_anywhere(tmp_path, fake, monkeypatch):
    make_workflow(tmp_path / "wfs")
    monkeypatch.chdir(tmp_path)
    assert swarm_main("./wfs/echo", "g", "--out", "run") == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    (fake / "calls.jsonl").unlink()
    runner._MODULES.clear()
    assert swarm_main("--resume", str(tmp_path / "run")) == 0
    assert unit_names(fake) == []


INTERRUPT_SCRIPT = '''
from lib.swarm_engine import sandbox
def run(wf):
    sandbox.create(wf.target, wf.run_dir / "sandboxes" / "probe-1")
    raise KeyboardInterrupt
'''


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_an_interrupted_target_run_leaves_no_worktree(tmp_path, fake, capsys):
    repo = git_repo(tmp_path / "repo", {"a.txt": "1\n"})
    wf = echo(tmp_path, manifest={"target": "required",
                                  "roles": {"prober": {"file": "roles/prober.md", "fence": "sandbox"}}},
              script=INTERRUPT_SCRIPT, roles=("prober",))
    run = tmp_path / "run"
    assert swarm_main(wf, "g", "--target", str(repo), "--out", str(run)) == 130
    assert "interrupted; resume with --resume" in capsys.readouterr().err
    assert not (run / "sandboxes" / "probe-1").exists()
    out = subprocess.run(["git", "worktree", "list"], cwd=str(repo), capture_output=True,
                         text=True, check=True).stdout
    assert out.count("\n") == 1


def test_raising_validate_exits_8(tmp_path, fake, capsys):
    script = "def validate(cfg):\n    raise ValueError('boom')\n" \
             "def run(wf):\n    wf.report('ok\\n')\n"
    wf = echo(tmp_path, script=script)
    assert swarm_main(wf, "g", "--out", str(tmp_path / "run")) == 8
    err = capsys.readouterr().err
    assert "internal error" in err and "boom" in err and "Traceback" not in err
    assert not (tmp_path / "run").exists()


def test_unexpected_error_exits_8(tmp_path, fake, capsys):
    run = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(run)) == 0
    capsys.readouterr()
    cfg = json.loads((run / "config.json").read_text(encoding="utf-8"))
    cfg["target"] = 5                                   # not a string: a type error on resume
    (run / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert swarm_main("--resume", str(run)) == 8
    err = capsys.readouterr().err
    assert "internal error" in err and "Traceback" not in err
    assert "TypeError" in (run / "error.log").read_text(encoding="utf-8")


def test_relative_workflow_path_matches_on_resume(tmp_path, fake, monkeypatch):
    make_workflow(tmp_path / "wfs")
    monkeypatch.chdir(tmp_path)
    assert swarm_main("./wfs/echo", "g", "--out", "run") == 0
    (fake / "calls.jsonl").unlink()
    runner._MODULES.clear()
    assert swarm_main("./wfs/echo", "--resume", "run") == 0      # the run's own relative path
    assert unit_names(fake) == []                               # resumed from the cache


def test_preflight_without_workflow(tmp_path, fake, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "preflight", lambda agent: True)   # preflight returns a bool
    assert swarm_main("--preflight") == 0
    assert capsys.readouterr().out.strip() == "ok: model reachable"
    monkeypatch.setattr(runner, "preflight", lambda agent: False)
    assert swarm_main("--preflight") == 3
    assert [p.name for p in tmp_path.iterdir()] == ["fake"]     # no run folder was made


def test_huge_hours_is_usage(tmp_path, fake, capsys, monkeypatch):
    wf = echo(tmp_path)
    for value in ("1e300", "inf", "nan"):                        # float yet no deadline fits
        assert swarm_main(wf, "g", "--hours", value, "--out", str(tmp_path / "run")) == 2
        assert "--hours" in capsys.readouterr().err
    monkeypatch.setenv("QWEN_SWARM_HOURS", "1e300")              # same through the environment
    assert swarm_main(wf, "g", "--out", str(tmp_path / "run")) == 2
    assert "--hours" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


def test_surrogate_goal_is_kept(tmp_path, fake):
    goal = "caf\udce9"                            # what a non-UTF-8 argv byte decodes to
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), goal, "--out", str(out)) == 0
    assert (out / "goal.md").read_bytes() == b"caf\xe9\n"
    assert runner.load_json(out / "config.json")["goal"] == goal


def test_stdin_with_goal_word_is_usage(tmp_path, fake, capsys):
    assert swarm_main(echo(tmp_path), "g", "--stdin", "--out", str(tmp_path / "run")) == 2
    assert "--stdin and a goal argument are exclusive" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


def test_user_workflow_named_research_gets_default_profile(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("QWEN_DR_TIMEOUT", "999")   # a QWEN_DR_* name: the built-in research workflow only
    wf = make_workflow(tmp_path / "wfs", manifest={"name": "research"})
    out = tmp_path / "run"
    assert swarm_main(str(wf), "g", "--out", str(out)) == 0
    assert (out / "goal.md").read_text(encoding="utf-8") == "g\n"
    assert not (out / "question.md").exists()
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert "question" not in cfg and cfg["timeout_per_item"] == 100


# ---------------------------------------------------------------- --deep

def calls_of(fake):
    return [json.loads(x) for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()
            if "--preflight-only" not in x]


def test_deep_flag_gives_a_role_the_depth_switches(tmp_path, fake):
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(out), "--deep", "worker") == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["deep"] == ["worker"]
    # a fence-none role has no tools to delegate with: the review round only
    assert all("--review-round" in a and "--subagents-nudge" not in a for a in calls_of(fake))


def test_no_depth_flag_leaves_the_config_alone_and_runs_deep(tmp_path, fake):
    # default depth on: neither "deep" nor "shallow" is written unless its flag is given,
    # and the units get the depth switches (besides their always-present --shallow) anyway
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(out)) == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert "deep" not in cfg and "shallow" not in cfg
    assert all("--shallow" in a and "--review-round" in a and "--subagents-nudge" not in a
               for a in calls_of(fake))                 # fence none: nothing to delegate


def test_manifest_deep_reaches_the_agents(tmp_path, fake):
    wf = echo(tmp_path, manifest={"roles": {"worker": {"file": "roles/worker.md", "fence": "none",
                                                        "deep": ["subagents"]}}})
    assert swarm_main(wf, "g", "--out", str(tmp_path / "run")) == 0
    argv = calls_of(fake)[0]
    assert "--subagents-nudge" in argv and "--review-round" not in argv


@pytest.mark.parametrize("value", ["bogus", "worker,bogus", "all,worker", ""])
def test_deep_flag_names_roles_or_all(tmp_path, fake, capsys, value):
    assert swarm_main(echo(tmp_path), "g", "--out", str(tmp_path / "run"), "--deep", value) == 2
    assert "--deep" in capsys.readouterr().err


def test_deep_on_resume_is_merged_and_reruns_the_units(tmp_path, fake):
    # default depth on: the role starts out "deep": false, because a role that is already
    # deep by default has no cache key left for --deep to change
    out = tmp_path / "run"
    shallow_worker = {"worker": {"file": "roles/worker.md", "fence": "none", "deep": False}}
    assert swarm_main(echo(tmp_path, manifest={"roles": shallow_worker}), "g",
                      "--out", str(out)) == 0
    first = len(calls_of(fake))
    assert all("--review-round" not in a for a in calls_of(fake))
    assert swarm_main("--resume", str(out), "--deep", "all") == 0
    assert json.loads((out / "config.json").read_text(encoding="utf-8"))["deep"] == ["worker"]
    later = calls_of(fake)[first:]
    assert len(later) == first and all("--review-round" in a for a in later)   # new cache keys
    assert swarm_main("--resume", str(out)) == 0
    assert len(calls_of(fake)) == 2 * first                    # and those stay cached


def test_deep_research_compat_does_not_take_deep(tmp_path, fake):
    with pytest.raises(SystemExit):
        runner.parse_args(agent_args() + ["q", "--deep", "all"], compat="deep-research")


# ---------------------------------------------------------------- exit_for and the run lock

@pytest.mark.parametrize("dropped,not_run,unmet,want", [
    (0, 0, None, 0), (1, 0, None, 4), (0, 2, None, 4), (0, 0, "no passing patch", 4),
    (3, 1, "x", 4)])
def test_exit_for_is_the_one_partial_rule(dropped, not_run, unmet, want):
    assert runner.exit_for(dropped, not_run, unmet) == want


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _lock(run, pid):
    (run / ".lock").write_text(json.dumps({"pid": pid, "host": socket.gethostname(),
                                           "started": "x"}), encoding="utf-8")


def test_every_run_end_removes_its_lock(tmp_path, fake):
    for rc, script in ((0, None), (5, "def run(wf):\n    wf.fail('nothing')\n"),
                       (8, "def run(wf):\n    raise RuntimeError('bug')\n"),
                       (130, "def run(wf):\n    raise KeyboardInterrupt\n")):
        kw = {} if script is None else {"script": script}
        run = tmp_path / ("run-%d" % rc)
        assert swarm_main(echo(tmp_path / str(rc), **kw), "g", "--out", str(run)) == rc
        assert run.is_dir() and not (run / ".lock").exists(), rc


def test_a_live_run_refuses_a_resume_and_a_second_start(tmp_path, fake, capsys):
    run = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(run)) == 0
    capsys.readouterr()
    _lock(run, os.getpid())                         # this test process: alive
    before = (run / "config.json").read_bytes()
    assert swarm_main("--resume", str(run), "--rounds", "2") == 2
    assert "run is live (pid %d)" % os.getpid() in capsys.readouterr().err
    assert (run / "config.json").read_bytes() == before       # nothing merged or written
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _lock(fresh, os.getpid())                       # a fresh run racing for its --out
    assert swarm_main(echo(tmp_path / "b"), "g", "--out", str(fresh)) == 2
    assert "run is live (pid %d)" % os.getpid() in capsys.readouterr().err
    assert (fresh / ".lock").exists()               # not ours: left alone


def test_a_refused_start_leaves_the_live_runs_sandboxes_alone(tmp_path, fake, capsys):
    """sandbox.cleanup removes everything under <run>/sandboxes, so only the runner that
    holds the lock may run it: a second start refused by a live runner's lock must not
    touch the live run's files."""
    wf = echo(tmp_path, manifest={"target": "required"})
    target = tmp_path / "code"
    target.mkdir()
    run = tmp_path / "run"
    (run / "sandboxes" / "x").mkdir(parents=True)
    _lock(run, os.getppid())                        # a live runner owns the folder
    assert swarm_main(wf, "g", "--target", str(target), "--out", str(run)) == 2
    assert "run is live (pid %d)" % os.getppid() in capsys.readouterr().err
    assert (run / "sandboxes" / "x").exists()       # not ours: left alone


def test_a_stale_lock_is_removed_by_resume_with_one_line(tmp_path, fake, capsys):
    run = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(run)) == 0
    capsys.readouterr()
    pid = _dead_pid()
    _lock(run, pid)
    assert swarm_main("--resume", str(run)) == 0
    err = capsys.readouterr().err
    assert err.count("removed stale lock (pid %d)" % pid) == 1
    assert not (run / ".lock").exists()


def test_a_resume_whose_preflight_fails_releases_the_lock(tmp_path, fake, monkeypatch):
    run = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(run)) == 0
    monkeypatch.setenv("FAKE_PREFLIGHT_RC", "3")
    assert swarm_main("--resume", str(run)) == 3
    assert not (run / ".lock").exists()


def test_clear_stale_timeout_is_a_usage_error(tmp_path, fake, capsys, monkeypatch):
    """clear_stale waits on RUN/.lock.clear and raises TimeoutError when a second
    process holds it past the wait: that is a "try again" usage error (exit 2), not an
    internal error with a traceback, on --resume and on a fresh start alike."""
    run = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(run)) == 0
    capsys.readouterr()

    def hold_the_clear(run_dir, say):
        raise TimeoutError("%s is held by another process (waited 10s)"
                           % (pathlib.Path(run_dir) / runlock.CLEAR_LOCK))

    monkeypatch.setattr(runlock, "clear_stale", hold_the_clear)
    assert swarm_main("--resume", str(run)) == 2
    err = capsys.readouterr().err
    assert "another process is clearing the lock at %s" % (run / ".lock") in err
    assert "try again" in err and "Traceback" not in err
    assert not (run / ".lock").exists() and not (run / "error.log").exists()
    assert swarm_main(echo(tmp_path / "b"), "g", "--out", str(tmp_path / "fresh")) == 2
    assert "another process is clearing the lock" in capsys.readouterr().err


def test_a_killed_runner_leaves_a_lock_that_resume_clears(tmp_path, fake, capsys):
    run = tmp_path / "run"
    wf = echo(tmp_path, manifest={"presets": {"quick": {"items": 1, "budget": 100,
                                                        "retries": 0, "rounds": 1}}})
    env = dict(os.environ, FAKE_SWARM_SLEEP="5")
    p = subprocess.Popen([sys.executable, runner.__file__] + agent_args()
                         + [wf, "g", "--out", str(run)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline and not ((run / ".lock").exists()
                                          and (fake / "calls.jsonl").exists()
                                          and len(runner_calls(fake)) >= 1):
        time.sleep(0.05)
    p.kill()                                        # SIGKILL / TerminateProcess: no finally
    p.wait(timeout=30)
    assert (run / ".lock").exists()
    assert json.loads((run / ".lock").read_text(encoding="utf-8"))["pid"] == p.pid
    assert swarm_main("--resume", str(run)) == 0
    assert "removed stale lock (pid %d)" % p.pid in capsys.readouterr().err
    assert not (run / ".lock").exists()


def runner_calls(fake):
    return [x for x in (fake / "calls.jsonl").read_text(encoding="utf-8").splitlines()
            if "--preflight-only" not in x]


# ---------------------------------------------------------------- events.jsonl

def events_of(run):
    path = run / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_a_run_emits_start_units_and_end_and_names_its_folder_first(tmp_path, fake, capsys):
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "say hi", "--out", str(out)) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0] == "qwen-swarm: run folder: %s" % out
    ev = events_of(out)
    assert [e["kind"] for e in ev] == ["run_start"] + ["unit_done"] * 3 + ["run_end"]
    start = ev[0]
    assert {k: start[k] for k in ("workflow", "goal", "run", "resumed", "depth", "knobs")} == {
        "workflow": "echo", "goal": "say hi", "run": str(out), "resumed": False,
        "depth": "quick", "knobs": {"items": 3}}
    assert sorted(e["unit"] for e in ev[1:4]) == ["work-1", "work-2", "work-3"]
    assert ev[-1]["exit"] == 0 and ev[-1]["report"] == str(out / "report.md")
    assert all(isinstance(e["t"], float) for e in ev)
    runner._MODULES.clear()
    assert swarm_main("--resume", str(out)) == 0          # a resume appends to the same file
    ev2 = events_of(out)[len(ev):]
    assert ev2[0]["kind"] == "run_start" and ev2[0]["resumed"] is True
    assert all(e["cached"] for e in ev2 if e["kind"] == "unit_done")
    assert ev2[-1]["kind"] == "run_end" and ev2[-1]["exit"] == 0


def test_a_dropped_unit_is_an_event_and_run_end_says_4(tmp_path, fake):
    (fake / "worker.py").write_text("def answer(p, r):\n    return (2, '') if 'I2' in p "
                                    "else (0, '```json\\n[]\\n```')\n", encoding="utf-8")
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(out)) == 4
    ev = events_of(out)
    (drop,) = [e for e in ev if e["kind"] == "unit_dropped"]
    assert drop["unit"] == "work-2" and drop["role"] == "worker" and "exit 2" in drop["why"]
    assert ev[-1] == dict(ev[-1], kind="run_end", exit=4, report=str(out / "report.md"))


@pytest.mark.parametrize("rc,script", [
    (5, "def run(wf):\n    wf.fail('nothing found')\n"),           # wf.fail
    (5, "def run(wf):\n    pass\n"),                                # finish: no report
    (8, "def run(wf):\n    raise RuntimeError('script bug')\n"),
    (130, "def run(wf):\n    raise KeyboardInterrupt\n"),
])
def test_run_end_is_written_on_every_exit_once_the_folder_exists(tmp_path, fake, rc, script):
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path, script=script), "g", "--out", str(out)) == rc
    ev = events_of(out)
    assert ev[0]["kind"] == "run_start"
    assert ev[-1]["kind"] == "run_end" and ev[-1]["exit"] == rc and ev[-1]["report"] is None


def test_run_end_exit_matches_a_usage_error_after_start(tmp_path, fake, capsys):
    """A run() that raises the runner's Usage exits 2 like any usage error -- and the
    run had started, so run_end carries that same 2, not the default 8."""
    script = ("from lib.swarm_engine.runner import Usage\n"
              "def run(wf):\n    raise Usage('the workflow gives up mid-run')\n")
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path, script=script), "g", "--out", str(out)) == 2
    assert "the workflow gives up mid-run" in capsys.readouterr().err
    ev = events_of(out)
    assert ev[0]["kind"] == "run_start"
    assert ev[-1]["kind"] == "run_end" and ev[-1]["exit"] == 2 and ev[-1]["report"] is None
    assert not (out / ".lock").exists()


def test_no_run_end_for_exits_2_and_3(tmp_path, fake, monkeypatch):
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(out)) == 0
    n = len(events_of(out))
    _lock(out, os.getpid())                              # live: --resume refuses (exit 2)
    assert swarm_main("--resume", str(out)) == 2
    (out / ".lock").unlink()
    monkeypatch.setenv("FAKE_PREFLIGHT_RC", "3")
    assert swarm_main("--resume", str(out)) == 3         # the preflight fails: exit 3
    assert len(events_of(out)) == n
    assert swarm_main(echo(tmp_path / "b"), "g", "--out", str(tmp_path / "r3")) == 3
    assert not (tmp_path / "r3").exists()


def test_wf_event_writes_workflow_kinds_and_refuses_engine_kinds(tmp_path, fake):
    script = '''
def run(wf):
    wf.event("attention", item="S1", reason="confirmed", detail="the canvas did not change")
    for kind in ("run_start", "unit_done", "unit_dropped", "claude_call", "run_end", ""):
        try:
            wf.event(kind)
        except ValueError:
            continue
        raise RuntimeError("engine kind %r was accepted" % kind)
    wf.report("ok\\n")
'''
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path, script=script), "g", "--out", str(out)) == 0
    kinds = [e["kind"] for e in events_of(out)]
    assert kinds == ["run_start", "attention", "run_end"]
    att = events_of(out)[1]
    assert (att["item"], att["reason"], att["detail"]) == ("S1", "confirmed", "the canvas did not change")


# ---------------------------------------------------------------- resume fills knobs, notice

FLAVOURED = {"knobs": {"items": "int", "flavour": "str"},
             "presets": {"quick": {"items": 3, "flavour": "plain", "budget": 100,
                                   "retries": 0, "rounds": 1},
                         "standard": {"items": 5, "flavour": "rich", "budget": 100,
                                      "retries": 0, "rounds": 1}}}


def test_resume_fills_a_knob_the_workflow_declared_later_from_the_runs_preset(tmp_path, fake,
                                                                             capsys):
    folder = make_workflow(tmp_path / "wfs")                    # knobs: items only
    run = tmp_path / "run"
    assert swarm_main(str(folder), "g", "--out", str(run), "--depth", "standard",
                      "--set", "items=2") == 0
    make_workflow(tmp_path / "wfs", manifest=FLAVOURED)         # the workflow gained a knob
    runner._MODULES.clear()
    capsys.readouterr()
    assert swarm_main("--resume", str(run)) == 0
    err = capsys.readouterr().err
    assert "config.json has no 'flavour'; using preset value 'rich'" in err
    cfg = runner.load_json(run / "config.json")
    assert cfg["flavour"] == "rich" and cfg["items"] == 2       # the run's own value stays
    assert swarm_main("--resume", str(run)) == 0
    assert "has no 'flavour'" not in capsys.readouterr().err   # saved: said once


def test_resume_without_a_preset_to_fill_from_is_usage(tmp_path, fake, capsys):
    folder = make_workflow(tmp_path / "wfs")
    run = tmp_path / "run"
    assert swarm_main(str(folder), "g", "--out", str(run)) == 0
    cfg = runner.load_json(run / "config.json")
    cfg["depth"] = "gone"
    runner.save_json(run / "config.json", cfg)
    make_workflow(tmp_path / "wfs", manifest=FLAVOURED)
    runner._MODULES.clear()
    assert swarm_main("--resume", str(run)) == 2
    assert "has no 'flavour' key, and its depth 'gone' is not a preset" in capsys.readouterr().err
    cfg2 = runner.load_json(run / "config.json")
    del cfg2["goal"]
    runner.save_json(run / "config.json", cfg2)
    assert swarm_main("--resume", str(run)) == 2                # engine keys are still required
    assert "has no 'goal' key" in capsys.readouterr().err


TWO_NEW = {"knobs": {"items": "int", "ratio": "float", "strict": "bool"},
           "presets": {"quick": {"items": 3, "ratio": 0.25, "strict": True, "budget": 100,
                                 "retries": 0, "rounds": 1},
                       "standard": {"items": 5, "ratio": 1.5, "strict": False, "budget": 100,
                                    "retries": 0, "rounds": 1}}}


def test_resume_fills_two_new_knobs_float_and_bool(tmp_path, fake, capsys):
    folder = make_workflow(tmp_path / "wfs")                    # knobs: items only
    run = tmp_path / "run"
    assert swarm_main(str(folder), "g", "--out", str(run), "--depth", "standard") == 0
    make_workflow(tmp_path / "wfs", manifest=TWO_NEW)           # the workflow gained two
    runner._MODULES.clear()
    capsys.readouterr()
    assert swarm_main("--resume", str(run)) == 0
    err = capsys.readouterr().err
    assert "config.json has no 'ratio'; using preset value 1.5" in err
    assert "config.json has no 'strict'; using preset value False" in err
    assert err.count("using preset value") == 2                 # one line per knob
    cfg = runner.load_json(run / "config.json")
    assert cfg["ratio"] == 1.5 and cfg["strict"] is False
    assert cfg["summary"]["knobs"]["ratio"] == 1.5
    assert cfg["summary"]["knobs"]["strict"] is False


def test_preset_fill_lines_wait_for_the_lock(tmp_path, fake, capsys):
    """A resume refused by a live runner says nothing about the knobs it would have
    filled: the lines are collected before the lock and printed only once it is held."""
    folder = make_workflow(tmp_path / "wfs")
    run = tmp_path / "run"
    assert swarm_main(str(folder), "g", "--out", str(run), "--depth", "standard") == 0
    make_workflow(tmp_path / "wfs", manifest=TWO_NEW)
    runner._MODULES.clear()
    _lock(run, os.getpid())                                     # a live runner owns it
    capsys.readouterr()
    assert swarm_main("--resume", str(run)) == 2
    err = capsys.readouterr().err
    assert "run is live (pid %d)" % os.getpid() in err
    assert "using preset value" not in err
    (run / ".lock").unlink()                                    # the lock is free again: ...
    assert swarm_main("--resume", str(run)) == 0                # ... the lines are said
    assert capsys.readouterr().err.count("using preset value") == 2


NOTICE_SCRIPT = '''
def notice(cfg):
    if cfg["items"] == 3:
        return "items=3: three agents will see the goal.\\nSecond line."
    return None
''' + ECHO_SCRIPT


def test_the_workflow_notice_is_printed_at_run_start_fresh_and_on_resume(tmp_path, fake, capsys):
    wf = echo(tmp_path, script=NOTICE_SCRIPT)
    run = tmp_path / "run"
    assert swarm_main(wf, "g", "--out", str(run)) == 0
    err = capsys.readouterr().err.splitlines()
    i = err.index("qwen-swarm: workflow echo (depth quick)")
    assert "qwen-swarm:   notice: items=3: three agents will see the goal." in err[i:]
    assert "qwen-swarm:   notice: Second line." in err[i:]
    runner._MODULES.clear()
    assert swarm_main("--resume", str(run)) == 0
    assert "notice: items=3" in capsys.readouterr().err
    assert swarm_main(echo(tmp_path / "b", script=NOTICE_SCRIPT), "g", "--set", "items=1",
                      "--out", str(tmp_path / "r2")) == 0
    assert "notice:" not in capsys.readouterr().err              # None: nothing printed


def test_a_raising_notice_is_exit_8_with_run_end_and_no_lock(tmp_path, fake, capsys):
    """notice() runs after run_start, so its crash is an internal error of a run that
    started: exit 8 with a run_end of 8, the traceback in error.log, the lock released."""
    wf = echo(tmp_path, script="def notice(cfg):\n    raise ValueError('notice boom')\n"
                               + ECHO_SCRIPT)
    run = tmp_path / "run"
    assert swarm_main(wf, "g", "--out", str(run)) == 8
    err = capsys.readouterr().err
    assert "internal error: ValueError: notice boom" in err
    assert "ValueError: notice boom" in (run / "error.log").read_text(encoding="utf-8")
    ev = events_of(run)
    assert ev[0]["kind"] == "run_start"
    assert ev[-1]["kind"] == "run_end" and ev[-1]["exit"] == 8 and ev[-1]["report"] is None
    assert not (run / ".lock").exists()


NONSTRING_NOTICE = '''
def notice(cfg):
    cfg["goal"] = "tampered"
    cfg["role_effort"]["worker"] = "high"
    return ["not", "a", "string"]
def run(wf):
    wf.save("seen", {"goal": wf.cfg["goal"], "role_effort": wf.cfg["role_effort"]})
    wf.report("ok\\n")
'''


def test_a_notice_sees_only_a_deep_copy_and_a_non_string_answer_is_ignored(tmp_path, fake,
                                                                           capsys):
    """The hook edits only its copy: a nested change cannot reach the run's config.
    A list answer is not str/None: nothing is printed, and run.log says it was ignored."""
    wf = echo(tmp_path, script=NONSTRING_NOTICE)
    run = tmp_path / "run"
    assert swarm_main(wf, "g", "--out", str(run)) == 0
    assert "notice:" not in capsys.readouterr().err
    assert runner.load_json(run / "seen.json") == {"goal": "g", "role_effort": {}}
    assert "notice returned list, ignored" in (run / "run.log").read_text(encoding="utf-8")
