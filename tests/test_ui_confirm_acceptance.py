"""The ui-confirm acceptance page and suite (tests/fixtures/ui-confirm/), fake-agent half.

The page reproduces the three ways a real ui-test run's testers reported failures that were
not there -- a canvas whose change on "Gain +" is a pixel or two, a shape that closes on a
double-click rather than on its first corner, and console errors (a 404 and a deprecation
notice) on a page that raises no uncaught error -- plus a file upload that needs a fixture and
one expectation that is wrong on purpose (one "Add item" click counts 2). The page exposes
`window.appState` so a confirmer can probe instead of squinting at screenshots.

The real-model half (a local Qwen tester, a real Claude confirmer, a Claude Code session
watching the events) is run by hand and recorded in the pull request. What CI proves here is
that the suite the hand run uses parses, that the page is one static file that reaches no
network, that its fixture reaches every tester as a native absolute path staged in the unit's
own folder, and that the confirm pass, given a confirmer that answers as the real one should,
counts only the broken expectation.
"""
import json
import os
import pathlib
import re
import sys

import test_swarm_runner
from lib import scenarios
from lib.swarm_engine import runner
from swarm_fixtures import agent_args

fake = test_swarm_runner.fake

PAGE = pathlib.Path(__file__).resolve().parent / "fixtures" / "ui-confirm"
SUITE = PAGE / "suite.md"
FIXTURE = PAGE / "fixtures" / "sample-notes.txt"
IDS = ["gain-canvas", "polygon-close", "console-clean", "upload-notes", "add-item"]
PATTERNS = ["gain-canvas", "polygon-close", "console-clean"]   # hold, though easy to misjudge
BROKEN = "add-item"                                            # the one real failure

# a tester that FAILs the ids FAKE_UI_FAIL names and PASSes the rest
TESTER = r'''
import json, os, re

def answer(prompt, resumed):
    ids = re.findall(r"^Scenario (\S+):", prompt, re.M)
    fail = os.environ.get("FAKE_UI_FAIL", "").split(",")
    out = [{"id": sid, "status": "FAIL" if sid in fail else "PASS",
            "failed_expectations": ["it did not hold"] if sid in fail else [],
            "evidence": ["shot.png"], "notes": ""} for sid in ids]
    return 0, "I ran the scenario.\n\n```json\n" + json.dumps({"results": out}) + "\n```"
'''

# a stand-in for `claude -p ... --output-format json`: finds the one scenario id its prompt
# names (argv first, stdin only when argv has none), answers FALSE_ALARM for the three
# patterns and CONFIRMED for the broken one, in claude's own result envelope, and logs argv
FAKE_CLAUDE = r'''
import json, os, sys

IDS = ["gain-canvas", "polygon-close", "console-clean", "upload-notes", "add-item"]
argv = sys.argv[1:]
if "--version" in argv:
    print("2.1.294 (Claude Code)")
    sys.exit(0)
text = " ".join(argv)
found = [i for i in IDS if i in text]
if not found and not sys.stdin.isatty():
    text += sys.stdin.read()
    found = [i for i in IDS if i in text]
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
sid = found[0] if found else "unknown"
verdict = "CONFIRMED" if sid == "add-item" else "FALSE_ALARM"
block = {"id": sid, "verdict": verdict,
         "evidence": ["probe: window.appState read after the steps"]}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "Checked.\n\n```json\n" + json.dumps(block) + "\n```",
                  "total_cost_usd": 0.01, "num_turns": 3, "session_id": "fake-claude"}))
'''


