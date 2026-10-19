"""qwen-agent --plan MODEL: the wiring. The hook itself is test_advisor_hook's, the loop's
plan dir and report are test_supervisor_plan's. The fake claude copies every .json argument
it is given (the --settings file) and, in mode `plancall`, writes the record a hook that
fired would have written."""
import json
import shlex

import pytest

from test_cli import flag, posix, same_path
from test_cli_deep import _until_done_argv, calls, dirty_repo, go, sys_prompt
import test_cli_default_depth

server = test_cli_default_depth.server        # the shared model server, by assignment

SHALLOW = {"QWEN_DEPTH": "shallow"}
TEST_ENV = {"QWEN_TEST_CMD": "true"}

FAKE_PLAN = r'''#!/usr/bin/env bash
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
d="$FAKE_DIR"
n=$(cat "$d/n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$d/n"
printf '%s\0' "$@" > "$d/argv.$n"
for _a in "$@"; do case "$_a" in *.json) [ -f "$_a" ] && cp "$_a" "$d/cfg.$n.$(basename "$_a")" ;; esac; done
# plancall -- act like a session whose hook fired: write the record the hook would
if [ "${FAKE_MODES:-}" = plancall ]; then
  for _a in "$@"; do case "$_a" in *settings*.json)
    python3 - "$_a" <<'PY'
import json, shlex, sys
cmd = json.load(open(sys.argv[1]))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
rec = shlex.split(cmd)
with open(rec[rec.index("--record") + 1], "a") as fh:
    fh.write(json.dumps({"trigger": "plan", "round": 1, "called": True, "skipped_reason": None,
                         "cost_usd": 0.1, "tool": "Edit", "path": "a.txt", "digest_chars": 321,
                         "cut": False, "advice": "check the loader"}) + "\n")
PY
  ;; esac; done
fi
pwd -P > "$d/pwd.$n"
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"answer %s","session_id":"sess-%s","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}\n' "$n" "$n"
'''


