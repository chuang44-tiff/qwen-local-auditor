"""Scripted UI suites: lib/scenarios.py (parse, prompt, results, summary, its CLI)
and qwen-agent --scenarios end to end, offline.

The fake claude records every call like test_cli_deep's fake and answers with the
text in $FAKE_ANSWER, or runs one of test_cli's failure modes per $FAKE_MODES
entry, so the run's own exit code can be pinned beside the scenario verdicts.
"""
import itertools
import json
import os
import pathlib
import subprocess
import sys

import pytest

from lib import scenarios                            # via conftest's sys.path
import test_cli
from test_cli import flag, posix, run, same_path
from test_cli_browser import mcp_entry, out_dir
from test_cli_deep import calls, sys_prompt

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = ROOT / "skill" / "local-auditor" / "lib" / "scenarios.py"

server = test_cli.server                             # the model server fixture

# The reporting contract, written out -- asserted against the prompt as built, not
# against scenarios.CONTRACT, so a change to either shows up here.
SPEC_CONTRACT = (
    "Run the scenarios in order, each from a fresh page load. For each scenario "
    "follow the steps exactly; do not skip or reorder them. Then end your answer "
    "with ONE fenced json block: {\"results\": [{\"id\": ..., \"status\": "
    "\"PASS\"|\"FAIL\"|\"BLOCKED\", \"failed_expectations\": [...], \"evidence\": "
    "[\"snapshot or screenshot file and what it shows\"], \"notes\": \"...\"}]} "
    "with one entry per scenario id. BLOCKED means a step could not be performed."
)

# A suite in exactly the documented shape: named ids and defaults, base, and the
# two lists per scenario.
CART = """\
# Suite: Cart page
base: http://localhost:3000/cart

## Scenario: add an item
id: add-item
steps:
1. Open the cart page
2. Click Add on "Widget"
expect:
- The cart shows 1 item
- The subtotal is 9.00

## Scenario: remove an item
steps:
1. Click Remove on the only line
expect:
- The cart is empty
"""

# Three scenarios, ids add / s2 / s3 -- enough to tell "one missing" from "all".
UNIT = """\
# Suite: Unit
base: http://127.0.0.1:3000

## Scenario: one
id: add
steps:
1. step one
expect:
- exp one

## Scenario: two
steps:
1. step two
expect:
- exp two

## Scenario: three
steps:
1. step three
expect:
- exp three
"""

FAKE_SCEN = r'''#!/usr/bin/env bash
# Records every call (argv NUL-separated, plus the cwd) like test_cli_deep's
# fake; answers per call from $FAKE_MODES (comma list, as there): the failure
# modes are test_cli's, anything else prints the text in $FAKE_ANSWER as the
# result, JSON-escaped with nothing but sed.
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
d="$FAKE_DIR"
n=$(cat "$d/n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$d/n"
printf '%s\0' "$@" > "$d/argv.$n"
pwd -P > "$d/pwd.$n"
mode="$(printf '%s' "${FAKE_MODES:-ok}" | cut -d, -f"$n")"
[ -n "$mode" ] || mode=ok
case "$mode" in
  error)   printf '%s\n' '{"type":"result","subtype":"error_max_turns","is_error":true,"terminal_reason":"max_turns","result":"gave up"}' ;;
  apierr)  printf '%s\n' '{"type":"result","is_error":true,"api_error_status":400,"result":"API Error: 400"}' ;;
  empty)   printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"","session_id":"fake-session-1"}' ;;
  garbage) printf 'this is not json\n' ;;
  *)       content=$(cat "$FAKE_ANSWER")               # JSON-escape with bash alone:
         content=${content//\\/\\\\}                   # backslash,  " -> \"
         content=${content//\"/\\\"}
         content=${content//$'\n'/\\n}                 # real newlines -> literal \n
         printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"%s","session_id":"sess-%s","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}\n' "$content" "$n" ;;
esac
'''


@pytest.fixture
def fake_scen(tmp_path):
    p = tmp_path / "fake-scen"
    p.write_text(FAKE_SCEN, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    (tmp_path / "calls").mkdir()
    return p


def cli_run(*args):
    """scenarios.py as qwen-agent runs it: its own python, UTF-8 both ways."""
    return subprocess.run([sys.executable, posix(LIB)] + [str(a) for a in args],
                          capture_output=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONUTF8="1"))


