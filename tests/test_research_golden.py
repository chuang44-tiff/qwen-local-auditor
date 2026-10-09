"""Byte-identity guard: a one-round research run (quick, standard) must produce the same
run folder, unit names, cache keys, phase files, report and stdout as the released
qwen-deep-research did before the swarm-engine port. Written to pass on that code first;
it must keep passing."""
import hashlib
import json
import pathlib
import re
import sys

import pytest

import lib.research as rs
from test_research import FAKE, env  # noqa: F401  (the fixture is used by name)

# events.jsonl is the one addition since the release: the engine's progress stream
# (lib/swarm_engine/events.py), written by every workflow's runs
PART_B_FILES = ["agents", "angles.json", "claims.json", "config.json", "events.jsonl",
                "fetch_stats.json", "mcp.json", "question.md", "report.md", "run.log",
                "totals.json", "urls.json", "votes.json"]
PART_B_CFG_KEYS = ["question", "depth", "angles", "sources", "claims", "voters", "max_agents",
                   "max_items", "timeout_per_item", "retries", "effort", "role_effort",
                   "hours", "deadline"]
RUN_ROWS = ["sources fetched", "sources attempted", "claims extracted", "supported", "refuted",
            "unclear", "agents run", "units dropped", "stopped at deadline", "tokens",
            "invocations", "wall time"]
GOLDEN = {
    "quick": {
        "units": ["fetch-1", "fetch-2", "fetch-3", "fetch-4", "scope-1", "search-1", "search-2",
                  "search-3", "synth-1", "verify-1", "verify-2", "verify-3", "verify-4",
                  "verify-5"],
        "phase": {"angles.json": "f8cf3ef88a5b414c", "urls.json": "7f498cb74b83751c",
                  "claims.json": "5ae3722fe4379dbb", "fetch_stats.json": "e3fd91ea6056ddc0",
                  "votes.json": "1d5cccc5b5f07ffb"},
        "prompts": "387ba6197b2cf6b7",
        "totals": {"agents_run": 14, "tokens": 1540, "invocations": 1},
        "report": (
            "# Answer\n\nThe answer is supported [1].\n\n## Sources\n\n"
            "[1] T1 — https://site1.example/page?utm_source=x#top\n"
            "[2] T2 — https://site2.example/page?utm_source=x#top\n"
            "[3] Shared — https://shared.example/\n"
            "[4] T3 — https://site3.example/page?utm_source=x#top\n\n"
            "## Run\n\n| | |\n|---|---|\n| sources fetched | 4 |\n| sources attempted | 4 |\n"
            "| claims extracted | 5 |\n| supported | 4 |\n| refuted | 1 |\n| unclear | 0 |\n"
            "| agents run | 14 |\n| units dropped | 0 |\n| stopped at deadline | no |\n"
            "| tokens | 1540 |\n| invocations | 1 |\n| wall time | WALL |\n"),
    },
    "standard": {
        "units": ["fetch-1", "fetch-2", "fetch-3", "fetch-4", "fetch-5", "fetch-6", "scope-1",
                  "search-1", "search-2", "search-3", "search-4", "search-5", "synth-1",
                  "verify-1", "verify-2", "verify-3", "verify-4", "verify-5", "verify-6",
                  "verify-7", "verify-8"],
        "phase": {"angles.json": "7ead07d95bb09cad", "urls.json": "3b6b51fbcedae301",
                  "claims.json": "22a79084a939ad13", "fetch_stats.json": "a65c532b94a45ddd",
                  "votes.json": "1cd9268cf849a1de"},
        "prompts": "d440ebaca8104c4c",
        "totals": {"agents_run": 21, "tokens": 2310, "invocations": 1},
        "report": (
            "# Answer\n\nThe answer is supported [1].\n\n## Sources\n\n"
            "[1] T1 — https://site1.example/page?utm_source=x#top\n"
            "[2] T4 — https://site4.example/page?utm_source=x#top\n"
            "[3] T2 — https://site2.example/page?utm_source=x#top\n"
            "[4] T5 — https://site5.example/page?utm_source=x#top\n"
            "[5] Shared — https://shared.example/\n"
            "[6] T3 — https://site3.example/page?utm_source=x#top\n\n"
            "## Run\n\n| | |\n|---|---|\n| sources fetched | 6 |\n| sources attempted | 6 |\n"
            "| claims extracted | 7 |\n| supported | 6 |\n| refuted | 1 |\n| unclear | 0 |\n"
            "| agents run | 21 |\n| units dropped | 0 |\n| stopped at deadline | no |\n"
            "| tokens | 2310 |\n| invocations | 1 |\n| wall time | WALL |\n"),
    },
}


def _canon(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _part_b_key(role_text, prompt, toolset, grants, web, mcp_text, effort, deep=""):
    blob = "%s\n%s\n%s\n%s\n%s\n%s\n%s" % (role_text, prompt, toolset, grants, web, mcp_text,
                                           effort or "")
    return hashlib.sha256((blob + deep).encode("utf-8")).hexdigest()


# default depth on: a research role with no "deep" field is deep now, so its key carries
# the switch list; the fan-out roles (searcher, reader, verifier) say "deep": false and
# their keys stay byte-identical to the released ones
DEEP = "\ndeep:review_round"            # fence none: no subagent switch
DEEP_ROLES = {"scoper": DEEP, "synthesizer": DEEP}


def _run(tmp_path, depth):
    out = tmp_path / ("run-" + depth)
    rc = rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--out", str(out),
                  "--depth", depth, "what is x?"])
    return rc, out


