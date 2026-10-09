"""wf.claude_check (lib/swarm_engine/claude_check.py) against a fake `claude`: a Python
script that records argv, env, cwd, stdin (and whether stdin is a pipe) per call and
answers by the MODE= word in its prompt. On POSIX it runs as the executable `claude`; on
Windows the one Windows test wraps it in a claude.cmd found on PATH. No network, no real
model. `auth status` (the availability probe) answers from FAKE_CC_LOGGEDIN without being
recorded as a check call; the probe tests point its HEAD at a local server or a dead port."""
import hashlib
import http.server
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

import pytest

from lib import browser_mcp
from lib.swarm_engine import claude_check as cc
from swarm_fixtures import make_workflow  # noqa: F401  (only the INTERRUPTED child imports it)
from test_swarm_api import workflow

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake claude is a POSIX script")
TESTS = pathlib.Path(__file__).resolve().parent
SA = TESTS.parent / "skill" / "local-auditor"

# The REAL offline claude 2.1.x (captured 2026-10-09: `claude -p --output-format json`
# with HTTPS_PROXY at a dead port): exit 1 after ~180 s with a PARSEABLE envelope.
OFFLINE_ENVELOPE = json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "terminal_reason": "api_error", "api_error_status": None,
    "duration_ms": 179117, "duration_api_ms": 0, "num_turns": 1,
    "total_cost_usd": 0,
    "result": "API Error: Connection refused — a firewall or proxy may be blocking it "
              "(ECONNREFUSED)",
    "session_id": "af379538-6c75-4455-afd5-ca79cf0bb924"})

FAKE = r'''#!/usr/bin/env python3
import json, os, stat, subprocess, sys, time
d = os.environ["FAKE_CC_DIR"]
if sys.argv[1:3] == ["auth", "status"]:        # the availability probe: NOT a check call
    a = len([f for f in os.listdir(d) if f.startswith("auth.")]) + 1
    open(os.path.join(d, "auth.%d" % a), "w").write("status\n")
    if os.environ.get("FAKE_CC_AUTH_RC"):      # FAKE_CC_AUTH_RC: the probe's binary cannot
        sys.exit(int(os.environ["FAKE_CC_AUTH_RC"]))   # be started at all (a 127, say)
    if os.environ.get("FAKE_CC_AUTH"):         # FAKE_CC_AUTH: what auth status prints instead
        print(os.environ["FAKE_CC_AUTH"])
        sys.exit(1)
    print('{"loggedIn": %s}' % os.environ.get("FAKE_CC_LOGGEDIN", "true"))
    sys.exit(0)
gate = os.path.join(d, "fail126")
if os.path.exists(gate):
    left = int(open(gate).read() or 0)
    if left > 0:
        open(gate, "w").write(str(left - 1))
        sys.exit(int(os.environ.get("FAKE_CC_GATE_RC") or 126))   # 126, or 127 for absent
stdin_fifo = stat.S_ISFIFO(os.fstat(0).st_mode)   # a pipe from the runner, not its fd 0
prompt = sys.stdin.read()
n = len([f for f in os.listdir(d) if f.startswith("call.")]) + 1
with open(os.path.join(d, "call.%d" % n), "w") as fh:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
               "files": sorted(os.listdir(".")), "prompt": prompt,
               "stdin_fifo": stdin_fifo}, fh)
mode = os.environ.get("FAKE_CC_FORCE") or (
    prompt.split("MODE=", 1)[1].split()[0] if "MODE=" in prompt else "ok")
item = prompt.split("ITEM=", 1)[1].split()[0] if "ITEM=" in prompt else "?"


def result(**kw):
    rec = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
           "total_cost_usd": 0.25, "usage": {"input_tokens": 100, "output_tokens": 20},
           "result": json.dumps({"seen": item})}
    rec.update(kw)
    print(json.dumps(rec))


if mode == "child":
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.path.join(d, "grandchild.pid"), "w").write(str(g.pid))
    time.sleep(120)
elif mode == "maxturns":
    result(subtype="error_max_turns", is_error=True, result=None)
elif mode == "budget":
    result(subtype="error_max_budget_usd", is_error=True, result=None)
elif mode == "iserr":
    result(is_error=True, result="the tool crashed")
elif mode == "page403":
    result(is_error=True, result="navigation returned 403 Forbidden")
elif mode == "creds":
    result(is_error=True, result="credential field not found")
elif mode == "appdown":
    result(is_error=True, result="net::ERR_CONNECTION_REFUSED at http://127.0.0.1:8501")
elif mode == "pageconn":                            # the page's words, an ordinary error
    result(is_error=True, terminal_reason="completed", duration_api_ms=5300,
           result="net::ERR_CONNECTION_REFUSED at http://127.0.0.1:8501")
elif mode == "ratelimit":                           # a usage limit: the item's own
    result(is_error=True, terminal_reason="api_error", api_error_status=429,
           duration_api_ms=0, result="API Error: 429 rate limit")
elif mode == "offline":                             # the real CLI offline: exit 1 with
    sys.stdout.write(os.environ["FAKE_CC_ENVELOPE"] + "\n")   # a parseable envelope
    sys.exit(1)
elif mode == "notlogin":
    result(is_error=True, result="Not logged in · Please run /login", total_cost_usd=0)
elif mode == "maxturns401":
    result(subtype="error_max_turns", is_error=True,
           result="stopped: API Error: 401, not logged in?")
elif mode == "env127":
    result()
    sys.exit(127)
elif mode == "netfail":                             # the CLI itself: stderr, no envelope
    sys.stderr.write("Connection error\n")
    sys.exit(1)
elif mode == "prose":
    result(result="It looks fine to me.")
elif mode == "auth":
    result(is_error=True, result="Invalid API key · Please run /login", total_cost_usd=0)
elif mode == "junk":
    print("not json")
    sys.exit(1)
else:
    result()
'''


