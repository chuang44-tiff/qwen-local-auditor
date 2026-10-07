"""Record and replay for scripted UI suites, offline: the replay runner of
lib/scenarios.py (replay-check, replay) and qwen-agent's --record / --replay.

No browser and no javascript here: the "scripts" are files whose marker comments
tell a fake `node` what to print, so each scenario's RESULT line -- the whole
contract between a script and the runner -- is whatever the test needs, and the
playwright package is a directory that merely holds one named `playwright`. The
fake claude is test_cli_browser's (test_scenarios' for the suite answers, and a
small recorder wrapper over it for --record, which has to WRITE the scripts).
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from lib import scenarios                            # via conftest's sys.path
import test_cli_browser
import test_scenarios
from test_cli import flag, posix, rule_path, run, same_path
from test_cli_deep import calls, go
from test_scenarios import report, senv

# The fake node is a bash script that the runner starts directly, and Windows cannot
# exec one (nor would shutil.which pick it over a real node.exe).
pytestmark = pytest.mark.skipif(os.name != "posix", reason="the fake node is a bash script")

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = ROOT / "skill" / "local-auditor" / "lib" / "scenarios.py"

server = test_cli_browser.server                     # the model server fixture
fake = test_cli_browser.fake                         # and the fake claude that records calls
fake_scen = test_scenarios.fake_scen                 # the fake that answers from $FAKE_ANSWER

# Five scenarios, one per thing validation can decide: keep a PASS, keep a FAIL,
# refuse a BLOCKED, refuse an unreported id, and refuse the script whose replay
# answers something else than the run recorded.
FIVE = """\
# Suite: Five
base: http://127.0.0.1:3000

## Scenario: a passes
id: a
steps:
1. step a
expect:
- exp a

## Scenario: b fails
id: b
steps:
1. step b
expect:
- exp b

## Scenario: c is blocked
id: c
steps:
1. step c
expect:
- exp c

## Scenario: d never ran
id: d
steps:
1. step d
expect:
- exp d

## Scenario: e disagrees
id: e
steps:
1. step e
expect:
- exp e
"""

FAKE_NODE = r'''#!/usr/bin/env bash
# node, as far as these tests need it: it runs no javascript. What a script does is
# written in its marker comments -- FAKE: print <stdout text>, FAKE: sleep <seconds>,
# FAKE: exit <code> -- applied in the order they appear, and every call logs the
# environment the runner handed it, so a test can read NODE_PATH, QWEN_REPLAY_BASE and
# the working directory without a browser in sight.
script="${@: -1}"
if [ -n "${FAKE_NODE_ENV:-}" ]; then
  nm=no; [ -L "$(dirname "$script")/node_modules" ] && nm=link
  printf 'script=%s cwd=%s NODE_PATH=%s QWEN_REPLAY_BASE=%s node_modules=%s\n' \
    "${script##*/}" "$PWD" "${NODE_PATH:-unset}" "${QWEN_REPLAY_BASE:-unset}" "$nm" \
    >> "$FAKE_NODE_ENV"
fi
[ -f "$script" ] || exit 127
while IFS= read -r line; do
  case "$line" in
    *"FAKE: sleep "*) sleep "${line##*FAKE: sleep }" ;;
    *"FAKE: print "*) printf '%s\n' "${line##*FAKE: print }" ;;
    *"FAKE: exit "*)  exit "${line##*FAKE: exit }" ;;
  esac
done < "$script"
'''

FAKE_NPM = '''#!/usr/bin/env bash
# Only "npm config get cache" is ever asked, and the answer is a directory the test
# laid out -- so the npx-cache lookup sees what the test arranged and never a cache
# that happens to exist on the machine running the tests.
printf '%s\n' "$FAKE_NPM_CACHE"
'''

FAKE_RECORDER = r'''#!/usr/bin/env bash
# The recording session, as far as these tests need it: every call is handed to
# $FAKE_SCEN_BIN (test_scenarios' fake, which records the argv and answers from
# $FAKE_ANSWER). The one thing added: on the call whose prompt is the record prompt,
# the scripts under $FAKE_REPLAY_SRC are written into the run folder's replay/ --
# which is where that prompt says to put them, and the only place its Write rule
# reaches (--add-dir is the run folder itself).
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
_dir="" _prev=""
for a in "$@"; do
  [ "$_prev" = --add-dir ] && _dir="$a"
  _prev="$a"
done
if [ -n "$_dir" ] && command -v cygpath >/dev/null 2>&1; then _dir="$(cygpath -u "$_dir")"; fi
case "$*" in
  *"deterministic replay script"*)
    if [ -n "${FAKE_REPLAY_SRC:-}" ] && [ -n "$_dir" ]; then
      mkdir -p "$_dir/replay"
      for f in "$FAKE_REPLAY_SRC"/*.mjs; do
        [ -f "$f" ] && cp "$f" "$_dir/replay/"
      done
    fi ;;
esac
exec "$FAKE_SCEN_BIN" "$@"
'''


@pytest.fixture
def toolchain(tmp_path):
    """(bin dir with a fake node, node_modules dir holding a fake playwright): what the
    runner resolves its toolchain from, so no real node or playwright is involved."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    node = bindir / "node"
    node.write_text(FAKE_NODE, encoding="utf-8", newline="\n")
    node.chmod(0o755)
    npm = bindir / "npm"
    npm.write_text(FAKE_NPM, encoding="utf-8", newline="\n")
    npm.chmod(0o755)
    (tmp_path / "npmcache").mkdir()
    nm = tmp_path / "node_modules"
    (nm / "playwright").mkdir(parents=True)
    return bindir, nm