# ------------------------------------------------------------------ parsing

def test_parse_full_example():
    suite = scenarios.parse(CART)
    assert suite["suite"] == "Cart page"
    assert suite["base"] == "http://localhost:3000/cart"
    assert [s["id"] for s in suite["scenarios"]] == ["add-item", "s2"]
    first = suite["scenarios"][0]
    assert first["title"] == "add an item"
    assert first["steps"] == ["Open the cart page", "Click Add on \"Widget\""]
    assert first["expect"] == ["The cart shows 1 item", "The subtotal is 9.00"]
    second = suite["scenarios"][1]
    assert second["title"] == "remove an item"
    assert second["steps"] == ["Click Remove on the only line"]
    assert second["expect"] == ["The cart is empty"]


def test_parse_default_ids():
    suite = scenarios.parse(UNIT)
    assert [s["id"] for s in suite["scenarios"]] == ["add", "s2", "s3"]
    plain = scenarios.parse("# Suite: S\n\n## Scenario: a\nsteps:\n1. x\nexpect:\n- y\n"
                            "\n## Scenario: b\nsteps:\n1. z\nexpect:\n- w\n")
    assert [s["id"] for s in plain["scenarios"]] == ["s1", "s2"]


@pytest.mark.parametrize("text,line,needle", [
    ("just prose, no heading at all\n", 1, "no '# Suite: <name>' heading"),
    ("# Notes\n\n# Suite: S\n", 1, "must be '# Suite: <name>'"),
    ("# Suite: S\n\nbase: http://x.test/\n", 1, "has no scenarios"),
    ("# Suite: S\n\n## Scenario: a\nexpect:\n- y\n", 3, "has no steps"),
    ("# Suite: S\n\n## Scenario: a\nsteps:\n1. x\n", 3, "has no expectations"),
    ("# Suite: S\n\n## Scenario: a\nid: not an id!\nsteps:\n1. x\nexpect:\n- y\n",
     4, "invalid scenario id"),
    ("# Suite: S\n\n## Scenario: a\nid: dup\nsteps:\n1. s\nexpect:\n- e\n"
     "\n## Scenario: b\nid: dup\nsteps:\n1. s\nexpect:\n- e\n",
     11, "duplicate scenario id 'dup'"),
    ("# Suite: S\n\n## Scenario: a\nid: s2\nsteps:\n1. s\nexpect:\n- e\n"
     "\n## Scenario: b\nsteps:\n1. s\nexpect:\n- e\n",
     4, "duplicate scenario id 's2'"),           # the default of the SECOND one taken
], ids=["no-heading", "first-heading", "no-scenarios", "no-steps", "no-expect",
        "invalid-id", "duplicate-id", "duplicate-default"])
def test_parse_errors_name_lines(text, line, needle):
    with pytest.raises(scenarios.ScenarioError) as e:
        scenarios.parse(text)
    assert str(e.value).startswith("line %d:" % line)
    assert needle in str(e.value)


# ------------------------------------------------------------------ the prompt

def test_prompt_lists_every_step_and_contract():
    text = scenarios.prompt(scenarios.parse(CART))
    flat = " ".join(text.split())
    for needle in ("Suite: Cart page", "Base URL: http://localhost:3000/cart",
                   "Scenario add-item: add an item",
                   "1. Open the cart page", "2. Click Add on \"Widget\"",
                   "- The cart shows 1 item", "- The subtotal is 9.00",
                   "Scenario s2: remove an item",
                   "1. Click Remove on the only line", "- The cart is empty"):
        assert needle in flat, needle
    assert text.endswith(SPEC_CONTRACT + "\n")     # the contract, verbatim, last


# ------------------------------------------------------------------ the results

def report(entries):
    return "I ran the suite.\n\n```json\n" + json.dumps({"results": entries}) + "\n```\n"


