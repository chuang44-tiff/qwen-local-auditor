"""qwen-agent: plan / advisor / rescue come from the config FILE (and typed flags), never
from the environment. Wiring only; the servers and hooks have their own test files."""
import json
import shlex

from test_cli import posix
from test_cli_deep import _until_done_argv, calls, dirty_repo, go
import test_cli_default_depth

fake = test_cli_default_depth.fake
server = test_cli_default_depth.server

SHALLOW = {"QWEN_DEPTH": "shallow"}


def cfg(tmp_path, text, crlf=False):
    p = tmp_path / "qa-config"
    p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    return {"QWEN_CONFIG": posix(p)}


def coder(tmp_path, *extra):
    repo = tmp_path / "repo"
    if not repo.exists():
        dirty_repo(tmp_path)
    return ["--shallow", "-r", "coder", "-C", posix(repo), *extra, "hi"]


def advisor_servers(tmp_path):
    return list((tmp_path / "calls").glob("cfg.*advisor.json"))


def hooks(tmp_path):
    return list((tmp_path / "calls").glob("cfg.*settings*.json"))


def hook_cmd(tmp_path):
    [p] = hooks(tmp_path)
    [h] = json.loads(p.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"]
    return h, shlex.split(h["command"])


def test_no_config_no_claude(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake, extra=dict(SHALLOW))
    assert r.returncode == 0, r.stderr
    assert "claude: plan=off advisor=off rescue=off" in r.stderr
    assert not advisor_servers(tmp_path) and not hooks(tmp_path)


def test_env_cannot_enable(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, QWEN_PLAN="opus", QWEN_ADVISOR="opus", QWEN_RESCUE="opus"))
    assert r.returncode == 0, r.stderr
    assert "claude: plan=off advisor=off rescue=off" in r.stderr
    assert not advisor_servers(tmp_path) and not hooks(tmp_path)


def test_config_enables_advisor(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_ADVISOR=opus\n')))
    assert r.returncode == 0, r.stderr
    [p] = advisor_servers(tmp_path)
    assert json.loads(p.read_text())["mcpServers"]["qla_advisor"]["env"]["QA_ADVISOR_MODEL"] == "opus"
    assert "advisor=opus" in r.stderr


def test_config_style_default_ignores_env(tmp_path, server, fake):
    # Config.example style: the file's default must win over an exported value
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, QWEN_PLAN="sonnet", **cfg(tmp_path, 'QWEN_PLAN="${QWEN_PLAN:-opus}"\n')))
    assert r.returncode == 0, r.stderr
    _, parts = hook_cmd(tmp_path)
    assert parts[parts.index("--model") + 1] == "opus"