@pytest.fixture
def recorder(tmp_path, fake_scen):
    """The fake claude of the recording run, and the directory the test's scripts wait
    in for the record round to copy them out of."""
    w = tmp_path / "fake-recorder"
    w.write_text(FAKE_RECORDER, encoding="utf-8", newline="\n")
    w.chmod(0o755)
    (tmp_path / "replay-src").mkdir(exist_ok=True)
    return w


def renv(tmp_path, toolchain, extra=None):
    """The environment a replay needs: the fake node on PATH, the fake playwright
    pinned, and the log file every node call appends its environment to."""
    bindir, nm = toolchain
    env = {"PATH": posix(bindir) + os.pathsep + os.environ["PATH"],
           "QWEN_PLAYWRIGHT_NODE_PATH": posix(nm),
           "FAKE_NODE_ENV": posix(tmp_path / "node-env.log"),
           "FAKE_NPM_CACHE": posix(tmp_path / "npmcache")}
    env.update(extra or {})
    return env


def pin(monkeypatch, tmp_path, toolchain):
    """The same environment in this process, for a call into scenarios.py directly."""
    for k, v in renv(tmp_path, toolchain).items():
        monkeypatch.setenv(k, v)


def cli_run(*args, **kw):
    """scenarios.py as qwen-agent runs it: its own python, UTF-8 both ways."""
    env = dict(os.environ, PYTHONUTF8="1")
    env.update(kw.pop("env", None) or {})
    assert not kw
    return subprocess.run([sys.executable, posix(LIB)] + [str(a) for a in args],
                          capture_output=True, encoding="utf-8", errors="replace",
                          env=env)


def node_log(tmp_path):
    p = tmp_path / "node-env.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def script(sid, verdict="PASS", note="", sleep=None, code=0):
    """One replay script, as the recorded session would have written it. A real one
    drives playwright; this one carries the markers FAKE_NODE acts on, so the runner
    sees exactly what a real script's stdout and exit code would have shown."""
    lines = ["// replay: %s" % sid,
             "import { chromium } from 'playwright';"]
    if sleep is not None:
        lines.append("// FAKE: sleep %s" % sleep)
    if verdict == "PASS":
        lines.append("// FAKE: print RESULT %s PASS" % sid)
    elif verdict == "FAIL":
        lines.append("// FAKE: print RESULT %s FAIL: %s" % (sid, note))
    if code:
        lines.append("// FAKE: exit %d" % code)
    return "\n".join(lines) + "\n"