@pytest.fixture
def fake_cc(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE, encoding="utf-8")
    fake.chmod(0o755)
    d = tmp_path / "cc"
    d.mkdir()
    monkeypatch.setenv("FAKE_CC_DIR", str(d))
    monkeypatch.setenv("QWEN_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("QWEN_EXEC_RETRY_BACKOFF", "0 0")
    monkeypatch.delenv("FAKE_CC_FORCE", raising=False)
    monkeypatch.delenv("FAKE_CC_AUTH", raising=False)
    monkeypatch.delenv("FAKE_CC_AUTH_RC", raising=False)
    monkeypatch.delenv("FAKE_CC_GATE_RC", raising=False)
    monkeypatch.delenv("FAKE_CC_LOGGEDIN", raising=False)
    monkeypatch.delenv("QWEN_PLAYWRIGHT_MCP", raising=False)
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL",
              "CLAUDE_EFFORT", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.setenv(k, "qwen")
    return d


def cc_calls(d):
    n = len(list(pathlib.Path(d).glob("call.*")))
    return [json.loads((pathlib.Path(d) / ("call.%d" % i)).read_text(encoding="utf-8"))
            for i in range(1, n + 1)]


def auth_probes(d):
    """Every `auth status` the fake answered (one file each, never a call.*): how often
    the probe spoke to the binary."""
    return sorted(pathlib.Path(d).glob("auth.*"))


def probe_events(tmp_path):
    """The run's claude_probe events (events.jsonl holds every kind)."""
    path = pathlib.Path(tmp_path) / "run" / "events.jsonl"
    if not path.exists():
        return []
    return [e for e in (json.loads(x) for x in
                        path.read_text(encoding="utf-8").splitlines() if x.strip())
            if e["kind"] == "claude_probe"]


def ask(it):
    return "check ITEM=%s MODE=%s" % (it["id"], it.get("mode", "ok"))


def parse(text, item):
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def states(res):
    return [(r["item"]["id"], r["state"]) for r in res]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                           # a zombie is dead too
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def gone(pid, wait=10.0):
    end = time.time() + wait
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


# ------------------------------------------------------------------ the call

@posix_only
def test_ok_call_argv_env_cwd_and_stdin(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    [r] = wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus", max_calls=5)
    assert r == {"item": {"id": "A"}, "state": "ok", "data": {"seen": "A"}, "why": ""}
    [call] = cc_calls(fake_cc)
    assert call["argv"] == ["-p", "--model", "opus", "--output-format", "json",
                            "--max-turns", "40", "--max-budget-usd", "2",
                            "--strict-mcp-config", "--setting-sources", "",
                            "--no-session-persistence", "--permission-mode", "dontAsk",
                            "--restricted", "--tools", "Read", "--allowedTools", "Read"]
    assert call["prompt"] == "check ITEM=A MODE=ok"           # on stdin, not in argv
    assert call["stdin_fifo"] is True                         # a pipe, never an inherited fd 0
    assert os.path.realpath(call["cwd"]) == os.path.realpath(str(tmp_path / "run" / "agents" / "c-A"))
    leaked = [k for k in call["env"] if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
              or k in ("CLAUDE_EFFORT", "AWS_BEARER_TOKEN_BEDROCK")]
    assert leaked == []
    assert call["env"]["HOME"] == os.environ["HOME"]          # the login lives under HOME


@posix_only
def test_knobs_reach_the_flags(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    wf.claude_check("c", [{"id": "A"}], ask, parse, model="sonnet", max_calls=1,
                    budget_usd=0.5, max_turns=7)
    argv = cc_calls(fake_cc)[0]["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5"


@posix_only
def test_bare_claude_is_found_on_path(tmp_path, fake_cc, monkeypatch):
    monkeypatch.delenv("QWEN_CLAUDE_BIN")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    wf = workflow(tmp_path)
    [r] = wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus", max_calls=1)
    assert r["state"] == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="claude.cmd is the Windows npm install")
def test_claude_cmd_is_found_on_path_on_windows(tmp_path, fake_cc, monkeypatch):
    # npm installs claude as claude.cmd, and CreateProcess finds neither it nor a bare
    # "claude" the way a shell does: resolve() goes through shutil.which (PATHEXT).
    winbin = tmp_path / "winbin"
    winbin.mkdir()
    (winbin / "fake_claude.py").write_text(FAKE, encoding="utf-8")
    (winbin / "claude.cmd").write_text('@"%s" "%%~dp0fake_claude.py" %%*\r\n' % sys.executable,
                                       encoding="utf-8")
    monkeypatch.delenv("QWEN_CLAUDE_BIN")
    monkeypatch.setenv("PATH", str(winbin) + os.pathsep + os.environ["PATH"])
    assert cc.resolve(dict(os.environ)).lower() == str(winbin / "claude.cmd").lower()
    wf = workflow(tmp_path)
    [r] = wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus", max_calls=1)
    assert r["state"] == "ok", r
    [call] = cc_calls(fake_cc)
    assert call["prompt"] == "check ITEM=A MODE=ok" and call["stdin_fifo"] is True


def test_wf_claude_check_looks_up_run_at_call_time(tmp_path, monkeypatch):
    # api.Workflow.claude_check reaches claude_check.run through the module attribute
    # on every call, so a workflow test can replace it without a fake binary.
    seen = []

    def fake_run(wf, name, items, prompt, parse, **kw):
        seen.append((wf, name, list(items), kw))
        return ["faked"]

    monkeypatch.setattr(cc, "run", fake_run)
    wf = workflow(tmp_path)
    assert wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus", max_calls=3) == ["faked"]
    [(got_wf, name, items, kw)] = seen
    assert got_wf is wf and name == "c" and items == [{"id": "A"}]
    assert kw == dict(model="opus", max_calls=3, budget_usd=2.0, browser=False, stage=None,
                      read_dirs=(), item_id=None, timeout=600, max_turns=40)


@posix_only
def test_browser_read_dirs_and_stage(tmp_path, fake_cc):
    fixtures = tmp_path / "fx"
    (fixtures / "sub").mkdir(parents=True)
    (fixtures / "a.png").write_bytes(b"\x89PNG")
    (fixtures / "sub" / "b.txt").write_text("b", encoding="utf-8")
    os.symlink(str(fixtures), str(fixtures / "loop"))          # a dir link: never followed
    extra = tmp_path / "evidence"
    extra.mkdir()
    wf = workflow(tmp_path)
    [r] = wf.claude_check("c", [{"id": "A/1"}], ask, parse, model="opus", max_calls=1,
                          browser=True, stage=str(fixtures), read_dirs=[str(extra)])
    assert r["state"] == "ok"
    argv = cc_calls(fake_cc)[0]["argv"]
    bdir = tmp_path / "run" / "browser" / "c-A_1"
    adds = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert adds == [str(extra), str(bdir)]
    cfg = argv[argv.index("--mcp-config") + 1]
    assert cfg == str(bdir / "mcp.json")
    assert pathlib.Path(cfg).read_text(encoding="utf-8") == browser_mcp.text(browser_mcp.build(str(bdir)))
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert denied == ["mcp__playwright__browser_run_code_unsafe", "mcp__playwright__browser_install",
                      "mcp__playwright__browser_network_request"]
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert allowed[0] == "Read"
    assert "mcp__playwright__browser_evaluate" in allowed
    assert "mcp__playwright__browser_network_requests" in allowed
    assert "mcp__playwright__browser_file_upload" in allowed
    assert not set(denied) & set(allowed)
    unit = tmp_path / "run" / "agents" / "c-A_1"
    assert unit / "fixtures" == wf.stage_dir("c-A_1")      # staging's folder, inside the cwd
    assert cc_calls(fake_cc)[0]["files"] == ["fixtures"]
    assert (unit / "fixtures" / "a.png").read_bytes() == b"\x89PNG"
    assert (unit / "fixtures" / "sub" / "b.txt").read_text(encoding="utf-8") == "b"
    assert not (unit / "fixtures" / "loop").exists()


@posix_only
def test_per_item_failures_do_not_trip_the_breaker(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    its = [{"id": "T", "mode": "maxturns"}, {"id": "B", "mode": "budget"},
           {"id": "E", "mode": "iserr"}, {"id": "P", "mode": "prose"}, {"id": "K"}]
    res = wf.claude_check("c", its, ask, parse, model="opus", max_calls=9, max_turns=40)
    assert states(res) == [("T", "failed"), ("B", "failed"), ("E", "failed"),
                           ("P", "failed"), ("K", "ok")]
    assert res[0]["why"] == "stopped at the 40-turn limit"
    assert res[1]["why"] == "stopped at the $2 budget"
    assert res[2]["why"] == "claude reported an error: the tool crashed"
    assert res[3]["why"].startswith("unusable answer: ")
    assert len(cc_calls(fake_cc)) == 5


@posix_only
def test_timeout_kills_the_group_and_fails_only_that_item(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    t0 = time.time()
    res = wf.claude_check("c", [{"id": "S", "mode": "child"}, {"id": "K"}], ask, parse,
                          model="opus", max_calls=2, timeout=2)
    assert time.time() - t0 < 30
    assert states(res) == [("S", "failed"), ("K", "ok")]
    assert res[0]["why"] == "no answer within 2s"
    pid = int((fake_cc / "grandchild.pid").read_text())
    assert gone(pid), "the fake claude's child outlived the timeout"


@posix_only
@pytest.mark.parametrize("first,why", [
    ("auth", "claude is not available: Invalid API key"),
    ("junk", "claude gave no readable answer (exit 1): not json")])
def test_breaker_trips_on_availability_failures(tmp_path, fake_cc, first, why):
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": first}, {"id": "B"}, {"id": "C"}], ask,
                          parse, model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable"), ("C", "unavailable")]
    assert all(r["why"].startswith(why) for r in res), res
    assert len(cc_calls(fake_cc)) == 1                     # B and C were never asked


@posix_only
def test_unreadable_envelope_after_the_first_call_is_per_item(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B", "mode": "junk"}, {"id": "C"}], ask,
                          parse, model="opus", max_calls=9)
    assert states(res) == [("A", "ok"), ("B", "failed"), ("C", "ok")]


# ------------------------------------------------- the page's words never trip the breaker

@posix_only
def test_page_403_in_an_error_envelope_is_per_item(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "page403"}, {"id": "B"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "failed"), ("B", "ok")]
    assert res[0]["why"] == "claude reported an error: navigation returned 403 Forbidden"
    assert len(cc_calls(fake_cc)) == 2                      # B was still asked


@posix_only
def test_page_credential_text_is_per_item(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "creds"}, {"id": "B"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "failed"), ("B", "ok")]
    assert res[0]["why"] == "claude reported an error: credential field not found"
    assert len(cc_calls(fake_cc)) == 2                      # B was still asked


@posix_only
def test_app_down_in_an_error_envelope_is_per_item(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "appdown"}, {"id": "B"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "failed"), ("B", "ok")]
    assert res[0]["why"] == ("claude reported an error: net::ERR_CONNECTION_REFUSED "
                             "at http://127.0.0.1:8501")
    assert len(cc_calls(fake_cc)) == 2                      # B was still asked


@posix_only
def test_api_error_envelope_without_is_error_trips_the_breaker(tmp_path, fake_cc, monkeypatch):
    # spec's first branch is terminal_reason ALONE: a success envelope with is_error
    # false still breaks the run -- the CLI-level field is the trigger, not its text
    monkeypatch.setenv("FAKE_CC_ENVELOPE", json.dumps(
        {"type": "result", "subtype": "success", "is_error": False,
         "terminal_reason": "api_error", "duration_api_ms": 0,
         "result": "{\"seen\": \"A\"}"}))
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "offline"}, {"id": "B", "mode": "offline"}],
                          ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable")]
    assert all(r["why"] == 'claude is not available: {"seen": "A"}' for r in res), res
    assert len(cc_calls(fake_cc)) == 1                      # the answer is never parsed


