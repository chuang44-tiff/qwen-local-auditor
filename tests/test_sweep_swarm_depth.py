"""Depth is the default for sweeps and swarms; shallow is the opt-in.

What qwen-sweep and qwen-swarm hand qwen-agent is read off the record, not off the
source: the fake dispatcher of test_sweep_cli records every argument of every batch, and
the fake swarm agent records every unit's argv. A unit never gets an implied --probe --
qwen-agent is always told --shallow and then the depth switches this run decided on.
"""
import hashlib
import json
import pathlib
import sys

import pytest

import test_sweep_cli
import test_swarm_runner
from lib import swarm
from lib.swarm_engine import manifest, runner
from swarm_fixtures import FAKE, agent_args, calls, make_workflow
from test_sweep_cli import files_args, posix, sweep

# the fakes and fixtures the two suites already stand up: the recording dispatcher for
# the sweep side, the recording agent (and the signal cleanup a runner.main needs) for
# the swarm side
repo = test_sweep_cli.repo
dispatch = test_sweep_cli.dispatch
fake = test_swarm_runner.fake
_restore_signal_handlers = test_swarm_runner._restore_signal_handlers

DEPTH_ON = ("--review-round", "--subagents-nudge")
NOT_FOR_A_UNIT = DEPTH_ON + ("--role-variant", "--deep", "--probe")


def batches(rec):
    """Every recorded batch command of a sweep: one list of argv items per batch."""
    text = pathlib.Path(rec).read_text(encoding="utf-8")
    return [c.strip("\n").split("\n") for c in text.split("---\n") if c.strip()]


def role_of(argv):
    return pathlib.Path(argv[argv.index("--role-file") + 1]).stem


# ---------------------------------------------------------------- the sweep side

def test_sweep_dispatch_defaults_deep_without_probe(tmp_path, repo, dispatch):
    # depth is typed onto every batch -- and typed with --shallow, because the depth qwen-agent
    # would imply for itself includes --probe, whose sandbox is built from a project tree
    # while a batch's -C is a folder of extracted text. --role-variant deep goes only to a
    # role that has a deep variant.
    for role, variant in (("auditor", "deep"), ("coder", "deep"), ("tester", None)):
        out, rec = tmp_path / ("run-" + role), tmp_path / ("rec-%s.txt" % role)
        r = sweep(tmp_path, files_args(repo, out) + ["--role", role], dispatch,
                  extra={"FAKE_RECORD": posix(rec)})
        assert r.returncode == 0, r.stdout + r.stderr
        got = batches(rec)
        assert got, "nothing was dispatched for --role %s" % role
        for argv in got:
            assert argv.count("--shallow") == 1, argv
            assert all(a in argv for a in DEPTH_ON), argv
            assert (argv.index("--shallow") < argv.index("--review-round")
                    < argv.index("--subagents-nudge")), argv
            assert ("--role-variant" in argv) is (variant is not None), argv
            if variant:
                assert argv[argv.index("--role-variant") + 1] == variant, argv
            assert "--probe" not in argv and "--deep" not in argv, argv


def test_sweep_shallow_option(tmp_path, repo, dispatch):
    # --shallow is the opt-out meant for a large fan-out run: the batches get plain
    # --shallow, one answer each, exactly as a sweep ran before depth became the default.
    out, rec = tmp_path / "run", tmp_path / "rec.txt"
    r = sweep(tmp_path, files_args(repo, out) + ["--shallow"], dispatch,
              extra={"FAKE_RECORD": posix(rec)})
    assert r.returncode == 0, r.stdout + r.stderr
    got = batches(rec)
    assert got, "nothing was dispatched"
    for argv in got:
        assert argv.count("--shallow") == 1, argv
        assert not [a for a in NOT_FOR_A_UNIT if a in argv], argv


def test_sweep_shallow_is_documented_in_the_help(tmp_path):
    r = sweep(tmp_path, ["--help"])
    assert r.returncode == 0 and "--shallow" in r.stdout and "fan-out" in r.stdout