def recording(tmp_path, name, entries):
    """A recording directory exactly as replay-check leaves it: the kept scripts and a
    manifest naming each one's verdict and bytes. ENTRIES is [(id, verdict, body)]."""
    rec = tmp_path / name
    rec.mkdir()
    scripts = []
    for sid, verdict, body in entries:
        f = rec / ("%s.mjs" % sid)
        f.write_text(body, encoding="utf-8", newline="\n")
        scripts.append({"id": sid, "verdict": verdict,
                        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        "file": "%s.mjs" % sid})
    manifest = {"suite": "Unit", "file": posix(tmp_path / "suite.md"),
                "base": "http://127.0.0.1:3000", "recorded": "2026-01-01T00:00:00Z",
                "scripts": scripts, "rejected": []}
    (rec / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                       encoding="utf-8")
    return rec


# ------------------------------------------------------------------ recording

def test_record_resumes_once_with_record_prompt(tmp_path, server, recorder, fake_scen,
                                                toolchain):
    suite = tmp_path / "suite.md"
    suite.write_text(test_scenarios.UNIT, encoding="utf-8")
    ids = ("add", "s2", "s3")
    for sid in ids:                                  # what the session is asked to write
        (tmp_path / "replay-src" / ("%s.mjs" % sid)).write_text(script(sid),
                                                                encoding="utf-8")
    rec = tmp_path / "recording"
    answer = report([{"id": i, "status": "PASS"} for i in ids])
    extra = dict(renv(tmp_path, toolchain), FAKE_SCEN_BIN=posix(fake_scen),
                 FAKE_REPLAY_SRC=posix(tmp_path / "replay-src"))
    r = run(tmp_path, ["--scenarios", posix(suite), "--record", posix(rec)],
            server, recorder, extra=senv(tmp_path, answer=answer, extra=extra))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == answer.strip() + "\n"         # the tester's report still owns stdout

    called = calls(tmp_path)
    assert len(called) == 2, "--record resumes the session exactly once"
    first, second = [a for a, _ in called]
    assert "--resume" not in first
    folder = pathlib.Path(flag(first, "--add-dir"))
    assert folder.is_dir()

    # the second call: the same session, the fixed prompt, and nothing else asked
    assert second.count("--resume") == 1 and second.count("--allowed-tools") == 1
    assert second[second.index("--resume") + 1] == "sess-1"
    assert second[-2] == "--"
    prompt = second[-1]
    assert "@FOLDER@" not in prompt
    for needle in ("deterministic replay script",
                   "%s/replay/<id>.mjs" % posix(folder),
                   "imports { chromium } from 'playwright'", "headless",
                   "never coordinates", "EVERY expectation",
                   "RESULT <id> PASS", "RESULT <id> FAIL: <the expectation that did not hold>",
                   "QWEN_REPLAY_BASE", "Handle dialogs explicitly",
                   "Do not change anything else.", "list of files written"):
        assert needle in prompt, needle

    # the write fence of that one call: Write inside the run's replay folder, nowhere
    # else -- and the Write tool only exists in this call's toolset at all.
    # Claude Code matches file writes through Edit(...) path rules, so both are granted
    for tool in ("Edit", "Write"):
        rule = "%s(%s/**)" % (tool, rule_path(folder / "replay"))
        assert rule in flag(second, "--allowed-tools").split(",")
        assert tool + "(" not in flag(first, "--allowed-tools")
    assert "Write" in flag(second, "--tools").split(",")
    assert "Write" not in flag(first, "--tools").split(",")

    # every script reproduced the run's verdict, so every one was kept
    kept = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
    assert list(kept) == ["suite", "file", "base", "recorded", "scripts", "rejected"]
    assert kept["suite"] == "Unit" and kept["base"] == "http://127.0.0.1:3000"
    assert same_path(kept["file"]) == same_path(suite)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", kept["recorded"])
    assert [s["id"] for s in kept["scripts"]] == list(ids)
    assert [s["verdict"] for s in kept["scripts"]] == ["PASS"] * 3
    assert kept["rejected"] == []
    for s in kept["scripts"]:
        body = (rec / s["file"]).read_bytes()
        assert s["file"] == "%s.mjs" % s["id"]
        assert s["sha256"] == hashlib.sha256(body).hexdigest()
        assert body == (tmp_path / "replay-src" / s["file"]).read_bytes()
    out = posix(r.stderr)
    assert "recorded: 3 of 3 scenarios (rejected: none)" in out
    assert posix(rec / "manifest.json") in out