@posix_only
def test_page_connection_refused_in_a_normal_error_is_per_item(tmp_path, fake_cc):
    # the page's words in an ordinary is_error result: no api_error field, and the API
    # call itself worked (duration_api_ms > 0) -- that row failed, not claude
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "pageconn"}, {"id": "B"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "failed"), ("B", "ok")]
    assert res[0]["why"] == ("claude reported an error: net::ERR_CONNECTION_REFUSED "
                             "at http://127.0.0.1:8501")
    assert len(cc_calls(fake_cc)) == 2                      # B was still asked


@posix_only
def test_rate_limited_api_error_is_per_item(tmp_path, fake_cc):
    # a usage limit is the item's own (by design): a 429 envelope stays failed
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "ratelimit"}, {"id": "B"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "failed"), ("B", "ok")]
    assert res[0]["why"] == "claude reported an error: API Error: 429 rate limit"
    assert len(cc_calls(fake_cc)) == 2                      # B was still asked


@posix_only
def test_max_turns_is_per_item_even_with_auth_words(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "maxturns401"}, {"id": "B"}], ask, parse,
                          model="opus", max_calls=9, max_turns=3)
    assert states(res) == [("A", "failed"), ("B", "ok")]    # the subtype is read first
    assert res[0]["why"] == "stopped at the 3-turn limit"
    assert len(cc_calls(fake_cc)) == 2                      # B was still asked