@pytest.mark.parametrize("depth", ["quick", "standard"])
def test_one_round_run_folder_matches_the_released_one(tmp_path, env, capsys, depth):  # noqa: F811
    rc, run = _run(tmp_path, depth)
    assert rc == 0
    g = GOLDEN[depth]
    # stdout is exactly one line, the report's path
    assert capsys.readouterr().out.splitlines() == [str(run / "report.md")]
    # the run folder holds exactly the released command's files: no rounds.json, no
    # round-1/, no report-round-1.md, no goal.md, no sandboxes/ or cmds/
    assert sorted(p.name for p in run.iterdir()) == PART_B_FILES
    agents = run / "agents"
    assert sorted(p.name for p in agents.iterdir() if p.is_dir()) == g["units"]
    for f, h in g["phase"].items():
        assert _canon(run / f) == h, f
    prompts = {p.name[:-len(".prompt.md")]: hashlib.sha256(
        p.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:16]
        for p in sorted(agents.glob("*.prompt.md"))}
    allp = hashlib.sha256("".join(prompts[k] for k in sorted(prompts)).encode()).hexdigest()[:16]
    assert allp == g["prompts"]
    totals = json.loads((run / "totals.json").read_text(encoding="utf-8"))
    assert sorted(totals) == ["agents_run", "invocations", "seconds", "tokens"]   # no stop_reason
    assert {k: totals[k] for k in g["totals"]} == g["totals"]
    report = (run / "report.md").read_text(encoding="utf-8")
    report = re.sub(r"\| wall time \| \d+m\d{2}s \|", "| wall time | WALL |", report)
    assert report == g["report"]
    assert re.findall(r"^\| ([a-z ]+) \|", report, re.M) == RUN_ROWS
    cfg = json.loads((run / "config.json").read_text(encoding="utf-8"))
    assert list(cfg)[:len(PART_B_CFG_KEYS)] == PART_B_CFG_KEYS   # new keys only after these
    assert (run / "question.md").read_text(encoding="utf-8") == "what is x?\n"


def test_one_round_cache_keys_match_the_released_ones(tmp_path, env):  # noqa: F811
    # the fence flags and the key blob are unchanged, so a run folder the released
    # command wrote resumes with every shallow unit still a cache hit; the roles that are
    # deep by default now (DEEP_ROLES) have new keys and run again
    rc, run = _run(tmp_path, "quick")
    assert rc == 0
    agents = run / "agents"
    mcp_text = (run / "mcp.json").read_text(encoding="utf-8")
    fences = {"scope": ("scoper", "none", "", False, ""),
              "search": ("searcher", "none", "mcp__search__search", False, mcp_text),
              "fetch": ("reader", "none", "mcp__search__search", True, mcp_text),
              "verify": ("verifier", "none", "mcp__search__search", True, mcp_text),
              "synth": ("synthesizer", "none", "", False, "")}
    for rec in sorted(agents.glob("*.json")):
        name = rec.name[:-len(".json")]
        role, toolset, grants, web, mcp = fences[name.split("-")[0]]
        role_text = (rs.ROLES / ("%s.md" % role)).read_text(encoding="utf-8")
        prompt = (agents / ("%s.prompt.md" % name)).read_text(encoding="utf-8")
        # default depth on: only a role that is deep by now gains the switch suffix
        want = _part_b_key(role_text, prompt, toolset, grants, web, mcp, None,
                           DEEP_ROLES.get(role, ""))
        assert json.loads(rec.read_text(encoding="utf-8"))["key"] == want, name


def test_nothing_usable_prints_the_run_folder_last(tmp_path, env, capsys):  # noqa: F811
    (env / "searcher.py").write_text("def answer(p, r):\n    return 0, '```json\\n[]\\n```'\n",
                                     encoding="utf-8")
    rc, run = _run(tmp_path, "quick")
    assert rc == 5
    assert capsys.readouterr().out.splitlines()[-1] == str(run)


def test_the_role_texts_are_pinned():
    # the role text is part of every cache key: editing a role breaks resume of old
    # runs, so its exact bytes are pinned here (universal newlines: a Windows CRLF
    # checkout hashes the same)
    pinned = {"reader": "47c846f16c7dacd74b5c886e3af978fe9c70a523d07f4cf9f6d7464dc1775996",
              "scoper": "a22ea2c378a65fbacb03ab08dab4c8821965a1fd068474790009609774e3118d",
              "searcher": "3508101a0456fb83a98e4b842b3f29b70a5d10082fa72c19f5698bd0fd131dd2",
              "synthesizer": "b10d7ec9ff7e06f857e71cd3d659db089a428727a625f44f5eee6a5a42b9c052",
              "verifier": "b6320e676fc1e62e3feb36bb6f668b4eb767755e57b8381108775827ec60154d"}
    for role, want in pinned.items():
        text = pathlib.Path(rs.ROLES, "%s.md" % role).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == want, role


def test_research_is_a_builtin_workflow(capsys):
    from lib.swarm_engine import manifest, runner
    assert "research" in runner.builtin_names()
    m = manifest.load(runner.BUILTIN / "research")
    assert {r: m.roles[r].fence for r in m.roles} == {
        "scoper": "none", "searcher": "search", "reader": "web", "verifier": "web",
        "synthesizer": "none", **({"planner": "none"} if "planner" in m.roles else {})}
    assert rs.PRESETS == {"quick": (3, 6, 10, 1, 240, 1), "standard": (5, 15, 25, 3, 240, 1),
                          "deep": (8, 30, 50, 3, 600, 2), "overnight": (10, 40, 80, 5, 900, 3)}
    assert runner.main(["--agent", sys.executable, "--list"]) == 0
    assert "research\tCited report" in capsys.readouterr().out
    assert runner.main(["--agent", sys.executable, "--check", "research"]) == 0
    assert capsys.readouterr().out.startswith("ok: research:")
