"""The qwen-swarm wrapper: everything the bash side owns, offline (fake agent)."""
import os
import pathlib
import re
import shutil
import subprocess
import sys

from swarm_fixtures import calls, make_workflow

ROOT = pathlib.Path(__file__).resolve().parents[1]
WRAP = ROOT / "skill" / "local-auditor" / "qwen-swarm.sh"
DR_WRAP = ROOT / "skill" / "local-auditor" / "qwen-deep-research.sh"
AGENT = ROOT / "skill" / "local-auditor" / "qwen-agent.sh"
FAKE = ROOT / "tests" / "fake_swarm_agent.py"
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")


def run(tmp_path, args, wrap=WRAP, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_")}
    e.update(QWEN_CONFIG=str(tmp_path / "noconfig"), HOME=str(tmp_path), **env)
    return subprocess.run([BASH, str(wrap), *args], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(tmp_path), env=e, timeout=120)


def fake_env(tmp_path):
    d = tmp_path / "fake"
    d.mkdir(exist_ok=True)
    return {"FAKE_SWARM_DIR": str(d), "FAKE_SWARM_SLEEP": "0",
            "QWEN_SWARM_AGENT_OVERRIDE": "%s %s" % (sys.executable, FAKE)}


def test_no_arguments_is_a_usage_error(tmp_path):
    r = run(tmp_path, [])
    assert r.returncode == 2 and "no workflow given" in r.stderr


def test_help_and_version_only_as_the_first_argument(tmp_path):
    r = run(tmp_path, ["--help"])
    assert r.returncode == 0 and "EXIT CODES" in r.stdout and "BUILT-IN WORKFLOWS" in r.stdout
    qa = re.search(r'^QA_VERSION="(.*)"$', AGENT.read_text(encoding="utf-8"), re.M).group(1)
    r = run(tmp_path, ["--version"])
    assert r.returncode == 0 and r.stdout.strip() == "qwen-swarm %s" % qa
    r = run(tmp_path, ["research", "q", "--help"], **fake_env(tmp_path))
    assert r.returncode == 2 and "EXIT CODES" not in r.stdout and "usage: qwen-swarm" in r.stderr


def test_swarm_help_lists_record_verdict(tmp_path):
    r = run(tmp_path, ["--help"])
    assert r.returncode == 0
    assert ("--record-verdict RUN --id ID --verdict CONFIRMED|FALSE_ALARM|NEEDS_HUMAN "
            "--evidence TEXT") in r.stdout
    assert "--evidence is repeatable" in r.stdout


def test_list_and_check_need_no_server(tmp_path):
    r = run(tmp_path, ["--list"], **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert re.search(r"^research\t", r.stdout, re.M) and re.search(r"^debug\t", r.stdout, re.M)
    for name in ("research", "debug"):
        r = run(tmp_path, ["--check", name], **fake_env(tmp_path))
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("ok: %s:" % name)


def test_a_folder_workflow_runs_end_to_end(tmp_path):
    folder = make_workflow(tmp_path / "wfs")
    out = tmp_path / "run"
    r = run(tmp_path, [str(folder), "say hi", "--out", str(out)], **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == str(out / "report.md")
    assert (out / "report.md").read_text(encoding="utf-8") == "# Echo\n\n3 rows\n"


def test_the_alias_keeps_its_own_help_and_version(tmp_path):
    r = run(tmp_path, ["--help"], wrap=DR_WRAP)
    assert r.returncode == 0 and "qwen-deep-research" in r.stdout and "EXIT CODES" in r.stdout
    r = run(tmp_path, ["--version"], wrap=DR_WRAP)
    assert r.stdout.startswith("qwen-deep-research ")
    r = run(tmp_path, [], wrap=DR_WRAP)
    assert r.returncode == 2 and "qwen-deep-research:" in r.stderr and "question" in r.stderr


# ---- backwards compatibility: qwen-deep-research must behave exactly as released ----
import io  # noqa: E402
import json  # noqa: E402

import lib.research as rs  # noqa: E402

RUNNER = ROOT / "skill" / "local-auditor" / "lib" / "swarm_engine" / "runner.py"


def test_compat_stdin_wins_over_a_question_word(tmp_path, monkeypatch):
    # (a) the released qwen-deep-research read stdin whenever --stdin was given and
    # ignored the question word; exit 0, and the run folder holds stdin's question,
    # not the word. The research
    # role behaviours are test_research's fake agents; the env vars are its fixture's,
    # set here so no module-level name shadows the run(...) helper's **env.
    import test_research
    d = tmp_path / "fake"
    d.mkdir()
    for name, src in (("scoper", test_research.SCOPER), ("searcher", test_research.SEARCHER),
                      ("reader", test_research.READER), ("verifier", test_research.VERIFIER),
                      ("synthesizer", test_research.SYNTH)):
        (d / ("%s.py" % name)).write_text(src, encoding="utf-8")
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    monkeypatch.setenv("QWEN_DR_SKIP_SEARCH_CHECK", "1")
    monkeypatch.setenv("QWEN_DR_BACKOFF", "0")
    for k in ("QWEN_DR_MAX_AGENTS", "QWEN_DR_SEATS", "QWEN_DR_WEB_SEATS", "QWEN_DR_TIMEOUT",
              "QWEN_DR_MAX_ITEMS", "QWEN_DR_MAX_UNIT_SECONDS", "QWEN_SEARCH_KEY",
              "QWEN_SEARCH_URL", "QWEN_SEARCH_BRAVE_URL", "QWEN_SEARCH_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    out = tmp_path / "run"
    monkeypatch.setattr(sys, "stdin", io.StringIO("what is x?\n"))
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--out", str(out),
                    "--depth", "quick", "--stdin", "ignored"]) == 0
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["question"] == "what is x?"


def test_plain_swarm_keeps_the_stdin_goal_conflict_a_usage_error(tmp_path):
    folder = make_workflow(tmp_path / "wfs")
    r = run(tmp_path, [str(folder), "say hi", "--stdin"], **fake_env(tmp_path))
    assert r.returncode == 2 and "exclusive" in r.stderr


def test_compat_error_messages_keep_the_released_wording(tmp_path):
    # (b) an empty question: the released command's message, not the engine's
    r = run(tmp_path, [], wrap=DR_WRAP)
    assert r.returncode == 2
    assert "qwen-deep-research: no question given" in r.stderr
    assert "no goal given" not in r.stderr
    # an unknown --depth: the released command's argparse choice error plus its usage
    # line, not the engine's "must be one of" text
    r = run(tmp_path, ["--depth", "huge", "what is x?"], wrap=DR_WRAP)
    assert r.returncode == 2 and "usage: qwen-deep-research QUESTION" in r.stderr
    assert "--depth" in r.stderr and "must be one of" not in r.stderr
    # --resume with settings the run folder owns: the released command's list of
    # changeable flags, which predates the engine-only --rounds and --keep-sandboxes
    r = run(tmp_path, ["--resume", str(tmp_path / "norun"), "--depth", "quick", "x"],
            wrap=DR_WRAP)
    assert r.returncode == 2 and (
        "--resume takes the question and settings from the run folder; only --seats, "
        "--web-seats, --timeout, --retries, --hours, --effort and --role-effort may change"
        in r.stderr)
    assert "--keep-sandboxes" not in r.stderr and "--rounds" not in r.stderr


def test_plain_swarm_keeps_the_engine_resume_message(tmp_path):
    r = run(tmp_path, ["--resume", str(tmp_path / "norun"), "--depth", "quick", "w"],
            **fake_env(tmp_path))
    assert r.returncode == 2 and "goal and settings" in r.stderr
    assert "--keep-sandboxes may change" in r.stderr


def test_legacy_check_still_runs_the_model_and_search_preflight(tmp_path):
    # (c) the released command's bare --check, which qwen-swarm also calls --preflight:
    # the alias must still run the model check, then the search check, and say which
    # failed
    r = run(tmp_path, ["--check"], wrap=DR_WRAP, QWEN_DR_SKIP_SEARCH_CHECK="1",
            **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("ok: model and search reachable")
    r = run(tmp_path, ["--check"], wrap=DR_WRAP, QWEN_DR_SKIP_SEARCH_CHECK="1",
            FAKE_PREFLIGHT_RC="3", **fake_env(tmp_path))
    assert r.returncode == 3 and "model" in r.stderr
    r = run(tmp_path, ["--check"], wrap=DR_WRAP, QWEN_SEARCH_URL="http://127.0.0.1:9",
            **fake_env(tmp_path))
    assert r.returncode == 3 and "search" in r.stderr


def test_unknown_compat_value_is_a_usage_error(tmp_path):
    # (d) a value the runner does not know is a usage error, not silently plain qwen-swarm
    e = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_")}
    e.update(QWEN_CONFIG=str(tmp_path / "noconfig"), HOME=str(tmp_path))
    r = subprocess.run([sys.executable, str(RUNNER), "--compat", "bogus", "--agent",
                        sys.executable, "--agent", str(FAKE), "what is x?"],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(tmp_path),
                       env=e, timeout=120)
    assert r.returncode == 2 and "unknown --compat value" in r.stderr


def test_engine_only_flags_are_not_advertised_in_the_alias_help(tmp_path):
    # (e) qwen-deep-research accepts these but must not list them: they are engine
    # flags the released command never had; its help stays its own flag set
    r = run(tmp_path, ["--help"], wrap=DR_WRAP)
    assert r.returncode == 0 and "EXIT CODES" in r.stdout
    for flag in ("--set", "--list", "--rounds", "--target", "--keep-sandboxes",
                 "--record-verdict"):                 # the alias does not even accept this one
        assert flag not in r.stdout, flag


# ---------------------------------------------------------------- the browser fence
BROWSER_WORKFLOW = r'''
import json
def run(wf):
    seen = wf.agent("look", "tester", "open the page", wf.steps.extract_json)
    wf.write("saw.json", json.dumps(seen))
    wf.report("# UI\n\nwhat the session saw: saw.json\n")
'''
SEES_BROWSER_DIR = r'''
import json, os
def answer(p, r):
    return 0, "```json\n" + json.dumps({"dir": os.environ.get("QWEN_BROWSER_DIR")}) + "\n```"
'''


def browser_folder(tmp_path):
    return make_workflow(tmp_path / "wfs",
                         {"roles": {"tester": {"file": "roles/tester.md",
                                               "fence": "browser"}}},
                         script=BROWSER_WORKFLOW, roles=("tester",))


def test_a_browser_role_gets_its_browser_and_its_own_folder(tmp_path):
    # The shipped command, end to end: a browser role's unit is passed qwen-agent
    # --browser and no --mcp-config of the engine's, and its session is told its
    # QWEN_BROWSER_DIR is RUN/browser/<unit> -- so the screenshots of a UI suite stay with
    # the run that made them. No search backend and no --target is involved anywhere.
    d = tmp_path / "fake"
    d.mkdir(exist_ok=True)
    (d / "tester.py").write_text(SEES_BROWSER_DIR, encoding="utf-8")
    folder = browser_folder(tmp_path)
    out = tmp_path / "run"
    r = run(tmp_path, [str(folder), "check the ui", "--out", str(out)], **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads((out / "saw.json").read_text(encoding="utf-8")) == {
        "dir": str(out / "browser" / "look-1")}
    argv = calls(d)[0]
    assert "--browser" in argv and "--mcp-config" not in argv and "--web" not in argv
    assert not (out / "mcp.json").exists()              # a browser run needs no search setup


def test_a_browser_workflow_checks_and_preflights_without_a_search_backend(tmp_path):
    # A UI suite runs against local URLs: a browser role is not a web fence, so neither
    # the dry run nor the preflight asks for mcp.json or a reachable search backend.
    folder = browser_folder(tmp_path)
    r = run(tmp_path, ["--check", str(folder)], **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("ok: echo:")
    r = run(tmp_path, ["--preflight", str(folder)], **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- config-file settings
ENV_WORKFLOW = r'''
import os
def run(wf):
    # a test probe: real workflows must not read the environment
    wf.save("seen", {k: os.environ.get(k) for k in
                     ("QWEN_PLAYWRIGHT_MCP", "QWEN_CLAUDE_BIN", "QWEN_EXEC_RETRY_BACKOFF")})
    wf.report("# env\n")
'''


def test_config_file_browser_and_claude_settings_reach_the_runner(tmp_path):
    # wf.claude_check runs inside the runner, not in a qwen-agent child that reads the
    # config file itself: qwen-swarm.sh must export these from the file.
    (tmp_path / "noconfig").write_text(
        'QWEN_PLAYWRIGHT_MCP="node /x/cli.js"\nQWEN_CLAUDE_BIN=/opt/cc/claude\n'
        'QWEN_EXEC_RETRY_BACKOFF="0 0"\n', encoding="utf-8")
    folder = make_workflow(tmp_path / "wfs", script=ENV_WORKFLOW)
    out = tmp_path / "run"
    r = run(tmp_path, [str(folder), "g", "--out", str(out)], **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr
    seen = json.loads((out / "seen.json").read_text(encoding="utf-8"))
    assert seen["QWEN_PLAYWRIGHT_MCP"] == "node /x/cli.js"
    assert seen["QWEN_EXEC_RETRY_BACKOFF"] == "0 0"
    # Git Bash hands a native Python the Windows spelling of a POSIX path
    assert seen["QWEN_CLAUDE_BIN"].replace("\\", "/").endswith("/opt/cc/claude")


def test_unset_settings_stay_unset(tmp_path):
    folder = make_workflow(tmp_path / "wfs", script=ENV_WORKFLOW)
    out = tmp_path / "run"
    r = run(tmp_path, [str(folder), "g", "--out", str(out)], **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads((out / "seen.json").read_text(encoding="utf-8")) == {
        "QWEN_PLAYWRIGHT_MCP": None, "QWEN_CLAUDE_BIN": None, "QWEN_EXEC_RETRY_BACKOFF": None}


def test_swarm_exports_a_blank_retry_backoff(tmp_path):
    # a blank QWEN_EXEC_RETRY_BACKOFF is a value of its own -- "no retry". Exported only
    # when non-empty, it would reach the runner unset there and the default "10 30" would
    # be back: every blank spelling (empty, or only a space) goes out blank, while the
    # other variables of that loop stay unset when they hold nothing
    for i, blank in enumerate(("", " ")):
        (tmp_path / "noconfig").write_text('QWEN_EXEC_RETRY_BACKOFF="%s"\n' % blank,
                                           encoding="utf-8")
        folder = make_workflow(tmp_path / ("wfs%d" % i), script=ENV_WORKFLOW)
        out = tmp_path / ("run%d" % i)
        r = run(tmp_path, [str(folder), "g", "--out", str(out)], **fake_env(tmp_path))
        assert r.returncode == 0, r.stderr
        seen = json.loads((out / "seen.json").read_text(encoding="utf-8"))
        assert seen["QWEN_EXEC_RETRY_BACKOFF"] == blank         # set blank, not unset
        assert seen["QWEN_PLAYWRIGHT_MCP"] is None and seen["QWEN_CLAUDE_BIN"] is None


# ------------------------------------------------------------ a config-file value reaches claude_check
import pytest  # noqa: E402

CLAUDE_WORKFLOW = r'''
import json
def run(wf):
    res = wf.claude_check("c", [{"id": "A"}], lambda it: "check ITEM=A",
                          lambda text, it: json.loads(text), model="opus", max_calls=1,
                          browser=True)
    wf.save("checked", res)
    wf.report("# checked\n")
'''


@pytest.mark.skipif(sys.platform == "win32", reason="fake claude is a POSIX script")
def test_config_file_settings_reach_claude_check(tmp_path):
    # The config file names the claude binary and the Playwright command; the runner's
    # wf.claude_check uses both: the fake claude answers, and the MCP config it was
    # handed starts the configured server.
    from test_claude_check import FAKE as FAKE_CLAUDE
    fake = tmp_path / "bin" / "my-claude"
    fake.parent.mkdir()
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    fake.chmod(0o755)
    cc = tmp_path / "cc"
    cc.mkdir()
    (tmp_path / "noconfig").write_text(
        'QWEN_PLAYWRIGHT_MCP="node /x/cli.js"\nQWEN_CLAUDE_BIN=%s\n' % fake, encoding="utf-8")
    folder = make_workflow(tmp_path / "wfs", script=CLAUDE_WORKFLOW)
    out = tmp_path / "run"
    r = run(tmp_path, [str(folder), "g", "--out", str(out)], FAKE_CC_DIR=str(cc),
            **fake_env(tmp_path))
    assert r.returncode == 0, r.stderr
    [row] = json.loads((out / "checked.json").read_text(encoding="utf-8"))
    assert row["state"] == "ok" and row["data"] == {"seen": "A"}
    argv = json.loads((cc / "call.1").read_text(encoding="utf-8"))["argv"]
    cfg = json.loads(pathlib.Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["playwright"]["command"] == "node"
    assert cfg["mcpServers"]["playwright"]["args"][0] == "/x/cli.js"