# ------------------------------------------------------------------ validation

def test_replay_check_keeps_matching_and_rejects_mismatch(tmp_path, toolchain):
    suite = tmp_path / "suite.md"
    suite.write_text(FIVE, encoding="utf-8")
    results = tmp_path / "results.json"
    results.write_text(json.dumps([
        {"id": "a", "status": "PASS"},
        {"id": "b", "status": "FAIL", "notes": "exp b"},
        {"id": "c", "status": "BLOCKED", "notes": "the button never appeared"},
        {"id": "e", "status": "PASS"},                      # d was never reported
    ]), encoding="utf-8")
    src = tmp_path / "replay"
    src.mkdir()
    for sid, body in [("a", script("a")),
                      ("b", script("b", "FAIL", "exp b did not hold")),
                      ("c", script("c")),                   # BLOCKED: never even run
                      ("d", script("d")),
                      ("e", script("e", "FAIL", "the subtotal is 9.00"))]:
        (src / ("%s.mjs" % sid)).write_text(body, encoding="utf-8", newline="\n")
    out_dir = tmp_path / "recording"
    r = cli_run("replay-check", posix(suite), posix(results), posix(src), posix(out_dir),
                env=renv(tmp_path, toolchain))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "recorded: 2 of 5 scenarios (rejected: c, d, e)" in r.stdout
    assert posix(out_dir / "manifest.json") in r.stdout

    kept = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert kept["suite"] == "Five" and kept["base"] == "http://127.0.0.1:3000"
    assert same_path(kept["file"]) == same_path(suite)
    # kept: the PASS that replays PASS and the FAIL that replays FAIL -- a scenario
    # that failed and reproduces is exactly what a later run must be able to retake
    assert [(s["id"], s["verdict"]) for s in kept["scripts"]] == [("a", "PASS"),
                                                                 ("b", "FAIL")]
    assert [(s["id"], s["reason"]) for s in kept["rejected"]] == [
        ("c", "recorded BLOCKED"),
        ("d", "not run (no result recorded)"),
        ("e", "replay said FAIL, the run recorded PASS: the subtotal is 9.00")]
    assert sorted(p.name for p in out_dir.iterdir()) == ["a.mjs", "b.mjs",
                                                         "manifest.json"]
    for s in kept["scripts"]:
        assert s["sha256"] == hashlib.sha256((out_dir / s["file"]).read_bytes()).hexdigest()
        assert (out_dir / s["file"]).read_bytes() == (src / s["file"]).read_bytes()
    # validation asks the scripts the question a later replay asks: no base override,
    # so a kept script passes because of what it does, not because of what it was told
    log = node_log(tmp_path)
    assert sorted(ln.split()[0] for ln in log) == ["script=a.mjs", "script=b.mjs",
                                                   "script=e.mjs"]
    assert all("QWEN_REPLAY_BASE=unset" in ln for ln in log)

    # a results file that is not the run's list of results is refused, not silently
    # read as "nothing was recorded"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"results": []}), encoding="utf-8")
    r = cli_run("replay-check", posix(suite), posix(bad), posix(src),
                posix(tmp_path / "out2"), env=renv(tmp_path, toolchain))
    assert r.returncode == 2
    assert "not a list" in r.stderr


# ------------------------------------------------------------------ replaying

def test_replay_all_pass_exit_0(tmp_path, toolchain):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a")),
                                      ("b", "PASS", script("b"))])
    out = tmp_path / "results.json"
    r = cli_run("replay", posix(rec), "--out", posix(out), env=renv(tmp_path, toolchain))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "| id | status | notes |"
    assert lines[1] == "|---|---|---|"
    assert "| a | PASS |  |" in lines and "| b | PASS |  |" in lines
    assert lines[-1] == "PASS 2 / FAIL 0 / ERROR 0"
    assert json.loads(out.read_text(encoding="utf-8")) == [
        {"id": "a", "status": "PASS", "notes": ""},
        {"id": "b", "status": "PASS", "notes": ""}]
    # the same recording answers the same way twice: that is the point of it
    again = cli_run("replay", posix(rec), env=renv(tmp_path, toolchain))
    assert again.returncode == 0 and again.stdout == r.stdout