def test_config_crlf_value(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_ADVISOR=opus\n', crlf=True)))
    assert r.returncode == 0, r.stderr
    assert "advisor=opus" in r.stderr
    assert "not a model name" not in r.stderr


def test_typed_off_beats_config(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path, "--advisor", "off"), server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_ADVISOR=opus\n')))
    assert r.returncode == 0, r.stderr
    assert not advisor_servers(tmp_path)
    assert "advisor=off" in r.stderr


def test_typed_on_beats_config_off(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path, "--plan", "sonnet"), server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_PLAN=off\n')))
    assert r.returncode == 0, r.stderr
    _, parts = hook_cmd(tmp_path)
    assert parts[parts.index("--model") + 1] == "sonnet"


def test_config_rescue_skipped_on_single_run(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--shallow", "-r", "auditor", "-C", posix(repo), "q"], server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_RESCUE=opus\n')))
    assert r.returncode == 0, r.stderr
    assert "rescue=skipped (needs --until-done)" in r.stderr
    assert not advisor_servers(tmp_path) and not hooks(tmp_path)


def test_config_plan_skipped_read_only(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    args = ["--shallow", "-r", "auditor", "-C", posix(repo), "q"]
    r = go(tmp_path, args, server, fake, extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_PLAN=opus\n')))
    assert r.returncode == 0, r.stderr
    assert "plan=skipped" in r.stderr and not hooks(tmp_path)
    r = go(tmp_path, ["--plan", "opus", *args], server, fake, extra=dict(SHALLOW))
    assert r.returncode == 2 and "--plan: nothing to plan" in r.stderr


def test_typed_rescue_needs_until_done(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path, "--rescue", "opus"), server, fake, extra=dict(SHALLOW))
    assert r.returncode == 2 and "--rescue needs --until-done" in r.stderr


def test_advisor_triggers_removed(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path, "--advisor-triggers", "fail"), server, fake, extra=dict(SHALLOW))
    assert r.returncode == 2
    assert "--advisor-triggers was replaced by --rescue MODEL" in r.stderr


def test_max_calls_zero_refused(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, QWEN_ADVISOR_MAX_CALLS="0", **cfg(tmp_path, 'QWEN_ADVISOR=opus\n')))
    assert r.returncode == 2 and "--advisor off" in r.stderr
    assert not list((tmp_path / "calls").glob("argv.*"))


def test_bad_model_in_config(tmp_path, server, fake):
    c = cfg(tmp_path, "QWEN_PLAN='op us'\n")
    r = go(tmp_path, coder(tmp_path), server, fake, extra=dict(SHALLOW, **c))
    assert r.returncode == 2
    assert c["QWEN_CONFIG"] in r.stderr


def test_timeout_zero_means_default(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path, "--advisor", "opus", "--plan", "opus"), server, fake,
           extra=dict(SHALLOW, QWEN_ADVISOR_TIMEOUT="0"))
    assert r.returncode == 0, r.stderr
    [p] = advisor_servers(tmp_path)
    assert json.loads(p.read_text())["mcpServers"]["qla_advisor"]["env"]["QA_ADVISOR_TIMEOUT"] == "600"
    h, parts = hook_cmd(tmp_path)
    assert parts[parts.index("--timeout") + 1] == "600" and h["timeout"] == 660


def test_show_claude_prints_and_exits(tmp_path, server, fake):
    r = go(tmp_path, ["--show-claude"], server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_ADVISOR=opus\n')))
    assert r.returncode == 0, r.stderr
    assert "claude: plan=off advisor=opus rescue=off tools=on" in r.stdout
    assert calls(tmp_path) == []


def test_tools_off_shows_in_the_line(tmp_path, server, fake):
    r = go(tmp_path, ["--show-claude"], server, fake, extra=dict(SHALLOW, QWEN_CLAUDE_TOOLS="0"))
    assert r.returncode == 0, r.stderr
    assert "claude: plan=off advisor=off rescue=off tools=off" in r.stdout


def test_until_done_passes_rescue(tmp_path):
    r, argv = _until_done_argv(tmp_path, [], extra=cfg(tmp_path, 'QWEN_RESCUE=opus\n'))
    assert r.returncode == 0, r.stdout + r.stderr
    sep = argv.index("--")
    assert argv[argv.index("--rescue") + 1] == "opus" and argv.index("--rescue") < sep
    assert "--advisor" not in argv and "--advisor-triggers" not in argv


def ask_tools(tmp_path):
    [p] = advisor_servers(tmp_path)
    return json.loads(p.read_text())["mcpServers"]["qla_advisor"]["env"].get("QA_ADVISOR_TOOLS")


def plan_tools(tmp_path):
    return "--tools" in hook_cmd(tmp_path)[1]


BOTH = 'QWEN_PLAN=opus\nQWEN_ADVISOR=opus\n'


def test_tools_default_on_all_calls(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake, extra=dict(SHALLOW, **cfg(tmp_path, BOTH)))
    assert r.returncode == 0, r.stderr
    assert plan_tools(tmp_path) and ask_tools(tmp_path) == "1"
    assert "tools=on" in r.stderr


def test_claude_tools_zero_off_from_env(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, QWEN_CLAUDE_TOOLS="0", **cfg(tmp_path, BOTH)))
    assert r.returncode == 0, r.stderr
    assert not plan_tools(tmp_path) and not ask_tools(tmp_path)
    assert "tools=off" in r.stderr


