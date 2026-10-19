"""lib/advisor_hook.py: the digest of a session's work and the PreToolUse hook of --plan.
The digest is pure (a fixture transcript in, text out). The hook tests run the script as
Claude Code would, with the hook JSON on stdin and a fake `claude` that records what it was
given; no network, no real model. The fake is a POSIX script, so the tests that reach it
skip on Windows."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

LIB =os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skill", "local-auditor"))
sys.path.insert(0, LIB)
from lib import advisor_hook as ah  # noqa: E402

HOOK = os.path.join(LIB, "lib", "advisor_hook.py")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake claude is a POSIX script")

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
d = os.environ["FAKE_PLAN_DIR"]
n = len([f for f in os.listdir(d) if f.startswith("stdin.")]) + 1
open(os.path.join(d, "stdin.%d" % n), "w").write(sys.stdin.read())
mode = os.environ.get("FAKE_PLAN_MODE", "ok")
if mode == "slow":
    time.sleep(8)
if mode == "err":
    print(json.dumps({"type": "result", "is_error": True, "result": "usage limit reached"})); sys.exit(1)
if mode == "flagged":
    print(json.dumps({"type": "result", "is_error": True, "result": "API Error: Opus's safeguards flagged this message (https://www.anthropic.com/legal/aup)."})); sys.exit(1)
print(json.dumps({"type": "result", "is_error": False,
                  "result": "1. APPROACH: read value.txt first.\n2. CHECK NEXT: grep for the loader.",
                  "total_cost_usd": 0.11, "modelUsage": {"claude-opus-test": {}}}))
'''

CWD = "/repo"
EDIT_NEW = "EDIT_CONTENT_MARKER = 1"
BODY = "RESULT_BODY_MARKER"


# ---------------------------------------------------------------- transcript fixtures
# Shapes as Claude Code writes them (checked against real transcripts): one block per row,
# an assistant row per thinking / text / tool_use block, a user row per tool_result.

def asst(block):
    return {"type": "assistant", "isSidechain": False,
            "message": {"role": "assistant", "content": [block]}}


def thought(text):
    return asst({"type": "thinking", "thinking": text, "signature": "sig"})


def said(text):
    return asst({"type": "text", "text": text})


def use(tid, name, **inp):
    return asst({"type": "tool_use", "id": tid, "name": name, "input": inp})


def result(tid, content, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [block]}}


def prompt(text):
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text}}


HELD = {"tool_name": "Edit", "tool_use_id": "held1",
        "tool_input": {"file_path": "/repo/src/x.py", "old_string": "a", "new_string": EDIT_NEW}}


def session_rows():
    """3 thoughts, 2 messages, Read / Glob / Grep / qwen-test FAIL and PASS / a denied Bash
    / a Read of .env, then the held Edit."""
    return [
        prompt("Fix the loader."),
        thought("THINK_ONE: the loader reads value.txt"),
        said("I will read the code first."),
        use("t1", "Read", file_path="/repo/src/x.py"),
        result("t1", "\n".join("%d\t%s line" % (i, BODY) for i in range(1, 41))),
        use("t2", "Glob", pattern="**/*.py"),
        result("t2", "src/x.py\nsrc/y.py\ntests/test_x.py"),
        use("t3", "Grep", pattern="foo", path="/repo/src", output_mode="content"),
        result("t3", "src/x.py:3:foo = %s\nsrc/x.py:9:foo()\nsrc/y.py:1:import foo" % BODY),
        thought("THINK_TWO: the dotenv file may hold the setting"),
        use("t4", "Read", file_path="/repo/.env"),
        result("t4", "API_KEY=abc123-ENV_SECRET"),
        use("t5", "Bash", command="qwen-test run.sh"),
        result("t5", "Exit code 1\nFAILED run.sh::test_a - assert 1 == 2\n%s" % BODY, is_error=True),
        use("t6", "Bash", command="qwen-test"),
        result("t6", "3 passed in 0.1s"),
        use("t7", "Bash", command="ls -la"),
        result("t7", "Permission to use Bash with command ls -la has been denied.", is_error=True),
        thought("THINK_THREE: change the default"),
        said("PLAN: change x.py."),
        use("held1", "Edit", file_path="/repo/src/x.py", old_string="a", new_string=EDIT_NEW),
    ]


# ---------------------------------------------------------------- the digest

def test_digest_lines_in_order_with_outcomes_and_no_contents():
    d = ah.build_digest(session_rows(), CWD, HELD)
    lines = d.splitlines()
    assert lines[0] == "[prompt: the task]"
    assert lines[1] == "SAID: I will read the code first."
    assert lines[2] == "Read src/x.py -> 40 lines"
    assert lines[3] == 'Glob "**/*.py" -> 3 files'
    assert lines[4] == 'Grep "foo" in src -> 3 hits'
    assert 'Read <secret-looking path withheld>' in lines
    assert "qwen-test run.sh -> FAIL: FAILED run.sh::test_a - assert 1 == 2" in lines
    assert "qwen-test -> PASS" in lines
    assert 'Bash "ls -la" -> DENIED' in lines
    assert "THINK_" not in d and "THOUGHT" not in d          # thinking is never sent
    assert lines[-2] == "SAID: PLAN: change x.py."
    assert lines[-1] == "about to Edit src/x.py"
    for secret in (BODY, "ENV_SECRET", "abc123", EDIT_NEW):
        assert secret not in d
    assert ".env" not in d


def test_digest_withholds_a_secret_held_path():
    held = dict(HELD, tool_name="Write", tool_input={"file_path": "/repo/.env", "content": "K=v"})
    d = ah.build_digest(session_rows()[:-1], CWD, held)
    assert d.splitlines()[-1] == "about to Write <secret-looking path withheld>"


def test_digest_withholds_a_bash_command_that_names_a_secret():
    rows = [use("b1", "Bash", command="cat .env"), result("b1", "API_KEY=ENV_SECRET"),
            use("held1", "Edit", file_path="/repo/a.py")]
    d = ah.build_digest(rows, CWD, HELD)
    assert "ENV_SECRET" not in d and "cat .env" not in d
    assert "Bash <secret-looking path withheld>" in d.splitlines()


def test_digest_stops_at_the_held_call_and_skips_subagent_rows():
    rows = session_rows()
    rows.insert(3, dict(said("SIDECHAIN_TEXT"), isSidechain=True))
    rows.append(use("later1", "Read", file_path="/repo/after.py"))
    d = ah.build_digest(rows, CWD, HELD)
    assert "SIDECHAIN_TEXT" not in d and "after.py" not in d


def test_digest_marks_a_transcript_that_lags():
    d = ah.build_digest(session_rows()[:-1], CWD, dict(HELD, may_lag=True))
    assert "may lag the held write" in d
    assert d.splitlines()[-1] == "about to Edit src/x.py"


def test_digest_leaves_out_every_thinking_block():
    rows = [prompt("task"), thought("SECRET_REASONING " + "x" * 50000),
            said("PLAN:\n1. GOAL fix x"), use("held1", "Edit", file_path="/repo/src/x.py")]
    d = ah.build_digest(rows, CWD, HELD)
    assert "SECRET_REASONING" not in d and "THOUGHT" not in d
    assert d.splitlines() == ["[prompt: the task]", "SAID: PLAN:", "1. GOAL fix x", "about to Edit src/x.py"]


def test_digest_caps_shrink_earlier_messages_first_and_keep_every_action_line():
    rows = [prompt("task")]
    for k in range(12):
        rows += [said("S%d first line\n%s" % (k, "x" * 10000)),
                 use("a%d" % k, "Read", file_path="/repo/f%d.py" % k), result("a%d" % k, "l\nl")]
    rows += [said("LAST_SAID_INTACT " + "s" * 3000), use("held1", "Edit", file_path="/repo/src/x.py")]
    d = ah.build_digest(rows, CWD, HELD)
    assert len(d) <= ah.DIGEST_MAX
    for k in range(12):
        assert "Read f%d.py -> 2 lines" % k in d
    assert "LAST_SAID_INTACT " + "s" * 3000 in d              # the PLAN briefing keeps 4000
    assert d.count("characters cut]") == 12                  # every earlier 10k message was cut


def test_digest_drops_the_oldest_lines_only_as_a_last_resort():
    rows = [use("r%d" % k, "Read", file_path="/repo/file%02d.py" % k) for k in range(40)]
    rows += [result("r%d" % k, "l") for k in range(40)]
    rows.append(use("held1", "Edit", file_path="/repo/src/x.py"))
    d = ah.build_digest(rows, CWD, HELD, limit=400)
    assert len(d) <= 400
    assert "earlier lines omitted]" in d.splitlines()[0]
    assert "Read file39.py -> 1 lines" in d and "file00.py" not in d
    assert d.splitlines()[-1] == "about to Edit src/x.py"


def test_digest_collapses_identical_action_lines():
    rows = [use("r%d" % k, "Read", file_path="/repo/src/x.py") for k in range(5)]
    rows += [result("r%d" % k, "a\nb") for k in range(5)]
    rows.append(use("held1", "Edit", file_path="/repo/src/x.py"))
    d = ah.build_digest(rows, CWD, HELD)
    assert d.splitlines() == ["Read src/x.py -> 2 lines (x5)", "about to Edit src/x.py"]


def test_action_line_marks_a_read_error_and_a_missing_result():
    err = ah._action_line({"name": "Read", "input": {"file_path": "/repo/nope.py"}},
                          {"type": "tool_result", "is_error": True,
                           "content": "File does not exist.\nmore"}, CWD)
    assert err == "Read nope.py -> ERROR: File does not exist."
    none = ah._action_line({"name": "Grep", "input": {"pattern": "zzz"}}, None, CWD)
    assert none == 'Grep "zzz" -> (no result)'
    part = ah._action_line({"name": "Read", "input": {"file_path": "/repo/a.py", "offset": 10,
                                                      "limit": 5}},
                           {"type": "tool_result", "content": [{"type": "text", "text": "1\nx"}]},
                           CWD)
    assert part == "Read a.py lines 10-14 -> 2 lines"


def test_digest_counts_bash_output_and_withholds_a_secret_behind_a_chain():
    rows = [use("b1", "Bash", command="cat notes.txt"), result("b1", "DB_PASSWORD=hunter2"),
            use("b2", "Bash", command="qwen-test; cat notes.txt"),
            result("b2", "DB_PASSWORD=hunter2", is_error=True),
            use("b3", "Bash", command="cat .env|head -1"), result("b3", "API_KEY=abc123"),
            use("held1", "Edit", file_path="/repo/src/x.py")]
    assert ah.build_digest(rows, CWD, HELD).splitlines() == [
        'Bash "cat notes.txt" -> 1 lines',
        'Bash "qwen-test; cat notes.txt" -> ERROR: 1 lines',
        "Bash <secret-looking path withheld>",
        "about to Edit src/x.py"]


# ---------------------------------------------------------------- _wait_for

def test_wait_for_sees_a_row_that_appears_later(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(prompt("hi")) + "\n", encoding="utf-8")

    def later():
        time.sleep(0.5)
        with open(str(t), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(use("held1", "Edit", file_path="/repo/a.py")) + "\n")
    threading.Thread(target=later, daemon=True).start()
    assert ah._wait_for(str(t), "held1", 5) is True


def test_wait_for_gives_up_and_says_so(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(prompt("hi")) + "\n", encoding="utf-8")
    t0 = time.time()
    assert ah._wait_for(str(t), "held1", 0.4) is False
    assert time.time() - t0 < 3
    assert ah._wait_for(str(t), "", 0.4) is True              # no id: nothing to wait for


# ---------------------------------------------------------------- the hook

@pytest.fixture
def hk(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(FAKE, encoding="utf-8")
    fake.chmod(0o755)
    calls = tmp_path / "calls"; calls.mkdir()
    state = tmp_path / "state"; state.mkdir()
    task = tmp_path / "task.md"
    task.write_text("# Goal\nMake the loader read value.txt (TASK_MARKER).\n", encoding="utf-8")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in session_rows()), encoding="utf-8")
    payload = {"session_id": "s1", "transcript_path": str(transcript), "cwd": CWD,
               "hook_event_name": "PreToolUse", "tool_name": "Edit",
               "tool_use_id": "held1", "tool_input": HELD["tool_input"]}
    return {"tmp": tmp_path, "fake": fake, "calls": calls, "state": state, "task": task,
            "transcript": transcript, "payload": payload, "record": tmp_path / "plan.jsonl",
            "log": tmp_path / "plan.md", "digest": tmp_path / "plan-digest.md"}


def run_hook(h, payload=None, timeout="30", mode="ok", extra=None):
    env = dict(os.environ, FAKE_PLAN_DIR=str(h["calls"]), FAKE_PLAN_MODE=mode,
               QWEN_PLAN_LAG_WAIT="0.3", QWEN_PLAN_ROUND="1")
    env.update(extra or {})
    argv = [sys.executable, HOOK, "--state", str(h["state"]), "--model", "opus",
            "--task", str(h["task"]), "--record", str(h["record"]), "--log", str(h["log"]),
            "--digest", str(h["digest"]), "--claude", str(h["fake"]), "--timeout", timeout]
    return subprocess.run(argv, input=json.dumps(h["payload"] if payload is None else payload),
                          capture_output=True, text=True, env=env, timeout=90)


def records(h):
    return [json.loads(ln) for ln in h["record"].read_text().splitlines()] if h["record"].exists() else []


def stub_calls(h):
    return sorted(p.name for p in h["calls"].iterdir())


@posix_only
def test_hook_denies_the_first_write_once_with_the_advice(hk):
    r = run_hook(hk)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    reason = spec["permissionDecisionReason"]
    assert "APPROACH: read value.txt first" in reason and "re-issue" in reason.lower()
    assert reason.startswith("This is not an error.")
    assert "plan: opus advised" in out["systemMessage"]
    assert (hk["state"] / "count").read_text() == "1"
    [rec] = records(hk)
    assert (rec["call"], rec["round"], rec["called"], rec["tool"], rec["path"]) == (
        "plan", 1, True, "Edit", "src/x.py")
    assert rec["cost_usd"] == 0.11 and rec["digest_chars"] > 100 and rec["cut"] is False
    assert "APPROACH: read value.txt first" in rec["advice"]
    sent = (hk["calls"] / "stdin.1").read_text()
    assert ah.PLAN_Q in sent and "about to Edit src/x.py" in sent and "TASK_MARKER" in sent
    for secret in (BODY, "ENV_SECRET", EDIT_NEW):
        assert secret not in sent
    assert "THINK_" not in sent and "SAID: PLAN: change x.py." in sent   # no thinking, the briefing is sent
    assert "about to Edit src/x.py" in hk["digest"].read_text()
    assert "TASK_MARKER" in hk["log"].read_text()            # the full brief is logged


def test_hook_lets_a_second_write_through_without_a_call(hk):
    (hk["state"] / "count").write_text("1")
    r = run_hook(hk)
    assert r.returncode == 0 and r.stdout == ""
    assert stub_calls(hk) == [] and records(hk) == []


@posix_only
@pytest.mark.parametrize("mode,timeout", [("err", "30"), ("slow", "1")])
def test_hook_fails_open_and_spends_the_budget(hk, mode, timeout):
    r = run_hook(hk, mode=mode, timeout=timeout)
    assert r.returncode == 0 and r.stdout == ""
    [rec] = records(hk)
    assert rec["called"] is False and rec["skipped_reason"]
    assert (hk["state"] / "count").read_text() == "1"        # never block twice
    assert run_hook(hk, mode="ok").stdout == ""              # the next write passes at once
    assert len(records(hk)) == 1


@posix_only
def test_hook_records_a_safeguard_refusal_as_its_own_reason(hk):
    r = run_hook(hk, mode="flagged")
    assert r.returncode == 0 and r.stdout == ""               # fails open
    [rec] = records(hk)
    assert rec["called"] is False and rec["skipped_reason"] == "refused by Claude's safeguards"


@posix_only
def test_hook_tools_flag_lets_the_plan_call_read_the_code(hk):
    env = dict(os.environ, FAKE_PLAN_DIR=str(hk["calls"]), FAKE_PLAN_MODE="ok",
               QWEN_PLAN_LAG_WAIT="0.3", QWEN_PLAN_ROUND="1")
    argv = [sys.executable, HOOK, "--state", str(hk["state"]), "--model", "opus",
            "--task", str(hk["task"]), "--record", str(hk["record"]), "--log", str(hk["log"]),
            "--digest", str(hk["digest"]), "--claude", str(hk["fake"]), "--timeout", "30", "--tools"]
    payload = dict(hk["payload"], cwd=str(hk["tmp"]))          # the call starts in a real dir
    r = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                       env=env, timeout=90)
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "YOU CAN READ THE CODE" in (hk["calls"] / "stdin.1").read_text()


@posix_only
def test_hook_without_tools_flag_ignores_an_inherited_tools_env(hk):
    run_hook(hk, extra={"QA_ADVISOR_TOOLS": "1"})
    assert "YOU CAN READ THE CODE" not in (hk["calls"] / "stdin.1").read_text()


def test_hook_ignores_a_subagent_write(hk):
    r = run_hook(hk, payload=dict(hk["payload"], agent_id="agent-1"))
    assert r.returncode == 0 and r.stdout == ""
    assert not (hk["state"] / "count").exists()
    assert stub_calls(hk) == [] and records(hk) == []


def test_hook_ignores_a_tool_that_is_not_a_write(hk):
    r = run_hook(hk, payload=dict(hk["payload"], tool_name="Read"))
    assert r.returncode == 0 and r.stdout == ""
    assert not (hk["state"] / "count").exists() and stub_calls(hk) == []


@posix_only
def test_hook_waits_for_a_lagging_transcript(hk):
    rows = session_rows()
    hk["transcript"].write_text("".join(json.dumps(r) + "\n" for r in rows[:-3]), encoding="utf-8")

    def later():
        time.sleep(1.0)
        with open(str(hk["transcript"]), "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in
                             [said("LATE_SAID before the edit")] + rows[-2:]))
    threading.Thread(target=later, daemon=True).start()
    r = run_hook(hk, extra={"QWEN_PLAN_LAG_WAIT": "8"})
    assert r.returncode == 0, r.stderr
    d = hk["digest"].read_text()
    assert "SAID: LATE_SAID" in d and "may lag" not in d
    assert records(hk)[0]["called"] is True


@posix_only
def test_hook_marks_a_digest_whose_transcript_never_caught_up(hk):
    rows = session_rows()[:-1]                               # the held tool_use row is missing
    hk["transcript"].write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    r = run_hook(hk)
    assert r.returncode == 0, r.stderr
    assert "may lag the held write" in hk["digest"].read_text()
    assert records(hk)[0]["called"] is True


def test_hook_without_a_transcript_allows_and_records_why(hk):
    r = run_hook(hk, payload=dict(hk["payload"], transcript_path=str(hk["tmp"] / "missing.jsonl")))
    assert r.returncode == 0 and r.stdout == ""
    [rec] = records(hk)
    assert rec["called"] is False and rec["skipped_reason"] == "no transcript"
    assert stub_calls(hk) == []


@posix_only
def test_hook_withholds_a_secret_looking_held_path(hk):
    payload = dict(hk["payload"], tool_name="Write",
                   tool_input={"file_path": "/repo/.env", "content": "K=v"})
    hk["transcript"].write_text(
        "".join(json.dumps(r) + "\n" for r in session_rows()[:-1]
                + [use("held1", "Write", file_path="/repo/.env", content="K=v")]), encoding="utf-8")
    r = run_hook(hk, payload=payload)
    assert r.returncode == 0, r.stderr
    assert hk["digest"].read_text().splitlines()[-1] == "about to Write <secret-looking path withheld>"
    assert records(hk)[0]["path"] == "<secret-looking path withheld>"
    assert ".env" not in (hk["calls"] / "stdin.1").read_text()


@posix_only
def test_hook_spends_the_budget_on_an_unexpected_error(hk):
    rows = [prompt("task"), use("b1", "Read", file_path="/repo/a.py"),
            {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "b1", "content": [{"type": "text", "text": None}]}]}},
            use("held1", "Edit", file_path="/repo/src/x.py")]
    hk["transcript"].write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    r = run_hook(hk)
    assert r.returncode == 0 and r.stdout == ""               # fails open, not a traceback
    assert (hk["state"] / "count").read_text() == "1"         # and never held twice


@posix_only
def test_hook_still_asks_when_the_transcript_holds_a_lone_surrogate(hk):
    hk["transcript"].write_text("".join(json.dumps(r) + "\n" for r in [said("lone \ud800 here")] + session_rows()),
                                encoding="utf-8")
    r = run_hook(hk)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (hk["state"] / "count").read_text() == "1" and records(hk)[0]["called"] is True


@posix_only
def test_hook_bad_command_line_still_allows_the_write(hk):
    r = subprocess.run([sys.executable, HOOK, "--state", str(hk["state"]), "--model", "opus",
                        "--timeout", "x"], input=json.dumps(hk["payload"]),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == ""                # exit 2 would block the write


# ---------------------------------------------------------------- the gate after the advice

def _advised(hk, after_rows):
    """Plan advice delivered for held1, then `after_rows`, then a re-issued Edit (held2)."""
    r = run_hook(hk)
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    deny = result("held1", "This is not an error. A stronger model reviewed your plan", is_error=True)
    rows = session_rows() + [deny] + after_rows + [use("held2", "Edit", file_path="/repo/src/x.py")]
    hk["transcript"].write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
    return run_hook(hk, payload=dict(hk["payload"], tool_use_id="held2"))


@posix_only
def test_gate_holds_a_reissued_edit_once_when_the_advice_was_ignored(hk):
    r = _advised(hk, [])
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "ADVICE RESPONSE" in out["permissionDecisionReason"]
    assert "CHECK NEXT" in out["permissionDecisionReason"]
    gate = [x for x in records(hk) if x["call"] == "plan-gate"]
    assert gate == [{"call": "plan-gate", "responded": False, "checked": False, "held": True}]
    again = run_hook(hk, payload=dict(hk["payload"], tool_use_id="held3"))
    assert again.stdout == ""                                 # never held twice
    assert stub_calls(hk) == ["stdin.1"]                      # the gate makes no model call


@posix_only
def test_gate_asks_only_for_what_is_missing(hk):
    r = _advised(hk, [said("ADVICE RESPONSE:\n1. DONE will check")])
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "CHECK NEXT" in reason and "Write the ADVICE RESPONSE" not in reason


@posix_only
def test_gate_lets_the_edit_through_when_the_advice_was_acted_on(hk):
    r = _advised(hk, [said("ADVICE RESPONSE:\n1. DONE read value.txt"),
                      use("c1", "Read", file_path="/repo/value.txt"), result("c1", "v")])
    assert r.stdout == ""
    assert [x for x in records(hk) if x["call"] == "plan-gate"][0]["held"] is False


@posix_only
def test_gate_is_off_when_the_plan_call_failed(hk):
    run_hook(hk, mode="err")
    assert run_hook(hk, payload=dict(hk["payload"], tool_use_id="held2")).stdout == ""
    assert [x["call"] for x in records(hk)] == ["plan"]
