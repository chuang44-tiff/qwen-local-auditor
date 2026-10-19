"""Per-role Claude settings of a swarm: launch flags, config defaults, --show-claude, cache key."""
import json
import os
import shutil

import pytest

from lib import swarm
from lib.swarm_engine import manifest, runner
from swarm_fixtures import agent_args, calls, git_repo, make_workflow

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

SCRIPT = '''
def run(wf):
    wf.fan_out("hyp", "hypothesizer", [{"id": "H1"}], lambda b: "think",
               lambda text, batch: [{"id": "H1"}])
    wf.fan_out("probe", "prober", [{"id": "P1"}], lambda b: "probe",
               lambda text, batch, patch: [{"patch": patch}])
    wf.report("done\\n")
'''
MANIFEST = {"target": "required",
            "roles": {"hypothesizer": {"file": "roles/hypothesizer.md", "fence": "none"},
                      "prober": {"file": "roles/prober.md", "fence": "sandbox"}}}
ROLES = ("hypothesizer", "prober")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    for k in list(os.environ):
        if k.startswith(("QWEN_SWARM_", "QWEN_DR_", "QA_CFG_")) or k in (
                "QWEN_PLAN", "QWEN_ADVISOR", "QWEN_RESCUE", "QWEN_CONFIRM"):
            monkeypatch.delenv(k)
    return d


def wf_dir(tmp_path, manifest_over=None):
    m = json.loads(json.dumps(MANIFEST))
    m.update(manifest_over or {})
    return str(make_workflow(tmp_path / "wfs", manifest=m, script=SCRIPT, roles=ROLES))


def go(tmp_path, *extra, out="run"):
    repo = tmp_path / "repo"
    if not repo.exists():
        git_repo(repo, {"a.txt": "1\n"})
    runner._MODULES.clear()
    return runner.main(agent_args() + [wf_dir(tmp_path), "g", "--target", str(repo),
                                       "--out", str(tmp_path / out)] + list(extra))


def argv_of(fake, unit_prefix):
    for a in calls(fake):
        if os.path.basename(a[a.index("-C") + 1]).startswith(unit_prefix):
            return a
    raise AssertionError("no %s unit in %r" % (unit_prefix, calls(fake)))