def test_results_last_block_missing_ids_invalid_status():
    suite = scenarios.parse(UNIT)
    answer = ("First draft:\n\n```json\n"
              + json.dumps({"results": [{"id": "add", "status": "FAIL"}]})
              + "\n```\n\nOn reflection, the LAST block is the report:\n\n```json\n"
              + json.dumps({"results": [
                    {"id": "add", "status": "PASS", "evidence": ["snap-add.md: 1 item"],
                     "notes": "all good"},
                    {"id": "no-such-id", "status": "PASS"},
                    {"id": "s2", "status": "MAYBE", "notes": "odd"}]})
              + "\n```\n")
    res = scenarios.results(answer, suite)
    assert [r["id"] for r in res] == ["add", "s2", "s3"]        # one per id, file order
    assert res[0]["status"] == "PASS"                            # the last block won
    assert res[0]["evidence"] == ["snap-add.md: 1 item"]
    assert "no-such-id" not in json.dumps(res)                   # unknown ids ignored
    assert res[1]["status"] == "BLOCKED"                         # an unknown status
    assert "invalid status" in res[1]["notes"] and "MAYBE" in res[1]["notes"]
    assert "odd" in res[1]["notes"]                              # its own note survives
    assert res[2] == {"id": "s3", "status": "BLOCKED", "notes": "no result reported"}
    # A LAST block that parses but carries nothing usable is still the report:
    # an empty results list, or entries only for ids the suite does not know,
    # replace the earlier one -- every scenario is then UNREPORTED ("no result
    # reported"), which is not the same verdict as "no result block".
    for empty_last in (json.dumps({"results": []}),
                       json.dumps({"results": [{"id": "not-in-suite", "status": "PASS"}]})):
        answer2 = ("report:\n```json\n"
                   + json.dumps({"results": [{"id": "add", "status": "PASS"}]})
                   + "\n```\nno wait:\n```json\n" + empty_last + "\n```\n")
        res2 = scenarios.results(answer2, suite)
        assert [r["id"] for r in res2] == ["add", "s2", "s3"]
        assert all(r["status"] == "BLOCKED" and r["notes"] == "no result reported"
                   for r in res2)


def test_results_no_block_all_blocked():
    suite = scenarios.parse(UNIT)
    for answer in ("I opened every page and everything looked fine.",
                   "Report:\n```json\nthis is not json at all\n```\n",
                   ""):
        res = scenarios.results(answer, suite)
        assert [r["id"] for r in res] == ["add", "s2", "s3"]
        assert all(r["status"] == "BLOCKED" and r["notes"] == "no result block"
                   for r in res)


def test_summary_table():
    suite = scenarios.parse(UNIT)
    res = scenarios.results(report([
        {"id": "add", "status": "PASS", "notes": "cart | ok\nlooks right"},
        {"id": "s2", "status": "FAIL", "failed_expectations": ["exp two"],
         "notes": "line remained"}]), suite)
    out = scenarios.summary(res)
    lines = out.splitlines()
    assert lines[0] == "| id | status | notes |"
    assert "| add | PASS | cart \\| ok looks right |" in lines   # pipes and newlines tamed
    assert "| s2 | FAIL | line remained |" in lines
    assert "| s3 | BLOCKED | no result reported |" in lines
    assert "PASS 1 / FAIL 1 / BLOCKED 1" in out


# ------------------------------------------------------------------ the module CLI

def test_cli_check_ok_and_error(tmp_path):
    good = tmp_path / "suite.md"
    good.write_text(UNIT, encoding="utf-8")
    r = cli_run("check", posix(good))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok: 3 scenarios"

    bad = tmp_path / "bad.md"
    bad.write_text("# Suite: Empty\n\nbase: http://127.0.0.1:3000\n", encoding="utf-8")
    r = cli_run("check", posix(bad))
    assert r.returncode == 2
    assert "line 1" in r.stderr and "has no scenarios" in r.stderr
    r = cli_run("check", posix(tmp_path / "absent.md"))
    assert r.returncode == 2, r.stderr

    r = cli_run("prompt", posix(good))                           # what the tester is handed
    assert r.returncode == 0, r.stderr
    assert "1. step one" in r.stdout and r.stdout.endswith(SPEC_CONTRACT + "\n")

    ans = tmp_path / "answer.md"
    ans.write_text(report([{"id": "add", "status": "PASS"}]), encoding="utf-8")
    out = tmp_path / "results.json"
    r = cli_run("results", posix(good), posix(ans), posix(out))
    assert r.returncode == 0, r.stderr
    assert "| id | status | notes |" in r.stdout
    assert "PASS 1 / FAIL 0 / BLOCKED 2" in r.stdout
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [d["id"] for d in data] == ["add", "s2", "s3"]
    assert data[0]["status"] == "PASS"


# ------------------------------------------------------------------ qwen-agent