def test_claude_tools_zero_off_from_config(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, BOTH + 'QWEN_CLAUDE_TOOLS=0\n')))
    assert r.returncode == 0, r.stderr
    assert not plan_tools(tmp_path) and not ask_tools(tmp_path)
    assert "tools=off" in r.stderr


def test_bench_scope_list_still_works(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, QWEN_ADVISOR_TOOLS="plan", **cfg(tmp_path, BOTH)))
    assert r.returncode == 0, r.stderr
    assert plan_tools(tmp_path) and not ask_tools(tmp_path)


def test_scope_ask_only(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, QWEN_ADVISOR_TOOLS="ask", **cfg(tmp_path, BOTH)))
    assert r.returncode == 0, r.stderr
    assert not plan_tools(tmp_path) and ask_tools(tmp_path) == "1"


def test_notice_says_tools(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake, extra=dict(SHALLOW, **cfg(tmp_path, BOTH)))
    assert r.stderr.count("claude: ") == 1
    assert [l for l in r.stderr.splitlines() if l.startswith("claude: ")][0].endswith("tools=on")
    r = go(tmp_path, ["--show-claude", "-r", "coder", "-C", posix(tmp_path / "repo")], server, fake,
           extra=dict(SHALLOW, QWEN_ADVISOR_TOOLS="rescue", **cfg(tmp_path, BOTH)))
    assert r.stdout.strip().endswith("tools=off")        # no plan/ask tools under rescue only
    r = go(tmp_path, ["--show-claude", "-r", "coder", "-C", posix(tmp_path / "repo")], server, fake,
           extra=dict(SHALLOW, QWEN_ADVISOR_TOOLS="plan", **cfg(tmp_path, BOTH)))
    assert r.stdout.strip().endswith("tools=on")


def test_timeout_non_numeric_refused(tmp_path, server, fake):
    for args, extra in ((coder(tmp_path), {}), (["--show-claude", "-r", "coder", "-C", posix(tmp_path / "repo")], {}),
                        (coder(tmp_path, "--until-done", str(tmp_path / "t.md")), {})):
        r = go(tmp_path, args, server, fake, extra=dict(SHALLOW, QWEN_ADVISOR_TIMEOUT="abc", **extra))
        assert r.returncode == 2 and "QWEN_ADVISOR_TIMEOUT" in r.stderr, (args, r.stderr)


def test_timeout_all_zero_means_default(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path, "--advisor", "opus"), server, fake,
           extra=dict(SHALLOW, QWEN_ADVISOR_TIMEOUT="0000"))
    assert r.returncode == 0, r.stderr
    assert json.loads(advisor_servers(tmp_path)[0].read_text())["mcpServers"]["qla_advisor"]["env"]["QA_ADVISOR_TIMEOUT"] == "600"


# ------------------------------------------------ -q and the claude: line

def test_quiet_hides_line_when_all_off(tmp_path, server, fake):
    r = go(tmp_path, ["-q"] + coder(tmp_path), server, fake, extra=dict(SHALLOW))
    assert r.returncode == 0, r.stderr
    assert "claude:" not in r.stderr


def test_quiet_keeps_line_when_on_or_skipped(tmp_path, server, fake):
    r = go(tmp_path, ["-q"] + coder(tmp_path), server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_ADVISOR=opus\n')))
    assert r.returncode == 0, r.stderr
    assert "claude: plan=off advisor=opus" in r.stderr
    r = go(tmp_path, ["-q", "-r", "auditor", "hi"], server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, 'QWEN_PLAN=opus\n')))
    assert r.returncode == 0, r.stderr
    assert "plan=skipped" in r.stderr


# ------------------------------------------------ one-time "what changed" notice

def notice_env(tmp_path, **more):
    state = tmp_path / "state"
    return dict(dict(SHALLOW, QWEN_NO_NOTICE="", XDG_STATE_HOME=posix(state)), **more)