def val(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_swarm_no_config_all_off(tmp_path, fake):
    assert go(tmp_path) == 0
    h, p = argv_of(fake, "hyp"), argv_of(fake, "probe")
    assert val(h, "--advisor") == "off" and val(h, "--plan") == "off"
    assert val(p, "--advisor") == "off" and val(p, "--plan") == "off"


def test_swarm_config_reaches_roles(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("QA_CFG_ADVISOR", "opus")
    monkeypatch.setenv("QA_CFG_PLAN", "opus")
    assert go(tmp_path) == 0
    h, p = argv_of(fake, "hyp"), argv_of(fake, "probe")
    assert val(h, "--advisor") == "opus" and val(h, "--plan") == "off"
    assert val(p, "--advisor") == "opus" and val(p, "--plan") == "opus"


def test_swarm_role_override_off(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("QA_CFG_ADVISOR", "opus")
    assert go(tmp_path, "--claude", "prober:advisor=off") == 0
    assert val(argv_of(fake, "probe"), "--advisor") == "off"
    assert val(argv_of(fake, "hyp"), "--advisor") == "opus"


def test_swarm_role_override_on(tmp_path, fake):
    assert go(tmp_path, "--claude", "hypothesizer:advisor=sonnet") == 0
    assert val(argv_of(fake, "hyp"), "--advisor") == "sonnet"
    assert val(argv_of(fake, "probe"), "--advisor") == "off"


def test_swarm_claude_repeatable_and_plan_on_write_role(tmp_path, fake):
    assert go(tmp_path, "--claude", "prober:plan=opus,advisor=sonnet",
              "--claude", "hypothesizer:advisor=opus") == 0
    p = argv_of(fake, "probe")
    assert val(p, "--plan") == "opus" and val(p, "--advisor") == "sonnet"
    assert val(argv_of(fake, "hyp"), "--advisor") == "opus"


@pytest.mark.parametrize("spec,word", [("boss:advisor=opus", "boss"),
                                       ("hypothesizer:plan=opus", "hypothesizer"),
                                       ("prober:rescue=opus", "rescue"),
                                       ("prober:advisor=bad model", "advisor"),
                                       ("prober", "prober")])
def test_swarm_claude_bad_role_or_key(tmp_path, fake, capsys, spec, word):
    assert go(tmp_path, "--claude", spec) == 2
    err = capsys.readouterr().err
    assert "--claude" in err and word in err
    assert calls(fake) == []


def test_manifest_cannot_enable_claude(tmp_path):
    m = json.loads(json.dumps(MANIFEST))
    m["roles"]["prober"]["advisor"] = "opus"
    with pytest.raises(manifest.ManifestError, match="unknown key"):
        manifest.validate({**json.loads(json.dumps(__import__("swarm_fixtures").BASE)), **m},
                          make_workflow(tmp_path / "wfs", manifest=MANIFEST, script=SCRIPT,
                                        roles=ROLES))


def test_swarm_show_claude(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("QA_CFG_ADVISOR", "opus")
    monkeypatch.setenv("QA_CFG_PLAN", "sonnet")
    runner._MODULES.clear()
    assert runner.main(agent_args() + [wf_dir(tmp_path), "--show-claude",
                                       "--claude", "prober:advisor=off"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "hypothesizer: plan=n/a advisor=opus rescue=n/a" in out
    assert "prober: plan=sonnet advisor=off rescue=n/a" in out
    assert calls(fake) == []


def test_swarm_startup_prints_the_same_lines(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("QA_CFG_ADVISOR", "opus")
    assert go(tmp_path) == 0
    err = capsys.readouterr().err
    assert "hypothesizer: plan=n/a advisor=opus rescue=n/a" in err
    assert "prober: plan=off advisor=opus rescue=n/a" in err


def test_swarm_env_cannot_enable(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("QWEN_ADVISOR", "opus")
    monkeypatch.setenv("QWEN_PLAN", "opus")
    assert go(tmp_path) == 0
    assert val(argv_of(fake, "hyp"), "--advisor") == "off"
    assert val(argv_of(fake, "probe"), "--advisor") == "off"
    assert val(argv_of(fake, "probe"), "--plan") == "off"


def test_swarm_cache_key_includes_claude(tmp_path):
    role = tmp_path / "r.md"
    role.write_text("role", encoding="utf-8")
    sw = swarm.Swarm(["x"], tmp_path / "run", 1, 10)
    keys = {sw._key(swarm.Unit("u", role, "p", claude=c))
            for c in (None, {"advisor": "off"}, {"advisor": "opus"}, {"advisor": "sonnet"},
                      {"advisor": "opus", "plan": "opus"})}
    assert len(keys) == 4          # None and all-off share the pre-existing key


def test_resume_changes_claude_and_reruns_the_unit(tmp_path, fake):
    assert go(tmp_path) == 0
    n = len(calls(fake))
    runner._MODULES.clear()
    assert runner.main(agent_args() + ["--resume", str(tmp_path / "run"),
                                       "--claude", "hypothesizer:advisor=opus"]) == 0
    new = calls(fake)[n:]
    assert len(new) == 1 and val(new[0], "--advisor") == "opus"


def test_show_claude_on_resume(tmp_path, fake, capsys):
    assert go(tmp_path, "--claude", "prober:advisor=sonnet") == 0
    n = len(calls(fake))
    capsys.readouterr()
    runner._MODULES.clear()
    assert runner.main(agent_args() + ["--resume", str(tmp_path / "run"), "--show-claude",
                                       "--claude", "hypothesizer:advisor=opus"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "hypothesizer: plan=n/a advisor=opus rescue=n/a" in out
    assert "prober: plan=off advisor=sonnet rescue=n/a" in out        # the stored one, merged
    assert len(calls(fake)) == n                                      # nothing launched


def test_show_claude_on_resume_sees_a_config_change(tmp_path, fake, monkeypatch, capsys):
    assert go(tmp_path) == 0
    capsys.readouterr()
    monkeypatch.setenv("QA_CFG_ADVISOR", "opus")           # the config file changed since
    runner._MODULES.clear()
    assert runner.main(agent_args() + ["--resume", str(tmp_path / "run"), "--show-claude"]) == 0
    assert "hypothesizer: plan=n/a advisor=opus rescue=n/a" in capsys.readouterr().out.splitlines()


CHECK_SCRIPT = '''
def run(wf):
    res = wf.claude_check("c", [{"id": "A"}], lambda it: "q", lambda t, it: t,
                          model="opus", max_calls=2)
    wf.report("states: %s | %s\\n" % (res[0]["state"], res[0]["why"]))
'''


def _check_run(tmp_path, fake, *extra):
    marker = tmp_path / "claude-ran"
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\necho ran >> %s\nexit 1\n" % marker, encoding="utf-8")
    claude.chmod(0o755)
    wfd = str(make_workflow(tmp_path / "wfs", manifest=MANIFEST, script=CHECK_SCRIPT,
                            roles=ROLES))
    repo = tmp_path / "repo"
    git_repo(repo, {"a.txt": "1\n"})
    runner._MODULES.clear()
    rc = runner.main(agent_args() + [wfd, "g", "--target", str(repo),
                                     "--out", str(tmp_path / "run")] + list(extra))
    return rc, marker


@pytest.mark.skipif(os.name == "nt", reason="sh stub")
def test_custom_workflow_claude_check_refused_without_config(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("QWEN_CLAUDE_BIN", str(tmp_path / "claude"))
    rc, marker = _check_run(tmp_path, fake)
    assert not marker.exists()
    report = next((tmp_path / "run").glob("report*.md")).read_text(encoding="utf-8")
    assert "unavailable" in report and "Claude is off for this run" in report


def test_show_claude_lists_claude_checks(tmp_path, fake, monkeypatch, capsys):
    runner._MODULES.clear()
    assert runner.main(agent_args() + [wf_dir(tmp_path), "--show-claude"]) == 0
    assert "claude checks: off" in capsys.readouterr().out.splitlines()
    monkeypatch.setenv("QA_CFG_CONFIRM", "sonnet")
    runner._MODULES.clear()
    assert runner.main(agent_args() + [wf_dir(tmp_path), "--show-claude"]) == 0
    assert "claude checks: sonnet" in capsys.readouterr().out.splitlines()


def test_startup_summary_lists_claude_checks_and_tools(tmp_path, fake, monkeypatch, capsys):
    assert go(tmp_path) == 0
    assert "claude checks: off" in capsys.readouterr().err
    monkeypatch.setenv("QA_CFG_ADVISOR", "opus")
    monkeypatch.setenv("QWEN_CLAUDE_TOOLS", "0")
    assert go(tmp_path, out="run2") == 0
    assert "read tools (plan/advisor/rescue) off" in capsys.readouterr().err


def test_show_claude_reflects_tools_off(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("QWEN_CLAUDE_TOOLS", "0")
    runner._MODULES.clear()
    assert runner.main(agent_args() + [wf_dir(tmp_path), "--show-claude"]) == 0
    assert "claude read tools (plan/advisor/rescue): off" in capsys.readouterr().out.splitlines()


def test_every_unit_argv_has_rescue_off_and_plan_off_on_read_roles(tmp_path, fake):
    assert go(tmp_path) == 0
    h, p = argv_of(fake, "hyp"), argv_of(fake, "probe")
    for a in (h, p):
        assert val(a, "--rescue") == "off"
    assert val(h, "--plan") == "off"                  # a read role: no real --plan
    assert val(p, "--plan") == "off"


def test_real_plan_stays_and_resume_repair_gets_plan_off(tmp_path):
    role = tmp_path / "r.md"
    role.write_text("role", encoding="utf-8")
    sw = swarm.Swarm(["x"], tmp_path / "run", 1, 10)
    u = swarm.Unit("u", role, "p", claude={"plan": "opus", "advisor": "off"})
    first = sw._argv(u, tmp_path / "p.md", 60)
    repair = sw._argv(u, tmp_path / "p.md", 60, resume="sess")
    assert val(first, "--plan") == "opus" and val(first, "--rescue") == "off"
    assert val(repair, "--plan") == "off" and val(repair, "--rescue") == "off"


def test_reason_line_skips_claude_line_and_notice():
    text = ("claude: plan=skipped (read-only run) advisor=off rescue=off tools=off\n"
            "qwen-agent: Claude settings changed (shown once)\n"
            "  Claude is now OFF\n    @x\n\n"
            "error: the real reason\nmore\n")
    assert swarm.reason_line(text) == "error: the real reason"
    assert swarm.reason_line("", "dflt") == "dflt"


@pytest.mark.skipif(os.name == "nt", reason="needs bash")
def test_failing_preflight_reason_is_the_real_error_even_with_config_rescue(
        tmp_path, monkeypatch, capsys):
    import pathlib
    agent = pathlib.Path(__file__).resolve().parent.parent / "skill" / "local-auditor" / "qwen-agent.sh"
    cfg = tmp_path / "cfg"
    cfg.write_text("QWEN_RESCUE=opus\nQWEN_ADVISOR=opus\nQWEN_PLAN=opus\n", encoding="utf-8")
    monkeypatch.setenv("QWEN_CONFIG", str(cfg))
    monkeypatch.setenv("QWEN_NO_NOTICE", "")                    # the notice block shows too
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_BASE_URL", "http://127.0.0.1:9/v1")  # nothing listens there
    monkeypatch.setenv("QWEN_API_KEY", "x")
    assert runner.preflight(["bash", str(agent)]) is False
    err = capsys.readouterr().err
    assert err.startswith("qwen-swarm: preflight: model:") or "preflight: model:" in err
    reason = err.split("preflight: model:", 1)[1]
    assert "claude:" not in reason and "Claude settings changed" not in reason


GATE_SCRIPT = '''
def run(wf):
    wf.cfg["claude_confirm"] = "opus"
    wf.cfg["claude_typed_confirm"] = True
    wf.cfg["confirm"] = "claude"
    wf.cfg["claude_default"] = {"advisor": "opus", "plan": "opus"}
    wf.cfg["claude"] = {"hypothesizer": {"advisor": "opus"}}
    res = wf.claude_check("c", [{"id": "A"}], lambda it: "q", lambda t, it: t,
                          model="opus", max_calls=2)
    wf.report("states: %s | advisor=%s\\n" % (res[0]["state"], wf._claude("hypothesizer")["advisor"]))
'''


@pytest.mark.skipif(os.name == "nt", reason="sh stub")
def test_workflow_cannot_open_claude_gate_via_cfg(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("QWEN_CLAUDE_BIN", str(tmp_path / "claude"))
    marker = tmp_path / "claude-ran"
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\necho ran >> %s\nexit 1\n" % marker, encoding="utf-8")
    claude.chmod(0o755)
    wfd = str(make_workflow(tmp_path / "wfs", manifest=MANIFEST, script=GATE_SCRIPT, roles=ROLES))
    repo = tmp_path / "repo"
    git_repo(repo, {"a.txt": "1\n"})
    runner._MODULES.clear()
    assert runner.main(agent_args() + [wfd, "g", "--target", str(repo),
                                       "--out", str(tmp_path / "run")]) == 0
    assert not marker.exists()
    report = next((tmp_path / "run").glob("report*.md")).read_text(encoding="utf-8")
    assert "states: unavailable" in report and "advisor=off" in report
    saved = json.loads((tmp_path / "run" / "config.json").read_text(encoding="utf-8"))
    assert not saved.get("claude_confirm")


HOOK_SCRIPT = '''
def run(wf):
    wf.report("conf=%r typed=%r checks=%s\\n" % (wf.cfg.get("claude_confirm"),
              wf.cfg.get("claude_typed_confirm"), wf._claude("hypothesizer")["advisor"]))

def claude_config(cfg, sets, config_confirm):
    cfg["claude_typed_confirm"] = True
    cfg["claude_confirm"] = "opus"
    cfg["confirm"] = "claude"
    cfg["claude_default"] = {"advisor": "opus"}
    return []
'''


def test_claude_config_hook_cannot_enable_confirm(tmp_path, fake, capsys):
    wfd = str(make_workflow(tmp_path / "wfs", manifest=MANIFEST, script=HOOK_SCRIPT, roles=ROLES))
    repo = tmp_path / "repo"
    git_repo(repo, {"a.txt": "1\n"})
    runner._MODULES.clear()
    assert runner.main(agent_args() + [wfd, "g", "--target", str(repo),
                                       "--out", str(tmp_path / "run")]) == 0
    report = next((tmp_path / "run").glob("report*.md")).read_text(encoding="utf-8")
    assert "conf='' typed=False checks=off" in report
    saved = json.loads((tmp_path / "run" / "config.json").read_text(encoding="utf-8"))
    assert not saved["claude_confirm"] and saved["claude_typed_confirm"] is False
    assert not saved.get("claude_default")
    assert "claude checks: off" in capsys.readouterr().err


VALIDATE_SCRIPT = '''
def validate(cfg):
    cfg.setdefault("claude_default", {}).update({"advisor": "opus", "plan": "opus"})
    cfg.setdefault("claude", {}).setdefault("hypothesizer", {})["advisor"] = "opus"
    return None

def run(wf):
    wf.fan_out("hyp", "hypothesizer", [{"id": "H1"}], lambda b: "think",
               lambda text, batch: [{"id": "H1"}])
    wf.report("done\\n")
'''


def test_validate_cannot_enable_claude(tmp_path, fake):
    wfd = str(make_workflow(tmp_path / "wfs", manifest=MANIFEST, script=VALIDATE_SCRIPT,
                            roles=ROLES))
    repo = tmp_path / "repo"
    git_repo(repo, {"a.txt": "1\n"})
    runner._MODULES.clear()
    assert runner.main(agent_args() + [wfd, "g", "--target", str(repo),
                                       "--out", str(tmp_path / "run")]) == 0
    assert val(argv_of(fake, "hyp"), "--advisor") == "off"
    assert val(argv_of(fake, "hyp"), "--plan") == "off"
    saved = json.loads((tmp_path / "run" / "config.json").read_text(encoding="utf-8"))
    assert not saved.get("claude_default")
    assert not saved.get("claude")
