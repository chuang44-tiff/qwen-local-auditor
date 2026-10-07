import json
import os
import pathlib
import shutil
import signal
import subprocess

import pytest

from lib.swarm_engine import runner
from swarm_fixtures import agent_args, git_repo, make_workflow, unit_names


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
    assert all("--review-round" in a and "--subagents-nudge" in a for a in calls_of(fake))


def test_no_depth_flag_leaves_the_config_alone_and_runs_deep(tmp_path, fake):
    # default depth on: neither "deep" nor "shallow" is written unless its flag is given,
    # and the units get the depth switches (besides their always-present --shallow) anyway
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "g", "--out", str(out)) == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert "deep" not in cfg and "shallow" not in cfg
    assert all("--shallow" in a and "--review-round" in a and "--subagents-nudge" in a
               for a in calls_of(fake))


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