@posix_only
def test_cli_not_logged_in_trips_the_breaker(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "notlogin"}, {"id": "B"}, {"id": "C"}],
                          ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable"), ("C", "unavailable")]
    assert all(r["why"].startswith("claude is not available: Not logged in") for r in res)
    assert len(cc_calls(fake_cc)) == 1                      # one call total
    # and mid-run: after a good answer, a later item's "Not logged in" envelope breaks
    # the run too -- the breaker does not only guard the first call
    res = wf.claude_check("c2", [{"id": "A"}, {"id": "N", "mode": "notlogin"}, {"id": "B"}],
                          ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "ok"), ("N", "unavailable"), ("B", "unavailable")]
    assert len(cc_calls(fake_cc)) == 3                      # B was never asked


@posix_only
def test_cli_network_failure_without_an_envelope_trips_the_breaker(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "netfail"}, {"id": "B"}, {"id": "C"}],
                          ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable"), ("C", "unavailable")]
    assert all(r["why"].startswith("claude gave no readable answer") for r in res)
    assert len(cc_calls(fake_cc)) == 1                      # one call total
    # and NET_DOWN trips mid-run too: after a good answer the CLI's "Connection error"
    # (stderr, no envelope) breaks the run instead of reading as per-item junk
    res = wf.claude_check("c2", [{"id": "A"}, {"id": "N", "mode": "netfail"}, {"id": "B"}],
                          ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "ok"), ("N", "unavailable"), ("B", "unavailable")]
    assert [c["prompt"] for c in cc_calls(fake_cc)] == [    # B was never asked
        "check ITEM=A MODE=netfail", "check ITEM=A MODE=ok", "check ITEM=N MODE=netfail"]


@posix_only
def test_real_offline_envelope_trips_the_breaker(tmp_path, fake_cc, monkeypatch):
    # the captured offline claude 2.1.x envelope: exit 1 with a PARSEABLE result object,
    # so the breaker must read the CLI's own fields, not NET_DOWN's nothing-parsed text
    monkeypatch.setenv("FAKE_CC_ENVELOPE", OFFLINE_ENVELOPE)
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A", "mode": "offline"}, {"id": "B", "mode": "offline"},
                                {"id": "C", "mode": "offline"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable"), ("C", "unavailable")]
    assert all("Connection refused" in r["why"] for r in res), res
    assert len(cc_calls(fake_cc)) == 1                      # one call, not one per row


@posix_only
def test_api_error_mid_run_trips_the_breaker(tmp_path, fake_cc, monkeypatch):
    monkeypatch.setenv("FAKE_CC_ENVELOPE", OFFLINE_ENVELOPE)
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "N", "mode": "offline"},
                                {"id": "B", "mode": "offline"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "ok"), ("N", "unavailable"), ("B", "unavailable")]
    assert [c["prompt"] for c in cc_calls(fake_cc)] == [    # B was never asked
        "check ITEM=A MODE=ok", "check ITEM=N MODE=offline"]


@posix_only
def test_exit_127_after_an_envelope_is_not_retried(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    [r] = wf.claude_check("c", [{"id": "A", "mode": "env127"}], ask, parse,
                          model="opus", max_calls=1)
    assert r["state"] == "ok"                               # the envelope is read
    assert len(cc_calls(fake_cc)) == 1                      # no re-resolve-and-retry
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8")
    assert "retrying" not in log


@posix_only
def test_not_found_trips_the_breaker_after_the_retries(tmp_path, fake_cc, monkeypatch):
    monkeypatch.setenv("QWEN_CLAUDE_BIN", str(tmp_path / "no-such-claude"))
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}], ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable")]
    assert res[0]["why"].startswith("claude could not be executed (exit 127)")
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8")
    assert log.count("claude could not be executed (exit 127); retrying in 0s") == 2


@posix_only
def test_exec_126_once_is_retried(tmp_path, fake_cc):
    (fake_cc / "fail126").write_text("1", encoding="utf-8")
    wf = workflow(tmp_path)
    [r] = wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus", max_calls=1)
    assert r["state"] == "ok"
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8")
    assert log.count("c-A: claude could not be executed (exit 126); retrying in 0s") == 1


@posix_only
def test_exec_126_past_the_retries_is_unavailable(tmp_path, fake_cc):
    (fake_cc / "fail126").write_text("3", encoding="utf-8")
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}], ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable")]
    assert res[0]["why"].startswith("claude could not be executed (exit 126)")
    assert cc_calls(fake_cc) == []


def test_backoff_reads_the_variable():
    assert cc.backoff({}) == (10, 30)
    assert cc.backoff({"QWEN_EXEC_RETRY_BACKOFF": "0 0"}) == (0, 0)
    assert cc.backoff({"QWEN_EXEC_RETRY_BACKOFF": "5"}) == (5,)
    for bad in ("x 1", "-1 3"):
        assert cc.backoff({"QWEN_EXEC_RETRY_BACKOFF": bad}) == (10, 30)


def test_blank_backoff_means_no_retry():
    # SET but blank is "no retry" -- qwen-agent.sh reads ${VAR-10 30}, so a blank config
    # value means blank delays, not "unreadable, fall back to the default"
    for blank in ("", " ", "   "):
        assert cc.backoff({"QWEN_EXEC_RETRY_BACKOFF": blank}) == ()


def test_unset_backoff_is_the_default():
    assert cc.backoff({}) == cc.DEFAULT_BACKOFF == (10, 30)


def test_bad_arguments_are_refused(tmp_path):
    wf = workflow(tmp_path)
    with pytest.raises(ValueError, match="model"):
        wf.claude_check("c", [], ask, parse, model="", max_calls=1)
    with pytest.raises(ValueError, match="max_calls"):
        wf.claude_check("c", [], ask, parse, model="opus", max_calls=-1)
    with pytest.raises(ValueError, match="artifact key"):
        wf.claude_check("../c", [], ask, parse, model="opus", max_calls=1)
    with pytest.raises(ValueError, match="share the id"):
        wf.claude_check("c", [{"id": "a"}, {"id": "A"}], ask, parse, model="opus", max_calls=1)