def test_replay_fail_exit_9(tmp_path, toolchain):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a")),
                                      ("b", "FAIL", script("b", "FAIL",
                                                           "the subtotal is 9.00"))])
    out = tmp_path / "results.json"
    r = cli_run("replay", posix(rec), "--out", posix(out), env=renv(tmp_path, toolchain))
    assert r.returncode == 9, r.stdout + r.stderr          # as --scenarios exits
    assert "| b | FAIL | the subtotal is 9.00 |" in r.stdout   # the expectation, in the table
    assert r.stdout.splitlines()[-1] == "PASS 1 / FAIL 1 / ERROR 0"
    assert json.loads(out.read_text(encoding="utf-8"))[1] == {
        "id": "b", "status": "FAIL", "notes": "the subtotal is 9.00"}


def test_replay_script_changed_is_error(tmp_path, toolchain):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a")),
                                      ("b", "PASS", script("b"))])
    (rec / "b.mjs").write_text(script("b") + "// edited after the recording\n",
                                      encoding="utf-8", newline="\n")
    r = cli_run("replay", posix(rec), env=renv(tmp_path, toolchain))
    assert r.returncode == 9
    assert "| b | ERROR | script changed since recording |" in r.stdout
    assert "| a | PASS |  |" in r.stdout                   # the untouched one still ran
    assert r.stdout.splitlines()[-1] == "PASS 1 / FAIL 0 / ERROR 1"
    assert "script=b.mjs" not in "\n".join(node_log(tmp_path))   # and it never ran


def test_replay_script_crash_and_timeout_are_error(tmp_path, toolchain, monkeypatch):
    pin(monkeypatch, tmp_path, toolchain)
    rec = recording(tmp_path, "rec", [
        ("crash", "PASS", script("crash", verdict=None, code=3)),   # died, said nothing
        ("slow", "PASS", script("slow", sleep=30)),                 # never finishes
    ])
    res = scenarios.replay(posix(rec), timeout=0.2)
    assert [r["status"] for r in res] == ["ERROR", "ERROR"]
    assert res[0]["notes"] == "no RESULT line (exit 3)"
    assert res[1]["notes"] == "timed out after 0.2s"       # the sleep was stopped, not waited out


def test_replay_never_calls_claude_or_preflight(tmp_path, toolchain, fake):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a"))])
    extra = dict(renv(tmp_path, toolchain),
                 QWEN_BASE_URL="http://127.0.0.1:1",       # nothing listens: preflight would exit 3
                 QWEN_BROWSER_DIR=posix(tmp_path / "browser"))
    r = go(tmp_path, ["--replay", posix(rec)], None, fake, extra=extra)
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls(tmp_path) == []                           # claude was never started
    assert not (tmp_path / "browser").exists()             # no run folder, no browser
    assert "| a | PASS |  |" in posix(r.stderr)            # the verdicts still arrive
    assert "PASS 1 / FAIL 0 / ERROR 0" in posix(r.stderr)


@pytest.mark.parametrize("bad", [
    ["-r", "tester"], ["-f", "brief.md"], ["hi"], ["--stdin"], ["-s", "extra"],
    ["--scenarios", "suite.md"], ["--until-done", "task.md"], ["--interactive"],
    ["--deep"], ["-m", "other-model"], ["--timeout", "60"], ["--browser"],
    ["--review-round"], ["--subagents"], ["--json"], ["-o", "answer.md"], ["-w"],
])
def test_replay_refuses_model_flags(tmp_path, toolchain, fake, bad):
    # A replay runs no model, so every flag that would steer one is refused where the
    # message can name it rather than quietly dropped.
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a"))])
    r = go(tmp_path, ["--replay", posix(rec)] + bad, None, fake,
           extra=renv(tmp_path, toolchain))
    assert r.returncode == 2, "%s: %s%s" % (bad, r.stdout, r.stderr)
    assert "--replay cannot be combined with" in r.stderr
    assert bad[0].split("=")[0] in r.stderr or "prompt argument" in r.stderr
    assert calls(tmp_path) == []                           # and nothing was asked
    assert node_log(tmp_path) == []                        # nor was any script run