def marker(tmp_path):
    return tmp_path / "state" / "qwen-agent" / "notice-claude-settings-1"


def test_whats_new_once(tmp_path, server, fake):
    env = notice_env(tmp_path)
    r = go(tmp_path, coder(tmp_path), server, fake, extra=env)
    assert r.returncode == 0, r.stderr
    assert "qwen-agent: Claude settings changed" in r.stderr
    assert any(ln.startswith("qwen-agent: Claude settings changed") for ln in r.stderr.splitlines())
    assert "qwen-agent: Claude settings changed" not in r.stdout
    assert marker(tmp_path).exists()
    r = go(tmp_path, coder(tmp_path), server, fake, extra=env)
    assert r.returncode == 0, r.stderr
    assert "Claude settings changed" not in r.stderr


def test_whats_new_lists_restore_lines(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=notice_env(tmp_path, **cfg(tmp_path, "")))
    for line in ("# QWEN_PLAN=opus", "# QWEN_ADVISOR=opus", "# QWEN_RESCUE=opus",
                 "# QWEN_CONFIRM=opus", "QWEN_CLAUDE_TOOLS=0", "--rescue"):
        assert line in r.stderr, line
    assert str(tmp_path / "qa-config") in r.stderr or posix(tmp_path / "qa-config") in r.stderr


def test_whats_new_not_for_help_version(tmp_path, server, fake):
    env = notice_env(tmp_path)
    for flagname in ("--help", "--version"):
        r = go(tmp_path, [flagname], server, fake, extra=env)
        assert r.returncode == 0
        assert "Claude settings changed" not in r.stderr
    assert not marker(tmp_path).exists()


def test_whats_new_on_show_claude_stderr_only(tmp_path, server, fake):
    r = go(tmp_path, ["--show-claude"], server, fake, extra=notice_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Claude settings changed" in r.stderr
    assert "Claude settings changed" not in r.stdout
    assert r.stdout.startswith("claude: plan=off")


def test_whats_new_unwritable_state_dir_does_not_fail(tmp_path, server, fake):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    env = notice_env(tmp_path, XDG_STATE_HOME=posix(blocker))
    r = go(tmp_path, coder(tmp_path), server, fake, extra=env)
    assert r.returncode == 0, r.stderr


def test_whats_new_mentions_rescue_split(tmp_path, server, fake):
    r = go(tmp_path, coder(tmp_path), server, fake, extra=notice_env(tmp_path))
    assert "--advisor no longer includes the call after a failing --until-done round" in r.stderr
    assert "add --rescue MODEL (or QWEN_RESCUE in the config)" in r.stderr


def test_whats_new_config_path_with_ampersand(tmp_path, server, fake):
    d = tmp_path / "a&b"
    d.mkdir()
    p = d / "config"
    p.write_text("", encoding="utf-8")
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=notice_env(tmp_path, QWEN_CONFIG=posix(p)))
    assert r.returncode == 0, r.stderr
    assert posix(p) in r.stderr                    # not mangled by & in the replacement


def test_preflight_only_prints_no_claude_line(tmp_path, server, fake):
    r = go(tmp_path, ["--preflight-only"], server, fake,
           extra=dict(SHALLOW, **cfg(tmp_path, "QWEN_RESCUE=opus\n")))
    assert r.returncode == 0, r.stderr
    assert "claude:" not in r.stderr


def test_supervised_round_ignores_config_and_prints_no_claude_line(tmp_path, server, fake):
    # QA_SUPERVISED=1 is what --until-done sets for every round it launches (the deviation
    # audit included): the supervisor owns the calls, the round never reads the config
    r = go(tmp_path, coder(tmp_path), server, fake,
           extra=dict(SHALLOW, QA_SUPERVISED="1", **cfg(tmp_path, "QWEN_ADVISOR=opus\nQWEN_PLAN=opus\n")))
    assert r.returncode == 0, r.stderr
    assert "claude:" not in r.stderr
    assert not advisor_servers(tmp_path) and not hooks(tmp_path)
