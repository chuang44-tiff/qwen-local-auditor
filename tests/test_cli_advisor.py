"""qwen-agent --advisor MODEL: the wiring. The advisor server itself is test_advisor_mcp's."""
import json
import os
import shutil

import pytest

from test_cli import flag, posix, same_path
from test_cli_deep import _until_done_argv, calls, dirty_repo, go, sys_prompt
import test_cli_default_depth

# the shared fixtures, by assignment: pytest collects module-level fixture objects
fake = test_cli_default_depth.fake
server = test_cli_default_depth.server

DEEP_ENV = {"QWEN_DEPTH": ""}


def advisor_cfg(tmp_path, n=1):
    [p] = list((tmp_path / "calls").glob("cfg.%d.*advisor.json" % n))
    return json.loads(p.read_text())["mcpServers"]["qla_advisor"]


def test_advisor_wiring(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-q", "--advisor", "opus", "-C", posix(repo), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "advisor: opus via your claude login" in r.stderr          # even with -q
    assert "leave this machine" in r.stderr
    (a1, _), (a2, _) = calls(tmp_path)
    assert a1.count("--mcp-config") == 1 and a1[-2] == "--"
    assert "mcp__qla_advisor__ask" in flag(a1, "--allowed-tools").split(",")
    srv = advisor_cfg(tmp_path)
    assert srv["args"][-1].endswith("advisor_mcp.py")
    assert srv["env"]["QA_ADVISOR_MODEL"] == "opus"
    assert srv["env"]["QA_ADVISOR_MAX_CALLS"] == "4" and srv["env"]["QA_ADVISOR_TIMEOUT"] == "600"
    assert "ask a stronger model" in sys_prompt(a1) and "at most 4 calls this run" in sys_prompt(a1)
    assert "you may ask the advisor" in a2[-1]                          # the review round
    assert advisor_cfg(tmp_path, 2)["env"]["QA_ADVISOR_STATE"] == srv["env"]["QA_ADVISOR_STATE"]


def test_only_the_typed_flag_turns_it_on(tmp_path, server, fake):
    # v1: QWEN_ADVISOR (env or config file) is ignored, so no driver child, no shell
    # profile and no config line can send code to the cloud behind the person's back.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_ADVISOR="opus"))
    assert r.returncode == 0, r.stderr
    assert "advisor:" not in r.stderr
    assert not list((tmp_path / "calls").glob("cfg.*advisor.json"))
    r = go(tmp_path, ["-r", "auditor", "--advisor", "sonnet", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_ADVISOR_MAX_CALLS="2"))
    assert r.returncode == 0, r.stderr
    srv = advisor_cfg(tmp_path, 3)
    assert srv["env"]["QA_ADVISOR_MODEL"] == "sonnet" and srv["env"]["QA_ADVISOR_MAX_CALLS"] == "2"


def test_advisor_off_is_accepted(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--advisor", "off", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "advisor:" not in r.stderr


def test_until_done_passes_advisor_to_the_supervisor_not_the_rounds(tmp_path):
    # --until-done --advisor is allowed: the loop gets the advisor as its OWN option and
    # hands --advisor/--advisor-state to each round itself. It must not sit in the
    # passthrough after `--`, where the deviation audit's call is built from it.
    r, argv = _until_done_argv(tmp_path, ["--advisor", "opus"])
    assert r.returncode == 0, r.stdout + r.stderr
    sep = argv.index("--")
    assert argv.count("--advisor") == 1
    assert argv.index("--advisor") < sep and argv[argv.index("--advisor") + 1] == "opus"
    assert not [a for a in argv[sep + 1:] if a.startswith("--advisor")]


def test_advisor_state_requires_advisor(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    d = tmp_path / "shared"; d.mkdir()
    r = go(tmp_path, ["-r", "auditor", "--advisor-state", posix(d), "-C", posix(repo), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 2 and "--advisor-state needs --advisor" in r.stderr
    assert not list((tmp_path / "calls").glob("argv.*"))          # no claude started


def test_advisor_state_dir_is_used_and_kept(tmp_path, server, fake):
    # A round of an --until-done run: the supervisor's directory IS the advisor state,
    # its own budget note says the budget covers the whole run, and cleanup leaves the
    # directory alone -- the next round still has the count, the lock and calls.jsonl.
    repo = dirty_repo(tmp_path)
    d = tmp_path / "shared"; d.mkdir()
    r = go(tmp_path, ["--shallow", "-r", "auditor", "-q", "--advisor", "opus",
                      "--advisor-state", posix(d), "-C", posix(repo), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "at most 4 calls across this --until-done run" in r.stderr       # the startup notice
    (a1, _) = calls(tmp_path)[0]
    assert "ask a stronger model" in sys_prompt(a1)
    assert "at most 4 calls across this --until-done run" in sys_prompt(a1)  # the session note
    [cfg] = list(d.glob("advisor-*.json"))                    # only its config, named per PID
    srv = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["qla_advisor"]
    assert srv["env"]["QA_ADVISOR_MODEL"] == "opus"
    assert same_path(srv["env"]["QA_ADVISOR_STATE"]) == same_path(str(d))
    assert flag(a1, "--mcp-config") and same_path(flag(a1, "--mcp-config")) == same_path(str(cfg))
    assert d.is_dir() and sorted(p.name for p in d.iterdir()) == [cfg.name]


def test_until_done_refuses_a_typed_advisor_state(tmp_path, server, fake):
    # The state directory is the loop's own arrangement: a typed one would aim the
    # rounds at a directory no supervisor made for this run.
    repo = dirty_repo(tmp_path)
    task = tmp_path / "task.md"; task.write_text("do it\n", encoding="utf-8")
    d = tmp_path / "shared"; d.mkdir()
    r = go(tmp_path, ["--until-done", posix(task), "--advisor", "opus", "--advisor-state", posix(d),
                      "-C", posix(repo)], server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 2
    # This refusal's own words, not any of the other --advisor-state complaints: the
    # directory belongs to the supervisor, so the typed one is dropped, never used.
    assert ("--until-done: --advisor-state is passed by the supervisor to its own rounds; "
            "drop --advisor-state") in r.stderr
    assert not list((tmp_path / "calls").glob("argv.*"))


def test_claude_binary_is_resolved_on_path(tmp_path, server, fake):
    # a bare name is resolved in bash, so Windows CreateProcess gets a real path
    bindir = tmp_path / "bin"; bindir.mkdir()
    shutil.copy(str(fake), str(bindir / "myclaude"))
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--advisor", "opus", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_ADVISOR_CLAUDE="myclaude",
                      PATH=posix(bindir) + os.pathsep + os.environ["PATH"]))
    assert r.returncode == 0, r.stderr
    assert same_path(advisor_cfg(tmp_path)["env"]["QA_ADVISOR_CLAUDE"]) == same_path(str(bindir / "myclaude"))


def test_browser_and_advisor_load_together(tmp_path, server, fake):
    r = go(tmp_path, ["--browser", "--advisor", "opus", "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_BROWSER_DIR=posix(tmp_path / "browser")))
    assert r.returncode == 0, r.stderr
    (a1, _) = calls(tmp_path)[0]
    vals = [a1[i + 1] for i, x in enumerate(a1) if x == "--mcp-config"]
    assert len(vals) == 2 and vals[1].endswith("advisor.json")
    assert a1[-2] == "--" and a1[-1] == "hi"
    assert "mcp__qla_advisor__ask" in flag(a1, "--allowed-tools").split(",")


def test_caller_config_naming_qla_advisor_is_refused(tmp_path, server, fake):
    cfg = tmp_path / "mine.json"
    cfg.write_text(json.dumps({"mcpServers": {"qla_advisor": {"command": "x"}}}), encoding="utf-8")
    r = go(tmp_path, ["--mcp-config", posix(cfg), "--advisor", "opus", "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 2 and "qla_advisor" in r.stderr


def test_log_defaults_to_the_cache_not_the_tree(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--advisor", "opus", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_OUTDIR="", XDG_CACHE_HOME=posix(tmp_path / "cache")))
    assert r.returncode == 0, r.stderr
    log = advisor_cfg(tmp_path)["env"]["QA_ADVISOR_LOG"]
    assert same_path(os.path.dirname(log)) == same_path(str(tmp_path / "cache" / "qwen-agent" / "advisor"))


def test_off_by_default_changes_nothing(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    (a1, _), _ = calls(tmp_path)
    assert "--mcp-config" not in a1
    assert "mcp__qla_advisor__ask" not in flag(a1, "--allowed-tools")
    assert "ask a stronger model" not in sys_prompt(a1)


@pytest.mark.parametrize("args,msg", [
    (["--advisor", "op us"], "--advisor: not a model name"),
    (["--advisor", ""], "--advisor needs a model"),
    (["--advisor", "opus", "--advisor-state", "no-such-dir"], "--advisor-state: not a directory"),
    (["--interactive", "--advisor", "opus"], "--advisor"),
    # the internal flag has no rounds to share a state dir with, interactive or not
    (["--interactive", "--advisor-state", "x"], "--advisor-state"),
])
def test_refusals(tmp_path, server, fake, args, msg):
    repo = dirty_repo(tmp_path)
    # --interactive refuses a prompt and -r first, so its case carries neither
    if "--interactive" in args:
        argv = args + ["-C", posix(repo)]
    else:
        argv = ["-r", "auditor"] + args + ["-C", posix(repo), "hi"]
    r = go(tmp_path, argv, server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 2 and msg in r.stderr


def test_json_record_and_state_cleanup(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--json", "--advisor", "opus", "-C", posix(repo), "hi"],
           server, fake, modes="advcall,ok", extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    adv = json.loads(r.stdout)["qwen_agent"]["advisor"]
    assert adv["model"] == "opus" and adv["budget"] == 4
    assert adv["calls"] == 2 and adv["answered"] == 1
    assert adv["seconds"] == 5.5 and adv["cost_usd"] == 0.1
    assert adv["unavailable"] == ["no answer within 600s"]
    assert adv["log"].endswith(".md")
    import os
    assert not os.path.exists(advisor_cfg(tmp_path)["env"]["QA_ADVISOR_STATE"])


def test_ask_note_keeps_out_of_mechanical_work(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--advisor", "opus", "-C", posix(repo), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "mechanical work" in sys_prompt(calls(tmp_path)[0][0])