def test_replay_needs_node_and_playwright(tmp_path, toolchain, fake):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a"))])
    bindir = tmp_path / "empty-bin"
    bindir.mkdir()

    # no node at all: the runner says what to install, and runs nothing
    r = cli_run("replay", posix(rec),
                env={"PATH": posix(bindir), "FAKE_NODE_ENV": posix(tmp_path / "none.log")})
    assert r.returncode == 2
    assert "replay needs node on PATH" in r.stderr
    assert not (tmp_path / "none.log").exists()

    # node, but no playwright: not in the pinned directory, not in the npx cache --
    # the refusal names both fixes, and no script is skipped over in silence
    cache = tmp_path / "npmcache" / "_npx" / "abc123" / "node_modules"
    (cache / "playwright-mcp").mkdir(parents=True)          # a near miss is not a hit
    empty = tmp_path / "node_modules-empty"
    empty.mkdir()
    r = cli_run("replay", posix(rec),
                env=renv(tmp_path, (toolchain[0], empty),
                         {"QWEN_PLAYWRIGHT_NODE_PATH": posix(empty)}))
    assert r.returncode == 2, r.stderr
    assert "playwright" in r.stderr and "QWEN_PLAYWRIGHT_NODE_PATH" in r.stderr
    assert "npm i -g playwright && npx playwright install chromium" in r.stderr
    assert node_log(tmp_path) == []

    # the same refusal reaches a --replay caller of qwen-agent, not just scenarios.py
    r = go(tmp_path, ["--replay", posix(rec)], None, fake,
           extra=renv(tmp_path, (toolchain[0], empty),
                      {"QWEN_PLAYWRIGHT_NODE_PATH": posix(empty)}))
    assert r.returncode == 2
    assert "npm i -g playwright && npx playwright install chromium" in posix(r.stderr)