def test_a_bad_stage_is_refused_before_any_call(tmp_path, monkeypatch):
    from lib.swarm_engine import staging
    monkeypatch.setenv("QWEN_CLAUDE_BIN", str(tmp_path / "must-not-run"))
    wf = workflow(tmp_path)
    with pytest.raises(ValueError, match="not a folder"):
        wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus", max_calls=1,
                        stage=str(tmp_path / "missing"))
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "big.bin").write_bytes(b"x" * 11)
    monkeypatch.setattr(staging, "MAX_BYTES", 10)          # staging's limit, read on call
    with pytest.raises(ValueError, match="over the limit"):
        wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus", max_calls=1, stage=str(fx))
    assert not (tmp_path / "run" / "agents" / "c-A").exists()


INTERRUPTED = r'''
import pathlib, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from lib import swarm
from lib.swarm_engine import api, manifest
from swarm_fixtures import make_workflow
tmp = pathlib.Path(sys.argv[3])
m = manifest.load(make_workflow(tmp / "wf"))
run = tmp / "run"
run.mkdir(exist_ok=True)
sw = swarm.Swarm([sys.executable, "-c", "pass"], run, seats=1, timeout=60)
cfg = {"goal": "g", "depth": "quick", "items": 3, "max_agents": 1, "max_items": 1,
       "timeout_per_item": 100, "retries": 0, "rounds": 1, "target": None}
wf = api.Workflow(m, cfg, run, sw, goal="g")
try:
    wf.claude_check("c", [{"id": "A"}], lambda it: "ITEM=A MODE=child",
                    lambda t, it: t, model="opus", max_calls=1)
except KeyboardInterrupt:
    print("interrupted", flush=True)
    sys.exit(130)
print("not interrupted", flush=True)
'''


@posix_only
def test_keyboard_interrupt_kills_the_group_and_propagates(tmp_path, fake_cc):
    script = tmp_path / "interrupted.py"
    script.write_text(INTERRUPTED, encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script), str(SA), str(TESTS), str(tmp_path)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pidfile = fake_cc / "grandchild.pid"
    end = time.time() + 30
    while not pidfile.exists() and time.time() < end:
        time.sleep(0.1)
    assert pidfile.exists(), p.communicate(timeout=5)
    time.sleep(0.2)
    p.send_signal(signal.SIGINT)              # the runner only: claude has its own group
    out, err = p.communicate(timeout=30)
    assert p.returncode == 130 and out.strip() == "interrupted", (out, err)
    assert gone(int(pidfile.read_text())), "the fake claude's child outlived the interrupt"


# ------------------------------------------------------------------ cap, deadline, cache, records

@posix_only
def test_cap_counts_calls_made_not_cache_hits(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    assert states(wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus",
                                  max_calls=1)) == [("A", "ok")]
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}, {"id": "C"}], ask, parse,
                          model="opus", max_calls=1)
    assert states(res) == [("A", "ok"), ("B", "ok"), ("C", "over_cap")]
    assert res[0]["why"] == "cached" and res[0]["data"] == {"seen": "A"}
    assert res[2]["why"] == "over the cap of 1 calls"
    assert [c["prompt"] for c in cc_calls(fake_cc)] == ["check ITEM=A MODE=ok",
                                                        "check ITEM=B MODE=ok"]


@posix_only
def test_deadline_stops_new_calls_but_not_cache_hits(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus", max_calls=5)
    wf.sw.deadline = time.time() - 1
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}], ask, parse, model="opus", max_calls=5)
    assert states(res) == [("A", "ok"), ("B", "deadline")]
    assert len(cc_calls(fake_cc)) == 1
    assert wf.not_run == 0                                 # the workflow maps the state
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8").splitlines()
    assert "c-B\tclaude-check\t-\t0\t0\tdeadline: B not started" in log


@posix_only
def test_cache_holds_only_ok_and_is_saved_after_each_call(tmp_path, fake_cc):
    wf = workflow(tmp_path)
    seen_on_disk = []

    def watching(text, item):
        seen_on_disk.append(sorted(wf.load("claude-c") or {}))
        return parse(text, item)

    res = wf.claude_check("c", [{"id": "A"}, {"id": "F", "mode": "iserr"}, {"id": "B"}],
                          ask, watching, model="opus", max_calls=9)
    assert states(res) == [("A", "ok"), ("F", "failed"), ("B", "ok")]
    assert seen_on_disk == [[], ["A"]]                     # A was on disk before B was asked
    saved = json.loads((tmp_path / "run" / "claude-c.json").read_text(encoding="utf-8"))
    assert sorted(saved) == ["A", "B"]
    assert saved["A"]["data"] == {"seen": "A"} and len(saved["A"]["key"]) == 64


@posix_only
def test_unserializable_answer_is_recorded_and_not_cached(tmp_path, fake_cc):
    def setparse(text, item):
        data = parse(text, item)
        return set(data) if item.get("sets") else data       # a set is not JSON

    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "S", "sets": True}, {"id": "B"}], ask, setparse,
                          model="opus", max_calls=9)
    assert states(res) == [("S", "ok"), ("B", "ok")]         # paid for: kept; B still asked
    assert res[0]["data"] == {"seen"}
    run = tmp_path / "run"
    lines = [json.loads(x) for x in (run / "claude" / "calls.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [(x["item"], x["state"]) for x in lines] == [("S", "ok"), ("B", "ok")]
    log = (run / "run.log").read_text(encoding="utf-8")
    assert ("claude-check: the answer for S was not cached (not JSON-serializable)" in log)
    saved = json.loads((run / "claude-c.json").read_text(encoding="utf-8"))
    assert sorted(saved) == ["B"]                            # S never entered the cache
    wf = workflow(tmp_path)                                  # a resume: S is asked again
    assert states(wf.claude_check("c", [{"id": "S", "sets": True}], ask, setparse,
                                  model="opus", max_calls=9)) == [("S", "ok")]
    assert len(cc_calls(fake_cc)) == 3


@posix_only
def test_mutating_a_result_does_not_change_the_cache(tmp_path, fake_cc):
    def nest(text, item):
        d = parse(text, item)
        d["tags"] = ["one"]                                  # nested: a shallow copy shares it
        return d

    wf = workflow(tmp_path)
    [r] = wf.claude_check("c", [{"id": "A"}], ask, nest, model="opus", max_calls=1)
    r["data"]["seen"] = "mutated"
    r["data"]["tags"].append("two")                          # mutated after the call
    [hit] = wf.claude_check("c", [{"id": "A"}], ask, nest, model="opus", max_calls=1)
    assert hit["why"] == "cached" and hit["data"] == {"seen": "A", "tags": ["one"]}
    hit["data"]["seen"] = "mutated"
    hit["data"]["tags"].append("three")                      # mutated after the hit
    [again] = wf.claude_check("c", [{"id": "A"}], ask, nest, model="opus", max_calls=1)
    assert again["data"] == {"seen": "A", "tags": ["one"]}   # the cache is what it was
    saved = json.loads((tmp_path / "run" / "claude-c.json").read_text(encoding="utf-8"))
    assert saved["A"]["data"] == {"seen": "A", "tags": ["one"]}
    assert len(cc_calls(fake_cc)) == 1                       # both later runs hit the cache


@posix_only
def test_resume_after_unavailable_asks_again(tmp_path, fake_cc, monkeypatch):
    monkeypatch.setenv("FAKE_CC_FORCE", "auth")
    wf = workflow(tmp_path)
    assert states(wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus",
                                  max_calls=1)) == [("A", "unavailable")]
    monkeypatch.delenv("FAKE_CC_FORCE")
    wf = workflow(tmp_path)                                # a resume: a fresh Workflow
    assert states(wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus",
                                  max_calls=1)) == [("A", "ok")]
    assert len(cc_calls(fake_cc)) == 2


