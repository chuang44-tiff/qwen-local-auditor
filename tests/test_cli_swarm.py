"""The qwen-swarm wrapper: everything the bash side owns, offline (fake agent)."""
import os
import pathlib
import re
import shutil
import subprocess
import sys

from swarm_fixtures import make_workflow

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
    for flag in ("--set", "--list", "--rounds", "--target", "--keep-sandboxes"):
        assert flag not in r.stdout, flag