_answer_seq = itertools.count(1)


def senv(tmp_path, answer=None, modes="ok", extra=None):
    """env for a --scenarios run: browser folders and the fake's calls under tmp_path,
    the fake's answer text in a file (it may span lines and contain quotes)."""
    (tmp_path / "outdir").mkdir(exist_ok=True)
    env = {"QWEN_BROWSER_DIR": posix(tmp_path / "browser"),
           "QWEN_OUTDIR": posix(tmp_path / "outdir"),
           "FAKE_DIR": posix(tmp_path / "calls"), "FAKE_MODES": modes}
    if answer is not None:
        f = tmp_path / ("answer-%d.txt" % next(_answer_seq))
        f.write_text(answer, encoding="utf-8")
        env["FAKE_ANSWER"] = posix(f)
    env.update(extra or {})
    return env


def run_scen(tmp_path, args, server, fake_scen, answer=None, modes="ok", extra=None):
    return run(tmp_path, args, server, fake_scen,
               extra=senv(tmp_path, answer, modes, extra))


def test_qwen_agent_scenarios_runs_tester_with_browser(tmp_path, server, fake_scen):
    suite = tmp_path / "suite.md"
    suite.write_text(UNIT, encoding="utf-8")
    answer = report([{"id": i, "status": "PASS", "notes": "seen"}
                     for i in ("add", "s2", "s3")])
    r = run_scen(tmp_path, ["--scenarios", posix(suite)], server, fake_scen, answer=answer)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(calls(tmp_path)) == 1                        # no depth here: one call
    argv, _ = calls(tmp_path)[0]
    assert "--strict-mcp-config" in argv                    # --scenarios implies --browser
    _, entry = mcp_entry(argv)                              # the one playwright server
    assert pathlib.Path(out_dir(entry)).is_dir()            # and its run folder is the evidence
    grants = flag(argv, "--allowed-tools").split(",")
    assert "mcp__playwright__browser_navigate" in grants    # the browser tools granted
    assert "BROWSER TESTER" in sys_prompt(argv)             # -r tester, with its text
    assert argv[-2] == "--"                                 # the prompt is the suite's task text
    prompt = " ".join(argv[-1].split())
    assert "Suite: Unit" in prompt
    assert "Base URL: http://127.0.0.1:3000" in prompt
    for step in ("1. step one", "1. step two", "1. step three"):
        assert step in prompt, step
    assert "with one entry per scenario id." in prompt
    assert "PASS 3 / FAIL 0 / BLOCKED 0" in r.stderr        # the summary goes to stderr
    assert r.stdout == answer.strip() + "\n"                # the answer keeps stdout


@pytest.mark.parametrize("verdict,modes,want", [
    ("all-pass", "ok", 0),
    ("one-fail", "ok", 9),
    ("one-blocked", "ok", 9),        # a reported BLOCKED, not just a missing id
    ("no-report", "ok", 9),          # no result block: all BLOCKED, exit 9
    ("all-pass", "error", 8),        # the run's own code wins over the verdict
    ("all-pass", "apierr", 4),
    ("all-pass", "empty", 6),
    ("all-pass", "garbage", 8),
])
def test_qwen_agent_scenarios_exit_codes(tmp_path, server, fake_scen, verdict, modes, want):
    suite = tmp_path / "suite.md"
    suite.write_text(UNIT, encoding="utf-8")
    answers = {
        "all-pass": report([{"id": i, "status": "PASS"} for i in ("add", "s2", "s3")]),
        "one-fail": report([{"id": "add", "status": "PASS"},
                            {"id": "s2", "status": "FAIL", "notes": "subtotal wrong"},
                            {"id": "s3", "status": "PASS"}]),
        "one-blocked": report([{"id": "add", "status": "PASS"},
                               {"id": "s2", "status": "BLOCKED", "notes": "button missing"},
                               {"id": "s3", "status": "PASS"}]),
        "no-report": "Everything passed, trust me.",
    }
    r = run_scen(tmp_path, ["--scenarios", posix(suite)], server, fake_scen,
                 answer=answers[verdict], modes=modes)
    assert r.returncode == want, r.stdout + r.stderr