@posix_only
def test_key_covers_model_browser_and_staged_bytes(tmp_path, fake_cc):
    from lib.swarm_engine import staging
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "a.txt").write_text("1", encoding="utf-8")
    wf = workflow(tmp_path)
    kw = dict(model="opus", max_calls=9, stage=str(fx))
    wf.claude_check("c", [{"id": "A"}], ask, parse, **kw)
    wf.claude_check("c", [{"id": "A"}], ask, parse, **kw)                 # cached
    assert len(cc_calls(fake_cc)) == 1
    (fx / "a.txt").write_text("2", encoding="utf-8")
    # wf.stage_digest walks a folder once per Workflow (once per run), so changed fixtures
    # are seen by the next invocation -- a resume -- which is a fresh Workflow
    wf = workflow(tmp_path)
    wf.claude_check("c", [{"id": "A"}], ask, parse, **kw)                 # new bytes: asked
    wf.claude_check("c", [{"id": "A"}], ask, parse, **dict(kw, model="sonnet"))
    wf.claude_check("c", [{"id": "A"}], ask, parse, **dict(kw, model="sonnet", browser=True))
    assert len(cc_calls(fake_cc)) == 4
    assert wf.stage_digest(str(fx)) == staging.digest(str(fx))         # one hash, staging's


@posix_only
def test_cache_key_is_json_of_its_parts(tmp_path, fake_cc):
    # fields joined with "\n" gave ("a\nb", model "c") and ("a", model "b\nc") the same
    # key; it is the sha256 of the JSON of the parts now, split-ambiguous no more
    wf = workflow(tmp_path)
    wf.claude_check("c", [{"id": "X"}], lambda it: "a\nb", parse, model="c", max_calls=9)
    wf.claude_check("c", [{"id": "X"}], lambda it: "a", parse, model="b\nc", max_calls=9)
    assert len(cc_calls(fake_cc)) == 2                       # the second was asked, not hit
    saved = json.loads((tmp_path / "run" / "claude-c.json").read_text(encoding="utf-8"))
    assert saved["X"]["key"] == hashlib.sha256(
        json.dumps(["a", "b\nc", False, ""]).encode("utf-8")).hexdigest()


@posix_only
def test_each_call_is_recorded_in_calls_jsonl_run_log_events_and_totals(tmp_path, fake_cc):
    run = tmp_path / "run"
    run.mkdir()
    (run / "totals.json").write_text(json.dumps({"agents_run": 1, "tokens": 5, "seconds": 7,
                                                 "invocations": 1, "claude_calls": 2,
                                                 "claude_cost_usd": 1.5}), encoding="utf-8")
    wf = workflow(tmp_path)
    wf.claude_check("c", [{"id": "A"}, {"id": "T", "mode": "maxturns"}, {"id": "Z"}], ask,
                    parse, model="opus", max_calls=2)
    lines = [json.loads(x) for x in (run / "claude" / "calls.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [(x["name"], x["item"], x["model"], x["state"], x["cost_usd"], x["num_turns"])
            for x in lines] == [("c", "A", "opus", "ok", 0.25, 3),
                                ("c", "T", "opus", "failed", 0.25, 3)]
    assert lines[1]["why"] == "stopped at the 40-turn limit"
    assert all(isinstance(x["seconds"], float) for x in lines)
    log = (run / "run.log").read_text(encoding="utf-8").splitlines()
    assert any(x.startswith("c-A\tclaude-check\t0\t120\t") and x.endswith("\tok") for x in log)
    assert any(x.startswith("c-T\tclaude-check\t0\t120\t")
               and x.endswith("\tfailed: stopped at the 40-turn limit") for x in log)
    assert not any(x.startswith("c-Z\t") for x in log)        # over the cap: no call, no line
    evs = [json.loads(x) for x in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    calls_ev = [e for e in evs if e["kind"] == "claude_call"]
    assert [(e["name"], e["item"], e["state"], e["cost_usd"]) for e in calls_ev] == [
        ("c", "A", "ok", 0.25), ("c", "T", "failed", 0.25)]
    t = wf.totals()
    assert t["claude_calls"] == 4 and t["claude_cost_usd"] == 2.0
    assert json.loads((run / "totals.json").read_text(encoding="utf-8"))["claude_calls"] == 4


def test_totals_keep_their_keys_without_claude_calls(tmp_path):
    wf = workflow(tmp_path)
    assert sorted(wf.totals()) == ["agents_run", "invocations", "seconds", "tokens"]


def test_check_dry_run_starts_nothing_and_records_the_calls(tmp_path, monkeypatch):
    from lib.swarm_engine import check
    monkeypatch.setenv("QWEN_CLAUDE_BIN", str(tmp_path / "must-not-run"))
    monkeypatch.setenv("QWEN_EXEC_RETRY_BACKOFF", "0 0")
    folder = make_workflow(tmp_path / "wf")
    from lib.swarm_engine import manifest
    m = manifest.load(folder)
    run = tmp_path / "run"
    answers = []

    def answer(role, prompt):
        answers.append((role, prompt))
        return '{"seen": "dry"}' if "MODE=ok" in prompt else "nope"

    sw = check.FakeSwarm(run, answer)
    cfg = {"goal": "g", "depth": "quick", "items": 3, "max_agents": 1, "max_items": 1,
           "timeout_per_item": 100, "retries": 0, "rounds": 1, "target": None}
    from lib.swarm_engine import api
    wf = api.Workflow(m, cfg, run, sw, goal="g", check=check.FakeCommands(None))
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B", "mode": "prose"}], ask, parse,
                          model="opus", max_calls=5)
    assert states(res) == [("A", "ok"), ("B", "unavailable")]
    assert res[0]["data"] == {"seen": "dry"}
    assert [a[0] for a in answers] == ["claude-check", "claude-check"]
    assert [c[:3] for c in wf.calls] == [("claude", "c-A", "claude-check"),
                                         ("claude", "c-B", "claude-check")]
    assert not (run / "claude").exists() and not (run / "claude-c.json").exists()


@posix_only
def test_probe_stays_away_from_over_cap_and_deadline_items(tmp_path, fake_cc, probe_live,
                                                           monkeypatch):
    # over_cap and deadline items never trigger the probe: logged out, a probe that spoke
    # would leave an auth file and an event -- none may exist for either state
    monkeypatch.setenv("FAKE_CC_LOGGEDIN", "false")
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", "http://127.0.0.1:9/")
    wf = workflow(tmp_path)
    assert states(wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus",
                                  max_calls=0)) == [("A", "over_cap")]
    wf = workflow(tmp_path)
    wf.sw.deadline = time.time() - 1
    assert states(wf.claude_check("c2", [{"id": "A"}, {"id": "B"}], ask, parse,
                                  model="opus", max_calls=5)) == [("A", "deadline"),
                                                                  ("B", "deadline")]
    assert auth_probes(fake_cc) == []
    assert cc_calls(fake_cc) == []
    assert probe_events(tmp_path) == []


