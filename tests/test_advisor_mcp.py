"""lib/advisor_mcp.py: the advisor tool. A fake `claude` records what it was given; no
network, no real model. The fake is a POSIX script, so the exec tests skip on Windows
(the module itself is plain stdlib)."""
import io
import json
import os
import subprocess
import sys

import pytest

LIB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skill", "local-auditor"))
sys.path.insert(0, LIB)
from lib import advisor_mcp as am  # noqa: E402

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake claude is a POSIX script")

# One ask() from a process of its own, for the budget's process-level half.
RUN_ASK = r'''import os, sys
sys.path.insert(0, os.environ["QA_LIB"])
from lib import advisor_mcp as am
try:
    print("OK " + am.ask(sys.argv[1], "", None, os.environ, os.getcwd()))
    sys.exit(0)
except Exception as e:
    print("%s: %s" % (type(e).__name__, e))
    sys.exit(3)
'''

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
d = os.environ["FAKE_ADV_DIR"]
n = len([f for f in os.listdir(d) if f.startswith("argv.")]) + 1
json.dump(sys.argv[1:], open(os.path.join(d, "argv.%d" % n), "w"))
json.dump(dict(os.environ), open(os.path.join(d, "env.%d" % n), "w"))
open(os.path.join(d, "stdin.%d" % n), "w").write(sys.stdin.read())
open(os.path.join(d, "cwd.%d" % n), "w").write(os.getcwd() + "\n" + "\n".join(os.listdir(".")))
mode = os.environ.get("FAKE_ADV_MODE", "ok")
if mode == "slow":
    time.sleep(5)
if mode == "junk":
    print("not json"); sys.exit(1)
if mode == "err":
    print(json.dumps({"type": "result", "is_error": True, "result": "usage limit reached"})); sys.exit(1)
print(json.dumps({"type": "result", "is_error": False, "result": "Take option A: the probe shows it.",
                  "total_cost_usd": 0.12, "modelUsage": {"claude-opus-test": {"outputTokens": 9}}}))