# ---------------------------------------------------------------- the swarm side

def role_workflow(tmp_path, deep=None):
    """The manifest of a one-role workflow: the role's "deep" field only when given."""
    worker = {"file": "roles/worker.md", "fence": "none"}
    if deep is not None:
        worker = dict(worker, deep=deep)
    return manifest.load(make_workflow(tmp_path / "wfs", {"roles": {"worker": worker}}))


def unit_from(tmp_path, role, mcp=None):
    """The Unit a manifest role turns into: the fence fields of a `none` fence, the
    depth the role asked for, and nothing else."""
    u = swarm.Unit(name="work-1", role_file=role.file, prompt="do it", toolset="none",
                   grants="", web=False, mcp_config=mcp, parse=swarm.extract_json,
                   deep=role.deep)
    return u, swarm.Swarm([sys.executable, str(FAKE)], tmp_path / "run", seats=1,
                          timeout=60, backoff=0)


def test_swarm_role_default_is_deep(tmp_path, fake):
    # a role that says nothing about depth is deep: the unit is handed --shallow (so
    # qwen-agent implies nothing) and then both switches.
    out = tmp_path / "run"
    folder = make_workflow(tmp_path / "wfs")
    assert runner.main(agent_args() + [str(folder), "g", "--out", str(out)]) == 0
    got = calls(fake)
    assert got, "no unit ran"
    for argv in got:
        assert argv.count("--shallow") == 1, argv
        assert all(a in argv for a in DEPTH_ON), argv
        assert argv.index("--shallow") < argv.index("--review-round") < argv.index("--subagents-nudge"), argv
        assert "--probe" not in argv and "--deep" not in argv, argv


def test_swarm_deep_false_is_shallow(tmp_path):
    # "deep": false is the opt-out, and its cache key is byte-identical to the released
    # one: the switches enter the key only when they are set, and --shallow is not one.
    m = role_workflow(tmp_path, deep=False)
    assert m.roles["worker"].deep == ()
    u, sw = unit_from(tmp_path, m.roles["worker"])
    argv = sw._argv(u, tmp_path / "p.md", 600)
    assert argv.count("--shallow") == 1, argv
    assert not [a for a in NOT_FOR_A_UNIT if a in argv], argv
    role_text = pathlib.Path(u.role_file).read_text(encoding="utf-8")
    blob = "%s\n%s\n%s\n%s\n%s\n%s\n%s" % (role_text, u.prompt, u.toolset, u.grants, u.web,
                                           "", u.effort or "")
    assert sw._key(u) == hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_swarm_deep_default_and_list_reach_the_same_argv(tmp_path):
    m_default, m_true = role_workflow(tmp_path), role_workflow(tmp_path, deep=True)
    assert m_default.roles["worker"].deep == manifest.DEEP_SWITCHES
    assert m_true.roles["worker"].deep == manifest.DEEP_SWITCHES
    assert m_default.roles["worker"].deep == ("review_round", "subagents")
    listed = role_workflow(tmp_path, deep=["subagents"])
    assert listed.roles["worker"].deep == ("subagents",)


TWO_ROLES = {"worker": {"file": "roles/worker.md", "fence": "none"},
             "voter": {"file": "roles/voter.md", "fence": "none"}}
TWO_SCRIPT = '''
def run(wf):
    items = [{"id": "I%d" % i} for i in range(1, wf.knob("items") + 1)]
    rows = []
    for role in ("worker", "voter"):
        res = wf.fan_out(role, role, items,
                         lambda batch: "items:\\n" + "\\n".join("- %s:" % it["id"] for it in batch),
                         lambda text, batch: [{"id": it["id"]} for it in batch])
        rows += res.rows
    wf.save("rows", rows)
    wf.report("# Echo\\n\\n%d rows\\n" % len(rows))
'''


