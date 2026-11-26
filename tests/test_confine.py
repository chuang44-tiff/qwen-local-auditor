"""Confinement of swarm units whose grants include Bash (spec 13): the settings block,
the scoped edit grants, the launcher's one-file merge, and what the report says when the
host cannot confine."""
import json
import os
import pathlib
import shutil
import sys
import tempfile

import pytest

import test_cli
from lib import swarm
from lib.swarm_engine import api, fences, manifest, runner
from swarm_fixtures import FAKE, agent_args, git_repo, make_workflow

ROLES = {"worker": {"file": "roles/worker.md", "fence": "none"},
         "reader": {"file": "roles/reader.md", "fence": "read"},
         "webby": {"file": "roles/webby.md", "fence": "web"},
         "prober": {"file": "roles/prober.md", "fence": "sandbox"}}

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


@pytest.fixture
def fake_claude(tmp_path):
    p = tmp_path / "fake-claude"
    p.write_text(test_cli.FAKE_CLAUDE, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    return p


def _workflow(tmp_path, run_dir=None):
    """A Workflow over the four fences; run_dir lets the caller place the run folder.
    The manifest says target required (a read/sandbox fence needs it); cfg keeps target
    None, so no fingerprinting or sandboxing happens at unit-build time."""
    folder = make_workflow(tmp_path / "wfs", {"target": "required", "roles": ROLES},
                           roles=tuple(ROLES))
    m = manifest.load(folder)
    cfg = {"goal": "g", "depth": "quick", "items": 2, "max_agents": 2, "max_items": 2,
           "timeout_per_item": 100, "retries": 0, "effort": None, "role_effort": {},
           "hours": None, "deadline": None, "rounds": 1, "target": None,
           "claude_confirm": "opus"}
    run = pathlib.Path(run_dir) if run_dir is not None else tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    mcp = run / "mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    sw = swarm.Swarm([sys.executable, str(FAKE)], run, seats=2, timeout=60, backoff=0)
    return api.Workflow(m, cfg, run, sw, goal="g", mcp=mcp, web_seats=1)


def _deny_list(R):
    return ["Edit(/%s/%s/**)" % (R, d) for d in ("inbox", "msg", "asks", "agents")]


def _dry_argv(r):
    """The argv lines of a --dry-run, without the brackets."""
    return [ln.strip()[1:-1] for ln in r.stdout.splitlines() if ln.startswith("  [")]


def test_confine_settings_shape(tmp_path):
    run = tmp_path / "x" / "run"
    run.mkdir(parents=True)
    R = os.path.realpath(str(run))
    cfg = fences.confine_settings(str(run), "u1")
    assert cfg["sandbox"] == {"enabled": True, "allowUnsandboxedCommands": False,
                              "autoAllowBashIfSandboxed": True}
    assert cfg["permissions"] == {"deny": _deny_list(R)}
    assert list(cfg) == ["sandbox", "permissions"]
    for rule in cfg["permissions"]["deny"]:
        assert rule.startswith("Edit(//") and rule.endswith("/**)"), rule
    joined = " ".join(cfg["permissions"]["deny"])
    for d in ("inbox", "msg", "asks", "agents"):
        assert "/%s/**)" % d in joined, d
    plain = fences.confine_settings(str(run), "u1", sandbox=False)
    assert "sandbox" not in plain
    assert plain == {"permissions": {"deny": _deny_list(R)}}


def test_bash_unit_scoped_grants(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(fences, "confine_probe", lambda run_dir: None)   # confined host
    wf = _workflow(tmp_path)
    R = os.path.realpath(str(wf.run_dir))
    scoped = "Edit(/%s/sandboxes/u1/**)" % R
    assert fences.scoped_grants("Read,Edit,Write,Bash,Glob,Grep", str(wf.run_dir),
                                "u1") == "Read,%s,Bash,Glob,Grep" % scoped
    assert fences.scoped_grants("Read,Glob,Grep", str(wf.run_dir), "u1") == "Read,Glob,Grep"
    u = wf._unit("u1", "prober", "p", 300, lambda text, batch, patch: [], None)
    assert u.grants == "Read,%s,Bash,Glob,Grep" % scoped
    assert u.toolset == "Read,Edit,Write,Bash,Glob,Grep"        # tools stay available
    assert u.settings_extra == fences.confine_settings(str(wf.run_dir), "u1")
    argv = wf.sw._argv(u, wf.sw.agents_dir / "u1.prompt.md", 300)
    assert argv.count("--settings-extra") == 1
    sfile = argv[argv.index("--settings-extra") + 1]
    assert pathlib.Path(sfile) == wf.sw.agents_dir / "u1.settings.json"
    assert json.loads(pathlib.Path(sfile).read_text(encoding="utf-8")) == u.settings_extra
    assert argv[argv.index("-t") + 1] == u.grants
    if os.name != "nt":
        assert os.stat(sfile).st_mode & 0o777 == 0o600
    # the repair call (a resume of the same session) gets the same file
    rargv = wf.sw._argv(u, wf.sw.agents_dir / "u1.repair.md", 300, resume="sess-1")
    assert rargv.count("--settings-extra") == 1
    assert rargv[rargv.index("--settings-extra") + 1] == sfile


def test_non_bash_unit_unchanged(tmp_path):
    wf = _workflow(tmp_path)
    for role, fence in (("worker", "none"), ("reader", "read"), ("webby", "web")):
        fields = fences.unit_fields(fence, wf.mcp if fence in fences.WEB_FENCES else None)
        u = wf._unit("u-" + role, role, "p", 300, swarm.extract_json, None)
        assert u.grants == fields["grants"] and u.toolset == fields["toolset"]
        assert u.settings_extra is None
        argv = wf.sw._argv(u, wf.sw.agents_dir / "x.prompt.md", 300)
        assert "--settings-extra" not in argv
        # the key is the pre-confinement one: an identical Unit that never saw
        # confinement hashes the same (nothing new rides in the key)
        plain = swarm.Unit(name=u.name, role_file=u.role_file, prompt=u.prompt,
                           toolset=u.toolset, grants=u.grants, web=u.web,
                           mcp_config=u.mcp_config, effort=u.effort,
                           key_extra=u.key_extra, deep=u.deep, browser=u.browser,
                           browser_eval=u.browser_eval, claude=u.claude)
        assert wf.sw._key(u) == wf.sw._key(plain)


def test_settings_merged_into_one_file(tmp_path, fake_claude):
    run = tmp_path / "run"
    run.mkdir()
    extra = tmp_path / "confine.json"
    conf = fences.confine_settings(str(run), "u1")
    extra.write_text(json.dumps(conf), encoding="utf-8")
    # watch activity on (the env the activity-hook tests run with), settings-extra too:
    # ONE --settings, and that file holds the watch hooks AND the sandbox AND the deny
    # list. The flags come BEFORE the prompt: the parse loop's positional arm swallows
    # everything from the first non-flag on into the prompt and breaks out.
    r = test_cli.run(tmp_path, ["--dry-run", "-q", "--settings-extra",
                                test_cli.posix(extra), "hi"],
                     fake=fake_claude,
                     extra={"QWEN_WATCH_ACTIVITY": test_cli.posix(tmp_path / "act.jsonl")})
    assert r.returncode == 0, r.stderr
    argv = _dry_argv(r)
    assert argv.count("--settings") == 1, argv
    settings = test_cli.flag(argv, "--settings")
    assert settings
    cfg = json.loads(pathlib.Path(test_cli.same_path(settings)).read_text(encoding="utf-8"))
    assert cfg["sandbox"] == {"enabled": True, "allowUnsandboxedCommands": False,
                              "autoAllowBashIfSandboxed": True}
    assert cfg["permissions"]["deny"] == conf["permissions"]["deny"]
    hooks = cfg.get("hooks")
    assert isinstance(hooks, dict), cfg
    for kind in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"):
        cmds = [h.get("command", "") for e in hooks.get(kind, []) for h in e.get("hooks", [])]
        assert any("watch_hook.py" in c for c in cmds), (kind, hooks)
    # and once with nothing else: FILE's content alone, under the run's temp folder
    r2 = test_cli.run(tmp_path, ["--dry-run", "-q", "--settings-extra",
                                 test_cli.posix(extra), "hi"],
                      fake=fake_claude)
    assert r2.returncode == 0, r2.stderr
    argv2 = _dry_argv(r2)
    assert argv2.count("--settings") == 1, argv2
    s2 = test_cli.flag(argv2, "--settings")
    assert s2 and pathlib.Path(test_cli.same_path(s2)).parent.name.startswith("qwen-agent.")
    assert json.loads(pathlib.Path(test_cli.same_path(s2)).read_text(encoding="utf-8")) == conf
    # a FILE that is not a JSON object exits 2, and so does one that cannot be read
    extra.write_text("[1, 2]", encoding="utf-8")
    r3 = test_cli.run(tmp_path, ["--dry-run", "-q", "--settings-extra",
                                 test_cli.posix(extra), "hi"],
                      fake=fake_claude)
    assert r3.returncode == 2, r3.stderr
    r4 = test_cli.run(tmp_path, ["--dry-run", "-q", "--settings-extra",
                                 test_cli.posix(tmp_path / "nope.json"), "hi"],
                      fake=fake_claude)
    assert r4.returncode == 2, r4.stderr


@pytest.mark.skipif(os.name == "nt", reason="the TMPDIR rule is POSIX's")
def test_run_under_tmpdir_not_confined(tmp_path):
    base = tempfile.mkdtemp(prefix="qla-confine-")
    try:
        run = pathlib.Path(base) / "run"
        assert fences.confine_probe(str(run)) == "run folder under TMPDIR"
        wf = _workflow(tmp_path, run_dir=run)
        u = wf._unit("u1", "prober", "p", 300, lambda text, batch, patch: [], None)
        R = os.path.realpath(str(run))
        assert u.settings_extra == {"permissions": {"deny": _deny_list(R)}}
        assert "sandbox" not in u.settings_extra
        assert u.grants == "Read,Edit(/%s/sandboxes/u1/**),Bash,Glob,Grep" % R
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the bwrap probe is Linux's")
@pytest.mark.skipif(shutil.which("git") is None, reason="the sandbox fence needs git")
def test_probe_failure_reported(tmp_path, fake, capsys, monkeypatch):
    bindir = tmp_path / "sandbox-bin"
    bindir.mkdir()
    bwrap = bindir / "bwrap"
    bwrap.write_text('#!/bin/sh\necho "bwrap: no permission" >&2\nexit 1\n',
                     encoding="utf-8", newline="\n")
    bwrap.chmod(0o755)
    socat = bindir / "socat"
    socat.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    socat.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    # the probe runs before the first confined unit; the run folder sits outside the
    # (pretend) temp folder so the TMPDIR rule does not answer first
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))
    assert fences.confine_probe(str(tmp_path / "r")) == \
        "sandbox probe failed: bwrap: no permission"
    (fake / "prober.py").write_text(PROBER, encoding="utf-8")
    repo = git_repo(tmp_path / "repo", {"a.txt": "1\n"})
    folder = make_workflow(tmp_path / "wfs2", {
        "target": "required",
        "roles": {"prober": {"file": "roles/prober.md", "fence": "sandbox"}}},
        script=SANDBOX_SCRIPT, roles=("prober",))
    out = tmp_path / "run"
    rc = runner.main(agent_args() + [str(folder), "g", "--target", str(repo),
                                     "--out", str(out)])
    assert rc == 0, (rc, (out / "run.log").read_text(encoding="utf-8")
                     if (out / "run.log").exists() else "")
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "bash not confined: sandbox probe failed: bwrap: no permission" in report
    err = capsys.readouterr().err
    assert err.count("swarm: bash not confined: sandbox probe failed: "
                     "bwrap: no permission") == 1, err