'''


@pytest.fixture
def adv(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(FAKE, encoding="utf-8")
    fake.chmod(0o755)
    calls = tmp_path / "calls"; calls.mkdir()
    state = tmp_path / "state"; state.mkdir()
    root = tmp_path / "root"; root.mkdir()
    (root / "a.py").write_text("print('a')\n", encoding="utf-8")
    env = dict(os.environ)
    env.update({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8001", "ANTHROPIC_MODEL": "qwen",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "qwen", "ANTHROPIC_AUTH_TOKEN": "dummy",
                "CLAUDE_CODE_SUBAGENT_MODEL": "qwen", "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
                "CLAUDE_EFFORT": "xhigh", "AWS_BEARER_TOKEN_BEDROCK": "x",
                "QA_ADVISOR_MODEL": "opus", "QA_ADVISOR_STATE": str(state),
                "QA_ADVISOR_MAX_CALLS": "4", "QA_ADVISOR_TIMEOUT": "30",
                "QA_ADVISOR_LOG": str(tmp_path / "advisor.md"),
                "QA_ADVISOR_CLAUDE": str(fake), "FAKE_ADV_DIR": str(calls)})
    return {"env": env, "calls": calls, "state": state, "root": root, "tmp": tmp_path}


def records(state):
    p = state / "calls.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


@posix_only
def test_ok_call_returns_advice_and_logs(adv):
    out = am.ask("Keep claim F2?", "probe output: X", ["a.py"], adv["env"], str(adv["root"]))
    assert out.startswith("ADVICE (claude-opus-test, call 1 of 4):")
    assert "Take option A" in out
    argv = json.loads((adv["calls"] / "argv.1").read_text())
    assert argv == ["-p", "--model", "opus", "--tools", "", "--strict-mcp-config",
                    "--setting-sources", "", "--no-session-persistence",
                    "--output-format", "json", "--max-turns", "1"]
    assert "--effort" not in argv
    brief = (adv["calls"] / "stdin.1").read_text()
    assert "QUESTION: Keep claim F2?" in brief and "CONTEXT: probe output: X" in brief
    assert "FILE a.py" in brief and "print('a')" in brief
    cwd_lines = (adv["calls"] / "cwd.1").read_text().splitlines()
    assert cwd_lines[0] != str(adv["root"]) and cwd_lines[1:] == []      # an empty temp dir
    log = (adv["tmp"] / "advisor.md").read_text()
    assert "Keep claim F2?" in log and "Take option A" in log
    [rec] = records(adv["state"])
    assert (rec["n"], rec["model"], rec["cost_usd"], rec["unavailable"]) == (1, "claude-opus-test", 0.12, None)
    assert isinstance(rec["seconds"], float)


@posix_only
def test_child_env_has_no_qwen_redirect(adv):
    am.ask("q", "", None, adv["env"], str(adv["root"]))
    env = json.loads((adv["calls"] / "env.1").read_text())
    leaked = [k for k in env if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
              or k in ("CLAUDE_EFFORT", "AWS_BEARER_TOKEN_BEDROCK")]
    assert leaked == []
    assert env["HOME"] == os.environ["HOME"]          # the login lives under HOME


@posix_only
def test_budget_is_shared_across_servers(adv):
    adv["env"]["QA_ADVISOR_MAX_CALLS"] = "2"
    am.ask("q1", "", None, adv["env"], str(adv["root"]))
    # a second server process of the same run: same state dir, fresh module state
    out = am.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": "ask", "arguments": {"question": "q2"}}},
                    env=adv["env"], root=str(adv["root"]))
    assert "call 2 of 2" in out["result"]["content"][0]["text"]
    with pytest.raises(am.Unavailable, match="budget of 2 advisor calls"):
        am.ask("q3", "", None, adv["env"], str(adv["root"]))
    assert len(list(adv["calls"].glob("argv.*"))) == 2        # the third never ran claude


def ask_in_process(adv, question):
    """One ask() run by a separate python process against adv's state dir: the only
    thing the two runs can share is that directory, which is the point."""
    runner = adv["tmp"] / "run-ask.py"
    runner.write_text(RUN_ASK, encoding="utf-8")
    p = subprocess.run([sys.executable, str(runner), question], env=dict(adv["env"], QA_LIB=LIB),
                       cwd=str(adv["root"]), capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr).strip()


@posix_only
def test_budget_is_shared_across_processes(adv):
    # The budget of a run is not per server process: --until-done gives every coder round
    # its own advisor server, all pointed at ONE state dir, so the cap has to survive the
    # process that made the first call.
    adv["env"]["QA_ADVISOR_MAX_CALLS"] = "1"
    rc, out = ask_in_process(adv, "q1")
    assert rc == 0 and out.startswith("OK ADVICE (") and "call 1 of 1" in out
    rc, out = ask_in_process(adv, "q2")
    assert rc == 3
    assert "Unavailable: the budget of 1 advisor calls for this run is spent" in out
    assert (adv["state"] / "count").read_text() == "1"       # the second call spent nothing
    assert len(records(adv["state"])) == 1                   # and never reached claude
    assert not list(adv["calls"].glob("argv.2"))


@posix_only
def test_refusals_spend_nothing(adv):
    with pytest.raises(am.Refused, match="question"):
        am.ask("  ", "", None, adv["env"], str(adv["root"]))
    with pytest.raises(am.Refused, match="12000"):
        am.ask("q", "x" * 12001, None, adv["env"], str(adv["root"]))
    with pytest.raises(am.Refused, match="at most 5"):
        am.ask("q", "", ["a.py"] * 6, adv["env"], str(adv["root"]))
    assert not (adv["state"] / "count").exists() or (adv["state"] / "count").read_text().strip() == "0"


@posix_only
def test_paths_outside_root_are_not_attached(adv):
    outside = adv["tmp"] / "secret.txt"; outside.write_text("SECRET", encoding="utf-8")
    os.symlink(str(outside), str(adv["root"] / "link.txt"))
    out = am.ask("q", "", ["../secret.txt", str(outside), "link.txt", "nope.py", "a.py"],
                 adv["env"], str(adv["root"]))
    brief = (adv["calls"] / "stdin.1").read_text()
    assert "SECRET" not in brief and "print('a')" in brief
    for p in ("../secret.txt", str(outside), "link.txt"):
        assert "%s: outside the working directory, not attached" % p in out
    assert "nope.py: not a file, not attached" in out


@posix_only
@pytest.mark.parametrize("mode,why", [("err", "claude reported an error: usage limit reached"),
                                      ("junk", "no readable answer"),
                                      ("slow", "no answer within 1s")])
def test_failures_are_unavailable_results(adv, mode, why):
    adv["env"]["FAKE_ADV_MODE"] = mode
    adv["env"]["QA_ADVISOR_TIMEOUT"] = "1"
    reply = am.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "ask", "arguments": {"question": "q"}}},
                      env=adv["env"], root=str(adv["root"]))
    text = reply["result"]["content"][0]["text"]
    assert text.startswith("ADVISOR UNAVAILABLE: ") and why in text
    assert text.endswith("Decide on the evidence you have.")
    assert reply["result"]["isError"] is False
    assert records(adv["state"])[-1]["unavailable"] is not None


def test_no_claude_and_no_state_are_unavailable(adv, tmp_path):
    env = dict(adv["env"], QA_ADVISOR_CLAUDE=str(tmp_path / "missing-claude"))
    with pytest.raises(am.Unavailable, match="could not start claude"):
        am.ask("q", "", None, env, str(adv["root"]))
    env = dict(adv["env"]); env.pop("QA_ADVISOR_STATE")
    with pytest.raises(am.Unavailable, match="state directory"):
        am.ask("q", "", None, env, str(adv["root"]))


def test_mcp_protocol(adv):
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}}]
    out = io.StringIO()
    am.serve(io.StringIO("\n".join(json.dumps(l) for l in lines) + "\nnot json\n"), out,
             env=adv["env"], root=str(adv["root"]))
    replies = [json.loads(l) for l in out.getvalue().splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == "qla-advisor"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["ask"]
    assert replies[2]["error"]["code"] == -32602
    assert replies[3]["error"]["code"] == -32700
    assert len(replies) == 4                      # the notification got no reply


@posix_only
def test_bare_claude_name_is_found_on_path(adv):
    env = dict(adv["env"], QA_ADVISOR_CLAUDE="claude",
               PATH=str(adv["tmp"]) + os.pathsep + adv["env"].get("PATH", ""))
    assert am.ask("q", "", None, env, str(adv["root"])).startswith("ADVICE (")


@posix_only
def test_unreadable_and_odd_paths_are_notes(adv):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    f = adv["root"] / "locked.py"; f.write_text("x", encoding="utf-8"); f.chmod(0)
    try:
        out = am.ask("q", "", ["locked.py", "bad\x00name", "a.py"], adv["env"], str(adv["root"]))
    finally:
        f.chmod(0o644)
    assert "locked.py: unreadable, not attached" in out
    assert "not attached" in out.split("bad")[1]
    assert "print('a')" in (adv["calls"] / "stdin.1").read_text()