def test_swarm_cli_shallow_merges_on_resume(tmp_path, fake):
    folder = make_workflow(tmp_path / "wfs", {"roles": TWO_ROLES}, script=TWO_SCRIPT,
                           roles=("worker", "voter"))
    out = tmp_path / "run"
    assert runner.main(agent_args() + [str(folder), "g", "--out", str(out),
                                       "--shallow", "worker"]) == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["shallow"] == ["worker"] and "deep" not in cfg
    first = calls(fake)
    assert [role_of(a) for a in first] and all(a.count("--shallow") == 1 for a in first)
    assert all(("--review-round" in a) is (role_of(a) == "voter") for a in first)
    # a resume adds to the stored list, and only the units whose depth changed run again
    assert runner.main(agent_args() + ["--resume", str(out), "--shallow", "voter"]) == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["shallow"] == ["voter", "worker"]
    later = calls(fake)[len(first):]
    assert later and all(role_of(a) == "voter" for a in later)
    assert all("--review-round" not in a and "--subagents-nudge" not in a for a in later)


def test_shallow_wins_over_deep_same_role(tmp_path, fake):
    # --deep worker --shallow worker in one run: the shallow list is checked first,
    # so the role runs plain even though both stored lists name it.
    folder = make_workflow(tmp_path / "wfs")
    out = tmp_path / "run"
    assert runner.main(agent_args() + [str(folder), "g", "--out", str(out),
                                       "--deep", "worker", "--shallow", "worker"]) == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["deep"] == ["worker"] and cfg["shallow"] == ["worker"]
    got = calls(fake)
    assert got, "no unit ran"
    for argv in got:
        assert argv.count("--shallow") == 1, argv
        assert not [a for a in NOT_FOR_A_UNIT if a in argv], argv


def test_shallow_wins_over_stored_deep_on_resume(tmp_path, fake):
    # The stored lists only grow on --resume: a role made shallow in run 1 stays
    # shallow in run 2 even though the resume --deep-names it (shallow wins). Had
    # deep won, the unit's cache key would change and it would run again WITH the
    # switches; staying shallow means the resume re-runs nothing.
    folder = make_workflow(tmp_path / "wfs")
    out = tmp_path / "run"
    assert runner.main(agent_args() + [str(folder), "g", "--out", str(out),
                                       "--shallow", "worker"]) == 0
    first = calls(fake)
    assert first and all("--review-round" not in a and "--subagents-nudge" not in a
                         for a in first)
    assert runner.main(agent_args() + ["--resume", str(out), "--deep", "worker"]) == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["deep"] == ["worker"] and cfg["shallow"] == ["worker"]
    assert calls(fake)[len(first):] == []                    # shallow still won: all cached


@pytest.mark.parametrize("flag", ["--deep", "--shallow"])
def test_the_released_research_command_takes_neither_depth_flag(flag):
    # qwen-deep-research is this engine with a released CLI: neither flag was released,
    # and adding one to it would change what its own usage text promises
    with pytest.raises(SystemExit):
        runner.parse_args(agent_args() + ["q", flag, "all"], compat="deep-research")


# ---------------------------------------------------------------- the built-ins

def builtin_json(name):
    root = pathlib.Path(manifest.__file__).resolve().parents[1] / "workflows" / name
    return json.loads((root / "workflow.json").read_text(encoding="utf-8"))


def test_research_fanout_roles_are_shallow():
    # the high fan-out roles answer once (each is one more request to the model), and the
    # roles that shape the report keep the default depth
    doc = builtin_json("research")
    for role in ("searcher", "reader", "verifier"):
        assert doc["roles"][role]["deep"] is False, role
    m = manifest.load(runner.BUILTIN / "research")
    for role in ("searcher", "reader", "verifier"):
        assert m.roles[role].deep == (), role
    for role in ("scoper", "planner", "synthesizer"):
        assert m.roles[role].deep == manifest.DEEP_SWITCHES, role
    # debug has no high fan-out role: every one of its roles keeps the new default
    debug = manifest.load(runner.BUILTIN / "debug")
    assert "deep" not in builtin_json("debug")["roles"]["prober"]
    assert all(r.deep == manifest.DEEP_SWITCHES for r in debug.roles.values())