def test_replay_base_override(tmp_path, toolchain, fake):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a"))])
    env = renv(tmp_path, toolchain)

    # unasked, a script keeps the base URL it was recorded against
    r = cli_run("replay", posix(rec), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "QWEN_REPLAY_BASE=unset" in node_log(tmp_path)[-1]

    base = "http://127.0.0.1:4000/app"
    r = cli_run("replay", posix(rec), "--base", base, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    line = node_log(tmp_path)[-1]
    assert "QWEN_REPLAY_BASE=%s" % base in line
    assert "NODE_PATH=%s" % posix(toolchain[1]) in line    # the pinned package, too

    # run in a directory of its own, and cleaned up afterwards
    cwd = re.search(r"cwd=(\S+)", line).group(1)
    assert "qwen-replay-" in cwd and not os.path.isdir(cwd)

    # and the same through qwen-agent's own --base
    r = go(tmp_path, ["--replay", posix(rec), "-b", base], None, fake,
           extra=renv(tmp_path, toolchain))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "QWEN_REPLAY_BASE=%s" % base in node_log(tmp_path)[-1]


def test_replay_script_runs_beside_a_node_modules_link(tmp_path):
    # Replay scripts are ES modules: `import 'playwright'` resolves from the script's own
    # folder upward and ignores NODE_PATH, so the runner must run a copy of the script in
    # a folder whose node_modules links to the resolved Playwright.
    # (scenarios is imported at module level, from lib)
    nm = tmp_path / "pw" / "node_modules"
    (nm / "playwright").mkdir(parents=True)
    bindir = tmp_path / "bin"; bindir.mkdir()
    node = bindir / "node"
    node.write_text(FAKE_NODE, encoding="utf-8", newline="\n"); node.chmod(0o755)
    log = tmp_path / "node-env.log"
    script = tmp_path / "s1.mjs"
    script.write_text("// FAKE: print RESULT s1 PASS\n", encoding="utf-8")
    os.environ["FAKE_NODE_ENV"] = str(log)
    try:
        status, _ = scenarios._run_script(str(node), str(nm), str(script))
    finally:
        os.environ.pop("FAKE_NODE_ENV", None)
    assert status == "PASS"
    line = log.read_text(encoding="utf-8")
    assert "node_modules=link" in line and "script=s1.mjs" in line


# ------------------------------------------------------------------ what a replay never trusts

def test_replay_of_a_recording_with_no_scripts_fails(tmp_path, toolchain):
    # A recording that kept nothing proves nothing: it must not exit 0, and the
    # scenarios it could not record are named, not left out of the table.
    rec = recording(tmp_path, "rec", [])
    m = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
    m["rejected"] = [{"id": "c", "reason": "recorded BLOCKED"},
                     {"id": "d", "reason": "no script was written"}]
    (rec / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    r = cli_run("replay", posix(rec), env=renv(tmp_path, toolchain))
    assert r.returncode == 9, r.stdout + r.stderr
    assert "| c | NOT RECORDED | recorded BLOCKED |" in r.stdout
    assert "| d | NOT RECORDED | no script was written |" in r.stdout
    assert "kept no scripts" in r.stderr


def test_replay_lists_unrecorded_scenarios_without_failing_the_kept_ones(tmp_path, toolchain):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a"))])
    m = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
    m["rejected"] = [{"id": "c", "reason": "recorded BLOCKED"}]
    (rec / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    r = cli_run("replay", posix(rec), env=renv(tmp_path, toolchain))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "| a | PASS |  |" in r.stdout
    assert "| c | NOT RECORDED | recorded BLOCKED |" in r.stdout
    assert r.stdout.splitlines()[-1] == "PASS 1 / FAIL 0 / ERROR 0 / NOT RECORDED 1"


def test_replay_never_runs_a_file_outside_the_recording(tmp_path, toolchain):
    outside = tmp_path / "x.mjs"
    outside.write_text(script("a"), encoding="utf-8", newline="\n")
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a"))])
    m = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
    m["scripts"][0]["file"] = "../x.mjs"                  # same bytes, same sha
    (rec / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    r = cli_run("replay", posix(rec), env=renv(tmp_path, toolchain))
    assert r.returncode == 9, r.stdout + r.stderr
    assert "| a | ERROR |" in r.stdout
    assert node_log(tmp_path) == []


def test_replay_result_line_must_name_its_scenario(tmp_path, toolchain):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("other"))])
    r = cli_run("replay", posix(rec), env=renv(tmp_path, toolchain))
    assert r.returncode == 9, r.stdout + r.stderr
    assert "| a | ERROR | no RESULT line" in r.stdout


def test_replay_without_a_node_modules_link_is_refused(tmp_path, toolchain, monkeypatch):
    # Without the link every `import 'playwright'` fails; that is the environment,
    # not five scenarios that each broke, so the replay stops with the reason.
    pin(monkeypatch, tmp_path, toolchain)
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a"))])

    def no_symlink(*a, **k):
        raise OSError("symbolic link privilege not held")
    monkeypatch.setattr(scenarios.os, "symlink", no_symlink)
    with pytest.raises(scenarios.ReplayError) as e:
        scenarios.replay(posix(rec))
    assert "node_modules" in str(e.value) and "privilege" in str(e.value)


def test_qwen_agent_replay_of_an_empty_recording_exits_9(tmp_path, toolchain, fake):
    rec = recording(tmp_path, "rec", [])
    r = go(tmp_path, ["--replay", posix(rec)], None, fake, extra=renv(tmp_path, toolchain))
    assert r.returncode == 9, r.stdout + r.stderr
    assert "kept no scripts" in r.stderr


def test_replay_ignores_an_invalid_qwen_depth(tmp_path, toolchain, fake):
    rec = recording(tmp_path, "rec", [("a", "PASS", script("a"))])
    r = go(tmp_path, ["--replay", posix(rec)], None, fake,
           extra=dict(renv(tmp_path, toolchain), QWEN_DEPTH="wide"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_replay_check_refuses_to_record_into_its_own_source(tmp_path, toolchain):
    suite = tmp_path / "suite.md"
    suite.write_text(FIVE, encoding="utf-8")
    results = tmp_path / "results.json"
    results.write_text(json.dumps([{"id": "a", "status": "PASS"}]), encoding="utf-8")
    src = tmp_path / "replay"
    src.mkdir()
    (src / "a.mjs").write_text(script("a"), encoding="utf-8", newline="\n")
    r = cli_run("replay-check", posix(suite), posix(results), posix(src), posix(src),
                env=renv(tmp_path, toolchain))
    assert r.returncode == 2 and "record into another directory" in r.stderr