def _fake_claude(tmp_path):
    """An executable `claude` that runs FAKE_CLAUDE with this interpreter: a shell script on
    POSIX, a .cmd on Windows (which shutil.which and CreateProcess both run)."""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "claude.py").write_text(FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":
        exe = d / "claude.cmd"
        exe.write_text('@"%s" "%%~dp0claude.py" %%*\r\n' % sys.executable, encoding="utf-8")
    else:
        exe = d / "claude"
        exe.write_text("#!/bin/sh\nexec '%s' \"$(dirname \"$0\")/claude.py\" \"$@\"\n"
                       % sys.executable, encoding="utf-8")
        exe.chmod(0o755)
    return exe


def _run(fake, out, *extra):
    (fake / "tester.py").write_text(TESTER, encoding="utf-8")
    return runner.main(agent_args() + ["ui-test", "ui-confirm acceptance",
                                       "--set", "scenarios=" + str(SUITE),
                                       "--out", str(out)] + list(extra))


def test_acceptance_suite_parses():
    text = SUITE.read_text(encoding="utf-8")
    suite = scenarios.parse(text)
    assert [sc["id"] for sc in suite["scenarios"]] == IDS
    assert suite["fixtures"] == "fixtures"                       # raw, resolved by ui-test
    assert suite["base"] == "http://127.0.0.1:8765/"             # the hand run passes --set base=
    # the one fixture, and the size the upload expectation quotes (.gitattributes keeps LF)
    assert sorted(p.name for p in FIXTURE.parent.iterdir()) == ["sample-notes.txt"]
    assert FIXTURE.stat().st_size == 113
    assert '"Uploaded: sample-notes.txt (113 bytes)"' in text
    assert '"Items: 1"' in text                                  # the deliberately wrong one
    assert "python -m http.server 8765" in " ".join(text.split())   # how to serve the page


def test_acceptance_page_is_static_and_self_contained():
    html = (PAGE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//""", html)  # nothing remote
    assert "<script src" not in html and "import(" not in html
    assert "window.appState = state" in html
    for probe in ("canvasHash", "polygon", "closed", "uncaughtErrors", "upload", "items"):
        assert probe in html, probe
    # the 404 is real: the page fetches a file this folder does not have
    assert 'fetch("missing-data.json")' in html and not (PAGE / "missing-data.json").exists()
    assert html.count("console.error(") == 3 and "throw " not in html
    for el in ('id="gain-up"', 'id="stage"', 'id="upload" type="file"', 'id="add-item"'):
        assert el in html, el


def test_upload_prompt_carries_native_absolute_fixture_paths(tmp_path, fake):
    out = tmp_path / "run"
    assert _run(fake, out, "--set", "confirm=none") == 0
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == IDS and all(r["status"] == "PASS" for r in rows)
    for r in rows:
        prompt = (out / "agents" / (r["unit"] + ".prompt.md")).read_text(encoding="utf-8")
        lines = prompt.splitlines()
        assert "Files for uploads:" in lines
        assert "Pass that absolute path to browser_file_upload." in lines
        m = re.search(r"sample-notes\.txt at (.+?sample-notes\.txt)", prompt)
        assert m, prompt
        named = m.group(1)
        assert os.path.isabs(named)
        assert "- sample-notes.txt at %s" % named in lines
        if os.name == "nt":                                      # native, not /c/... or /tmp/...
            assert re.match(r"^[A-Za-z]:\\", named), named
        staged = out / "agents" / r["unit"] / "fixtures" / "sample-notes.txt"
        assert os.path.samefile(named, str(staged))              # this unit's own copy
        assert staged.read_bytes() == FIXTURE.read_bytes()


def _events(out):
    text = (out / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(x) for x in text.splitlines() if x.endswith("}")]


def test_confirm_counts_only_the_broken_expectation(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_UI_FAIL", ",".join(PATTERNS + [BROKEN]))
    monkeypatch.setenv("QWEN_CLAUDE_BIN", str(_fake_claude(tmp_path)))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude-calls.jsonl"))
    monkeypatch.setenv("QWEN_EXEC_RETRY_BACKOFF", "0 0")
    out = tmp_path / "run"
    assert _run(fake, out) == 4                                  # defaults: confirm=claude
    rows = {r["id"]: r for r in json.loads((out / "results.json").read_text(encoding="utf-8"))}
    assert rows["upload-notes"]["status"] == "PASS"
    assert "confirmation" not in rows["upload-notes"]            # a PASS is never re-checked
    for sid in PATTERNS:
        assert rows[sid]["status"] == "FAIL"                     # the tester's word is kept
        assert rows[sid]["confirmation"]["verdict"] == "FALSE_ALARM"
        assert rows[sid]["confirmation"]["evidence"]
        assert rows[sid]["final"] == {"verdict": "FALSE_ALARM", "by": "claude:opus"}
    assert rows[BROKEN]["final"] == {"verdict": "CONFIRMED", "by": "claude:opus"}
    counted = [sid for sid in IDS if rows[sid]["status"] != "PASS"
               and rows[sid]["final"]["verdict"] != "FALSE_ALARM"]
    assert counted == [BROKEN]                                   # the exit counts only that one
    logged = [json.loads(x) for x in
              (tmp_path / "claude-calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(logged) == 4                                      # one call per non-PASS row
    assert all(a[a.index("--model") + 1] == "opus" for a in logged)
    report = (out / "report.md").read_text(encoding="utf-8")
    false_alarms = report.split("## False alarms", 1)[1].split("\n## ", 1)[0]
    for sid in PATTERNS:
        assert sid in false_alarms, sid
    assert BROKEN not in false_alarms
    err = capsys.readouterr().err                                # the run-start lines
    assert "run folder: %s" % out in err
    assert "confirm=claude: for each FAIL/BLOCKED scenario (up to 10)" in err
    # the event stream: tester scores, the confirmer's verdicts, and why a session should look
    ev = _events(out)
    assert [e["id"] for e in ev if e["kind"] == "scored"] == IDS
    verdicts = {e["id"]: e for e in ev if e["kind"] == "verdict"}
    assert sorted(verdicts) == sorted(PATTERNS + [BROKEN])
    assert verdicts[BROKEN]["verdict"] == "CONFIRMED" and verdicts[BROKEN]["by"] == "claude:opus"
    assert {e["item"]: e["reason"] for e in ev if e["kind"] == "attention"} == dict(
        {sid: "false alarm on a tester FAIL" for sid in PATTERNS}, **{BROKEN: "confirmed failure"})
    assert [e["exit"] for e in ev if e["kind"] == "run_end"] == [4]
    # the claude call records: one per row checked, named after the confirm pass
    calls = [json.loads(x) for x in (out / "claude" / "calls.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert sorted(c["item"] for c in calls) == sorted(PATTERNS + [BROKEN])
    assert all(c["state"] == "ok" and c["model"] == "opus" and c["name"] for c in calls)