def test_qwen_agent_scenarios_writes_results_and_summary(tmp_path, server, fake_scen):
    suite = tmp_path / "suite.md"
    suite.write_text(UNIT, encoding="utf-8")
    answer = report([{"id": "add", "status": "PASS", "evidence": ["snap1.md: one item"]},
                     {"id": "s2", "status": "FAIL",
                      "failed_expectations": ["exp two"], "notes": "line remained"}])
    r = run_scen(tmp_path, ["--scenarios", posix(suite), "--json"], server, fake_scen,
                 answer=answer)
    assert r.returncode == 9, r.stdout + r.stderr
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    folder = pathlib.Path(out_dir(entry))                   # beside the screenshots
    data = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    assert [d["id"] for d in data] == ["add", "s2", "s3"]   # one entry per scenario, file order
    assert data[0]["status"] == "PASS" and data[0]["evidence"] == ["snap1.md: one item"]
    assert data[1]["status"] == "FAIL"
    assert data[1]["failed_expectations"] == ["exp two"]
    assert data[2]["status"] == "BLOCKED"
    assert data[2]["notes"] == "no result reported"
    summary = (folder / "summary.md").read_text(encoding="utf-8")
    assert summary.splitlines()[0] == "| id | status | notes |"
    assert "PASS 1 / FAIL 1 / BLOCKED 1" in summary
    assert "| id | status | notes |" in r.stderr            # the table, on stderr...
    assert "PASS 1 / FAIL 1 / BLOCKED 1" in r.stderr
    assert "results: %s" % posix(folder / "results.json") in posix(r.stderr)
    # ...and the counts in the --json record; the answer still owns stdout
    rec = json.loads(r.stdout)
    scen = rec["qwen_agent"]["scenarios"]
    assert scen["file"] == posix(suite)
    assert same_path(scen["results"]) == same_path(folder / "results.json")
    assert (scen["pass"], scen["fail"], scen["blocked"]) == (1, 1, 1)
    assert "```json" in rec["result"]


def test_qwen_agent_scenarios_refusals(tmp_path, server, fake_scen):
    suite = tmp_path / "suite.md"
    suite.write_text(UNIT, encoding="utf-8")
    (tmp_path / "brief.md").write_text("do this", encoding="utf-8")
    (tmp_path / "task.md").write_text("- [ ] something -- check: true", encoding="utf-8")
    for extra_flags, needle in [
        (["-r", "auditor"], "-r"),
        (["-f", "brief.md"], "-f"),
        (["hi"], "prompt argument"),
        (["--stdin"], "--stdin"),
        (["--until-done", "task.md"], "--until-done"),
        (["--interactive"], "--interactive"),
    ]:
        r = run_scen(tmp_path, ["--scenarios", posix(suite)] + extra_flags,
                     server, fake_scen)
        assert r.returncode == 2, "%s: %s%s" % (extra_flags, r.stdout, r.stderr)
        assert "--scenarios" in r.stderr and needle in r.stderr
        assert calls(tmp_path) == []                        # claude never started
        assert not (tmp_path / "browser").exists()          # and nothing was created


def test_qwen_agent_scenarios_refuses_a_broken_suite(tmp_path, server, fake_scen):
    bad = tmp_path / "bad.md"
    bad.write_text("# Suite: Broken\n\n## Scenario: no steps here\n", encoding="utf-8")
    r = run_scen(tmp_path, ["--scenarios", posix(bad)], server, fake_scen)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "line 3" in r.stderr and "has no steps" in r.stderr
    assert calls(tmp_path) == []                            # validated BEFORE claude could run
    assert not (tmp_path / "browser").exists()              # and before any folder was made
    r = run_scen(tmp_path, ["--scenarios", posix(tmp_path / "absent.md")],
                 server, fake_scen)
    assert r.returncode == 2
    assert calls(tmp_path) == []


def test_qwen_agent_scenarios_background_status_names_the_failure(tmp_path, server, fake_scen):
    from test_cli import wait_for_status
    suite = tmp_path / "suite.md"
    suite.write_text(UNIT, encoding="utf-8")
    answer = report([{"id": "add", "status": "FAIL", "notes": "no"}])
    out = tmp_path / "bg.md"
    r = run_scen(tmp_path, ["--scenarios", posix(suite), "-w", "-o", posix(out)], server,
                 fake_scen, answer=answer)
    assert r.returncode == 0, r.stdout + r.stderr
    status = wait_for_status(pathlib.Path(str(out) + ".status"))
    assert "exit=9" in status and "reason=scenario_fail" in status