@pytest.fixture
def fake(tmp_path):
    p = tmp_path / "fake-plan"
    p.write_text(FAKE_PLAN, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    (tmp_path / "calls").mkdir()
    return p


def settings_of(tmp_path, n=1):
    [p] = list((tmp_path / "calls").glob("cfg.%d.*settings*.json" % n))
    return json.loads(p.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]


def plan_args(tmp_path, *extra):
    repo = tmp_path / "repo"
    if not repo.exists():
        dirty_repo(tmp_path)
    return ["--shallow", "-r", "coder", "-q", "-C", posix(repo), *extra, "hi"]


def test_plan_wiring_holds_the_first_write_with_a_hook(tmp_path, server, fake):
    args = plan_args(tmp_path, "--plan", "opus", "--test")
    r = go(tmp_path, args, server, fake, extra=dict(SHALLOW, **TEST_ENV,
           QWEN_TEST_WORKTREES=posix(tmp_path / "wts")))
    assert r.returncode == 0, r.stderr
    assert "plan: opus via your claude login" in r.stderr              # even with -q
    assert "leaves this machine" in r.stderr
    [(a1, _)] = calls(tmp_path)
    cfg = flag(a1, "--settings")
    assert cfg and cfg.endswith("settings.json")
    hook = settings_of(tmp_path)
    assert hook["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    [h] = hook["hooks"]
    assert h["type"] == "command" and h["timeout"] == 660
    parts = shlex.split(h["command"])
    assert parts[1].endswith("advisor_hook.py")
    assert parts[parts.index("--model") + 1] == "opus"
    assert parts[parts.index("--timeout") + 1] == "600"
    state = parts[parts.index("--state") + 1]
    assert same_path(parts[parts.index("--task") + 1]) == same_path(state) + "/task.md"
    assert same_path(parts[parts.index("--record") + 1]) == same_path(state) + "/plan.jsonl"
    assert same_path(cfg) == same_path(state) + "/settings.json"
    sp = sys_prompt(a1)
    assert "first Edit or Write is held once" in sp and "ADVICE RESPONSE block" in sp
    assert "never sees your thinking" in sp and "1. GOAL" in sp and "6. UNSURE" in sp
    assert not same_path(state).startswith(same_path(tmp_path / "repo"))   # outside the repo
    import os
    assert not os.path.exists(state)                                      # removed with the run


def test_plan_hook_timeout_follows_the_advisor_timeout(tmp_path, server, fake):
    r = go(tmp_path, plan_args(tmp_path, "--plan", "opus"), server, fake,
           extra=dict(SHALLOW, QWEN_ADVISOR_TIMEOUT="660"))
    assert r.returncode == 0, r.stderr
    [h] = settings_of(tmp_path)["hooks"]
    assert h["timeout"] == 720
    assert shlex.split(h["command"])[shlex.split(h["command"]).index("--timeout") + 1] == "660"


def test_plan_off_and_no_flag_change_nothing(tmp_path, server, fake):
    r = go(tmp_path, plan_args(tmp_path, "--plan", "off"), server, fake, extra=dict(SHALLOW))
    assert r.returncode == 0, r.stderr
    r2 = go(tmp_path, plan_args(tmp_path), server, fake, extra=dict(SHALLOW, QWEN_PLAN="opus"))
    assert r2.returncode == 0, r2.stderr
    for argv, _ in calls(tmp_path):
        assert "--settings" not in argv
        assert "first Edit or Write is held once" not in sys_prompt(argv)
    assert "plan:" not in r.stderr and "plan:" not in r2.stderr


@pytest.mark.parametrize("args,needle", [
    (["--plan", "opus", "--resume", "sess-1", "-r", "coder"], "--plan cannot be combined with --resume"),
    (["--plan", "opus", "-r", "auditor"], "nothing to plan: this run cannot write"),
    (["--plan", "opus"], "nothing to plan: this run cannot write"),
    (["--plan", "opus", "--toolset", "Read,Grep", "-r", "coder"], "nothing to plan: this run cannot write"),
    (["--plan", "not a model!", "-r", "coder"], "--plan: not a model name"),
    (["--plan-state", "somewhere", "-r", "coder"], "--plan-state needs --plan"),
])
def test_plan_refusals(tmp_path, server, fake, args, needle):
    r = go(tmp_path, [*args, "hi"], server, fake, extra=dict(SHALLOW))
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert not list((tmp_path / "calls").glob("argv.*"))                  # claude never started


def test_plan_refuses_interactive(tmp_path, server, fake):
    r = go(tmp_path, ["--interactive", "--plan", "opus"], server, fake, extra=dict(SHALLOW))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--interactive cannot be combined with --plan" in r.stderr
    assert not list((tmp_path / "calls").glob("argv.*"))


def test_plan_dry_run_checks_the_run_but_creates_nothing(tmp_path, server, fake):
    ok = go(tmp_path, ["--dry-run", "--shallow", "--plan", "opus", "-r", "coder", "hi"], server, fake,
            extra=dict(SHALLOW))
    assert ok.returncode == 0, ok.stderr
    assert "--settings" not in ok.stdout and "first Edit or Write is held once" not in ok.stdout
    bad = go(tmp_path, ["--dry-run", "--shallow", "--plan", "opus", "-r", "auditor", "hi"], server, fake,
             extra=dict(SHALLOW))
    assert bad.returncode == 2 and "nothing to plan" in bad.stderr


def test_plan_state_must_be_a_directory(tmp_path, server, fake):
    r = go(tmp_path, ["--plan", "opus", "--plan-state", posix(tmp_path / "nope"), "-r", "coder", "hi"],
           server, fake, extra=dict(SHALLOW))
    assert r.returncode == 2 and "--plan-state: not a directory" in r.stderr


def test_plan_alone_has_no_advisor_server(tmp_path, server, fake):
    r = go(tmp_path, plan_args(tmp_path, "--plan", "opus"), server, fake, extra=dict(SHALLOW))
    assert r.returncode == 0, r.stderr
    [(a1, _)] = calls(tmp_path)
    assert "--mcp-config" not in a1
    assert "mcp__qla_advisor__ask" not in (flag(a1, "--allowed-tools") or "")
    assert "ask a stronger model" not in sys_prompt(a1)
    assert "advisor:" not in r.stderr and "plan: opus" in r.stderr
    assert flag(a1, "--settings")


def test_plan_and_advisor_together_keep_separate_state(tmp_path, server, fake):
    r = go(tmp_path, plan_args(tmp_path, "--plan", "opus", "--advisor", "sonnet"), server, fake,
           extra=dict(SHALLOW))
    assert r.returncode == 0, r.stderr
    [(a1, _)] = calls(tmp_path)
    [mcp] = list((tmp_path / "calls").glob("cfg.1.*advisor.json"))
    adv_state = json.loads(mcp.read_text())["mcpServers"]["qla_advisor"]["env"]["QA_ADVISOR_STATE"]
    h = settings_of(tmp_path)["hooks"][0]
    parts = shlex.split(h["command"])
    plan_state = parts[parts.index("--state") + 1]
    assert same_path(adv_state) != same_path(plan_state)
    assert "ask a stronger model" in sys_prompt(a1) and "first Edit or Write is held once" in sys_prompt(a1)
    assert parts[parts.index("--model") + 1] == "opus"
    assert json.loads(mcp.read_text())["mcpServers"]["qla_advisor"]["env"]["QA_ADVISOR_MODEL"] == "sonnet"


def test_plan_state_dir_is_used_and_kept(tmp_path, server, fake):
    # A round of an --until-done run: the supervisor's directory is the plan state, this
    # session adds only its own settings file and cleanup leaves the directory alone.
    d = tmp_path / "shared"; d.mkdir()
    (d / "task.md").write_text("THE WHOLE TASK\n", encoding="utf-8")
    r = go(tmp_path, plan_args(tmp_path, "--plan", "opus", "--plan-state", posix(d)), server, fake,
           extra=dict(SHALLOW))
    assert r.returncode == 0, r.stderr
    [cfg] = list(d.glob("settings-*.json"))
    h = json.loads(cfg.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]
    parts = shlex.split(h["command"])
    assert same_path(parts[parts.index("--state") + 1]) == same_path(str(d))
    assert (d / "task.md").read_text(encoding="utf-8") == "THE WHOLE TASK\n"       # not overwritten
    assert sorted(p.name for p in d.iterdir()) == sorted([cfg.name, "task.md"])


def test_single_shot_json_record_carries_the_plan_call(tmp_path, server, fake):
    r = go(tmp_path, plan_args(tmp_path, "--plan", "opus", "--json"), server, fake,
           modes="plancall", extra=dict(SHALLOW))
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)["qwen_agent"]["plan"]
    assert (plan["model"], plan["called"], plan["round"], plan["tool"], plan["path"]) == (
        "opus", True, 1, "Edit", "a.txt")
    assert plan["digest_chars"] == 321 and plan["cut"] is False and plan["cost_usd"] == 0.1
    assert plan["skipped_reason"] is None and plan["log"].endswith(".md") and "plan-" in plan["log"]


def test_single_shot_json_record_says_when_no_write_happened(tmp_path, server, fake):
    r = go(tmp_path, plan_args(tmp_path, "--plan", "opus", "--json"), server, fake, extra=dict(SHALLOW))
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)["qwen_agent"]["plan"]
    assert plan["called"] is False and plan["skipped_reason"] == "no write"


def test_until_done_passes_plan_to_the_supervisor_not_the_rounds(tmp_path):
    r, argv = _until_done_argv(tmp_path, ["--plan", "opus"])
    assert r.returncode == 0, r.stdout + r.stderr
    sep = argv.index("--")
    assert argv.count("--plan") == 1
    assert argv.index("--plan") < sep and argv[argv.index("--plan") + 1] == "opus"
    assert not [a for a in argv[sep + 1:] if a.startswith("--plan")]


def test_until_done_plan_off_is_not_forwarded_either(tmp_path):
    r, argv = _until_done_argv(tmp_path, ["--plan", "off"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--plan" not in argv and "off" not in argv[argv.index("--"):]


def test_until_done_refuses_a_typed_plan_state(tmp_path, server, fake):
    d = tmp_path / "shared"; d.mkdir()
    r = go(tmp_path, ["--until-done", "t.md", "--plan", "opus", "--plan-state", posix(d)], server, fake)
    assert r.returncode == 2
    assert ("--until-done: --plan-state is passed by the supervisor to its own rounds; "
            "drop --plan-state") in r.stderr
    assert not list((tmp_path / "calls").glob("argv.*"))