# ---------------------------------------------------------------- the availability probe

@pytest.fixture
def probe_live(monkeypatch):
    """The probe in action: conftest switches it off for every test; the probe tests
    switch it back on and own the whole env around it -- a developer's own proxy settings
    would otherwise send every probe through that proxy."""
    monkeypatch.delenv("QWEN_CLAUDE_PROBE", raising=False)
    monkeypatch.delenv("QWEN_CLAUDE_PROBE_URL", raising=False)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)


class _Gone404(http.server.BaseHTTPRequestHandler):
    """Answers every HEAD with 404, as api.anthropic.com does: an answer -- any answer --
    is reachability."""

    def do_HEAD(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):                     # the suite's stderr stays clean
        pass


@pytest.fixture
def reachable_url():
    """A local server that answers (404): the probe's HEAD reaches it in milliseconds."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gone404)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02},
                     daemon=True).start()
    try:
        yield "http://127.0.0.1:%d/" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def test_probe_off_short_circuits(fake_cc, monkeypatch):
    # QWEN_CLAUDE_PROBE=off (conftest's default, and the escape hatch for a network the
    # probe gets wrong) answers from the env alone: no spawn, no socket -- from os.environ
    assert cc.probe(dict(os.environ)) == ("unknown", "probe off", "")
    monkeypatch.delenv("QWEN_CLAUDE_PROBE")       # ... or from the env= argument alone
    assert cc.probe({"QWEN_CLAUDE_PROBE": "off"}) == ("unknown", "probe off", "")


@posix_only
def test_probe_never_raises(fake_cc, probe_live, monkeypatch):
    # a URL the probe cannot even try stays one more "unknown", not a crash
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", "not a url")
    state, why, kind = cc.probe(dict(os.environ))
    assert state == "unknown" and kind == "" and why.startswith("probe error: "), (state, why)


@posix_only
def test_probe_logged_out_makes_no_call(tmp_path, fake_cc, probe_live, monkeypatch):
    monkeypatch.setenv("FAKE_CC_LOGGEDIN", "false")
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}, {"id": "C"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable"), ("C", "unavailable")]
    why = "claude is not available: claude is not logged in (run: claude /login)"
    assert all(r["why"] == why for r in res), res
    assert cc_calls(fake_cc) == []                        # no call was made at all
    assert [(e["name"], e["state"]) for e in probe_events(tmp_path)] == [("c", "unavailable")]
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8")
    assert ("claude-check: probe says claude is not available (claude is not logged in "
            "(run: claude /login)); no calls made" in log)


@posix_only
def test_probe_unreachable_makes_no_call(tmp_path, fake_cc, probe_live, monkeypatch):
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", "http://127.0.0.1:9/")   # discard: refused
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}, {"id": "C"}], ask, parse,
                          model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable"), ("C", "unavailable")]
    assert all(r["why"].startswith("claude is not available: cannot reach 127.0.0.1:")
               for r in res), res
    assert cc_calls(fake_cc) == []                        # zero check calls, three items
    assert len(probe_events(tmp_path)) == 1               # one event for the run, not per item


@posix_only
def test_probe_missing_binary_is_unavailable(tmp_path, fake_cc, probe_live, monkeypatch):
    # a binary the probe cannot start ends nothing on the probe's word: the call goes out,
    # the retries run out with it, and the CALL's reason is what every item gets
    gone = str(tmp_path / "no-such-claude")
    monkeypatch.setenv("QWEN_CLAUDE_BIN", gone)
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", "http://127.0.0.1:9/")   # never reached
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}], ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "unavailable"), ("B", "unavailable")]
    assert all(r["why"].startswith("claude could not be executed (exit 127)") for r in res), res
    assert cc_calls(fake_cc) == []
    assert auth_probes(fake_cc) == []                      # nothing there for the probe to ask
    assert probe_events(tmp_path) == []                    # a binary makes no event
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8")
    assert "the probe could not run claude" in log
    assert log.count("claude could not be executed (exit 127); retrying in 0s") == 2


@posix_only
def test_probe_not_executable_is_unavailable(tmp_path, fake_cc, probe_live, monkeypatch):
    silent = tmp_path / "claude"                   # exists, but nothing can run it
    silent.write_text("not an executable\n", encoding="utf-8")
    silent.chmod(0o644)
    monkeypatch.setenv("QWEN_CLAUDE_BIN", str(silent))
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", "http://127.0.0.1:9/")   # never reached
    assert cc.probe(dict(os.environ)) == (
        "unavailable", "claude not found or not executable (%s)" % silent, "binary")


@posix_only
def test_probe_missing_binary_defers_to_the_retry(tmp_path, fake_cc, probe_live, monkeypatch):
    # the probe must not undo the 126/127 retry: a claude mid-auto-update is briefly not
    # found or not executable, and that is exactly what the call's own
    # QWEN_EXEC_RETRY_BACKOFF retry rides out. So a "binary" unavailable pre-trips nothing:
    # the call goes out, one retry follows, and the run is ok.
    monkeypatch.setenv("FAKE_CC_AUTH_RC", "127")        # the probe: no claude to run
    monkeypatch.setenv("FAKE_CC_GATE_RC", "127")        # and the first check call likewise
    (fake_cc / "fail126").write_text("1", encoding="utf-8")          # once, then it is back
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", "http://127.0.0.1:9/")   # never reached
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}], ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "ok"), ("B", "ok")]
    assert len(auth_probes(fake_cc)) == 1                   # probed once for the whole run
    assert len(cc_calls(fake_cc)) == 2                      # the 127 attempt records nothing
    assert probe_events(tmp_path) == []                     # no event, no breaker
    log = (tmp_path / "run" / "run.log").read_text(encoding="utf-8")
    assert log.count("claude could not be executed (exit 127); retrying in 0s") == 1
    assert "the probe could not run claude" in log


@posix_only
def test_probe_reachable_http_error_counts_as_available(tmp_path, fake_cc, probe_live,
                                                        reachable_url, monkeypatch):
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", reachable_url)
    assert cc.probe(dict(os.environ)) == ("available", "", "")   # logged in; 404 answers
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}], ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "ok"), ("B", "ok")]
    assert [c["prompt"] for c in cc_calls(fake_cc)] == ["check ITEM=A MODE=ok",
                                                        "check ITEM=B MODE=ok"]
    assert probe_events(tmp_path) == []                   # available: no event, no log line
    assert "probe says" not in (tmp_path / "run" / "run.log").read_text(encoding="utf-8")


@posix_only
def test_probe_uses_the_proxy_in_env_unless_no_proxy_bypasses_it(fake_cc, probe_live,
                                                                 reachable_url, monkeypatch):
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", reachable_url)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")   # a proxy nothing answers on
    # through it: refused -- and the kind names the ROUTE as what failed, not the binary
    state, why, kind = cc.probe(dict(os.environ), timeout=2)
    assert (state, kind) == ("unavailable", "network") and why.startswith("cannot reach ")
    monkeypatch.setenv("no_proxy", "127.0.0.1")              # bypassed: reached directly
    assert cc.probe(dict(os.environ), timeout=2) == ("available", "", "")
    # and a proxy in env is really used: the server answers for the unreachable host too
    monkeypatch.delenv("no_proxy")
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", "http://gone.invalid/")
    monkeypatch.setenv("http_proxy", reachable_url.rstrip("/"))
    assert cc.probe(dict(os.environ), timeout=2) == ("available", "", "")


@posix_only
def test_probe_undetermined_falls_back_to_the_call(tmp_path, fake_cc, probe_live,
                                                   reachable_url, monkeypatch):
    monkeypatch.setenv("FAKE_CC_AUTH", "auth status: not json")   # no object to read
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", reachable_url)
    assert cc.probe(dict(os.environ)) == ("unknown", "", "")   # login undecided, net reached
    # an object is not enough either: loggedIn must be a bool before anyone believes it
    monkeypatch.setenv("FAKE_CC_AUTH", '{"loggedIn": "yes"}')
    assert cc.probe(dict(os.environ)) == ("unknown", "", "")
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}], ask, parse, model="opus", max_calls=9)
    assert states(res) == [("A", "ok"), ("B", "ok")]      # the call decides, exactly as today
    assert len(cc_calls(fake_cc)) == 2
    assert probe_events(tmp_path) == []


@posix_only
def test_probe_runs_once_and_not_for_cached_or_dry_runs(tmp_path, fake_cc, probe_live,
                                                        monkeypatch):
    monkeypatch.setenv("FAKE_CC_LOGGEDIN", "false")
    monkeypatch.setenv("QWEN_CLAUDE_PROBE_URL", "http://127.0.0.1:9/")   # never reached
    wf = workflow(tmp_path)
    res = wf.claude_check("c", [{"id": "A"}, {"id": "B"}, {"id": "C"}], ask, parse,
                          model="opus", max_calls=9)
    assert all(r["state"] == "unavailable" for r in res)
    assert len(auth_probes(fake_cc)) == 1                 # one probe for three items
    assert len(probe_events(tmp_path)) == 1
    wf = workflow(tmp_path)                               # the next invocation probes again
    wf.claude_check("c2", [{"id": "D"}], ask, parse, model="opus", max_calls=1)
    assert len(auth_probes(fake_cc)) == 2

    # a cached item asks for nothing: A is cached from a probe-off run, and the probe
    # speaks only when B's real call comes up
    monkeypatch.setenv("QWEN_CLAUDE_PROBE", "off")
    wf = workflow(tmp_path)
    assert states(wf.claude_check("c3", [{"id": "A"}], ask, parse, model="opus",
                                  max_calls=1)) == [("A", "ok")]
    monkeypatch.delenv("QWEN_CLAUDE_PROBE")
    wf = workflow(tmp_path)
    res = wf.claude_check("c3", [{"id": "A"}, {"id": "B"}], ask, parse, model="opus", max_calls=1)
    assert states(res) == [("A", "ok"), ("B", "unavailable")]
    assert res[0]["why"] == "cached"
    assert len(auth_probes(fake_cc)) == 3                 # B probed once; cached A not at all
    assert len(cc_calls(fake_cc)) == 1                    # only the cached-earning call ever ran

    # a --check dry run never probes: the fake answers, and with the fake logged out only
    # a probe that was skipped can leave the dry answer ok
    from lib.swarm_engine import api, check, manifest
    m = manifest.load(make_workflow(tmp_path / "wf2"))
    run2 = tmp_path / "run2"
    sw = check.FakeSwarm(run2, lambda role, prompt: '{"seen": "dry"}')
    cfg = {"goal": "g", "depth": "quick", "items": 3, "max_agents": 1, "max_items": 1,
           "timeout_per_item": 100, "retries": 0, "rounds": 1, "target": None}
    wf = api.Workflow(m, cfg, run2, sw, goal="g", check=check.FakeCommands(None))
    assert states(wf.claude_check("c", [{"id": "A"}], ask, parse, model="opus",
                                  max_calls=5)) == [("A", "ok")]
    assert not (run2 / "events.jsonl").exists()           # and it wrote no probe event
