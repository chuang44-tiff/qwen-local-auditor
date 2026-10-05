"""Multi-round research (deep: 2 rounds, overnight: until the deadline or convergence)."""
import json
import pathlib
import re
import sys

import lib.research as rs
from test_research import (DROPPING_READER, FAKE, READER, VERIFIER, calls,
                           env)  # noqa: F401  (env is a fixture)

PLANNER_NEW = r'''
import json
def answer(p, r):
    return 0, "```json\n" + json.dumps({"angles": [
        {"angle": "facet new", "queries": ["fresh query"], "reason": "the gap"},
        {"angle": "facet 0", "queries": ["q0"], "reason": "a repeat: dropped"}],
        "recheck": ["C2", "C999"]}) + "\n```"
'''
PLANNER_EMPTY = r'''
def answer(p, r):
    return 0, '```json\n{"angles": [], "recheck": []}\n```'
'''
# C2 is unclear in round 1; once the planner has run (the flag file), every vote supports
FLAG_VERIFIER = r'''
import json, os, pathlib, re
def answer(p, r):
    later = pathlib.Path(os.environ["FAKE_SWARM_DIR"], "round2").exists()
    out = []
    for c in re.findall(r"^- (C\d+):", p, re.M):
        v = "refuted" if c == "C1" else ("unclear" if c == "C2" and not later else "supported")
        out.append({"claim": c, "verdict": v, "evidence_url": "https://e.example/",
                    "snippet": "s", "reason": "r"})
    return 0, "```json\n" + json.dumps(out) + "\n```"
'''
FLAG_PLANNER = PLANNER_NEW.replace("def answer(p, r):\n", (
    "def answer(p, r):\n"
    "    import os, pathlib\n"
    "    pathlib.Path(os.environ['FAKE_SWARM_DIR'], 'round2').write_text('x')\n"))


def deep(tmp_path, *extra):
    run = tmp_path / "run"
    rc = rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--out", str(run),
                  "--depth", "deep", *extra, "what is x?"])
    return rc, run


def names(d):
    return {pathlib.Path(a[a.index("-C") + 1]).name for a in calls(d)}


def test_deep_runs_a_second_round_on_new_angles_only(tmp_path, env):  # noqa: F811
    (env / "planner.py").write_text(PLANNER_NEW, encoding="utf-8")
    rc, run = deep(tmp_path)
    assert rc == 0
    got = names(env)
    assert {"r2-plan-1", "r2-search-1", "r2-fetch-1", "r2-synth-1"} <= got
    assert all(n.startswith("r2-") for n in got if n.startswith("r2"))
    plan = json.loads((run / "round-2" / "plan.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in plan["angles"]] == ["A9"]            # the repeat was dropped
    assert plan["recheck"] == []                                  # C2 is not unclear here
    urls1 = json.loads((run / "urls.json").read_text(encoding="utf-8"))
    urls2 = json.loads((run / "round-2" / "urls.json").read_text(encoding="utf-8"))
    assert [u["id"] for u in urls2] == ["S%d" % (len(urls1) + 1)]  # ids continue
    assert urls2[0]["url"].startswith("https://site9.example/")    # shared.example was seen
    claims1 = json.loads((run / "claims.json").read_text(encoding="utf-8"))
    claims2 = json.loads((run / "round-2" / "claims.json").read_text(encoding="utf-8"))
    assert [c["claim"] for c in claims2] == ["claim from S%d" % (len(urls1) + 1)]   # no repeat
    assert claims2[0]["id"] == "C%d" % (len(claims1) + 1)
    r1 = (run / "report-round-1.md").read_text(encoding="utf-8")
    r2 = (run / "report-round-2.md").read_text(encoding="utf-8")
    assert (run / "report.md").read_text(encoding="utf-8") == r2
    assert "| rounds | 1 |" in r1 and "| rounds | 2 |" in r2
    assert "| claims extracted | %d |" % (len(claims1) + 1) in r2
    totals = json.loads((run / "totals.json").read_text(encoding="utf-8"))
    assert totals["stop_reason"] == "rounds"
    assert json.loads((run / "rounds.json").read_text(encoding="utf-8")) == [1, 2]
    prompt = (run / "agents" / "r2-plan-1.prompt.md").read_text(encoding="utf-8")
    assert "- A1: facet 0" in prompt and "fresh query" not in prompt


def test_an_empty_plan_converges_without_another_synthesis(tmp_path, env):  # noqa: F811
    (env / "planner.py").write_text(PLANNER_EMPTY, encoding="utf-8")
    rc, run = deep(tmp_path)
    assert rc == 0
    got = names(env)
    assert "r2-plan-1" in got and not any(n.startswith(("r2-search", "r2-synth")) for n in got)
    assert not (run / "report-round-2.md").exists()
    totals = json.loads((run / "totals.json").read_text(encoding="utf-8"))
    assert totals["stop_reason"] == "converged: the planner found no new angle"


def test_rechecked_votes_accumulate_against_the_votes_requested(tmp_path, env):  # noqa: F811
    (env / "verifier.py").write_text(FLAG_VERIFIER, encoding="utf-8")
    (env / "planner.py").write_text(FLAG_PLANNER, encoding="utf-8")
    rc, run = deep(tmp_path)
    assert rc == 0
    plan = json.loads((run / "round-2" / "plan.json").read_text(encoding="utf-8"))
    assert plan["recheck"] == ["C2"]                              # C999 is no unclear claim
    v2 = json.loads((run / "round-2" / "votes.json").read_text(encoding="utf-8"))
    assert len(v2["C2"]["votes"]) == 3 and v2["C2"]["requested"] == 3
    # 3 unclear + 3 supported of 6 requested: no majority, still unclear
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "| unclear | 1 |" in report


def test_a_round_without_a_new_supported_claim_converges(tmp_path, env):  # noqa: F811
    (env / "planner.py").write_text(PLANNER_NEW, encoding="utf-8")
    (env / "verifier.py").write_text(VERIFIER.replace('"refuted" if c == "C1"',
                                                      '"refuted" if c in ("C1", "C11")'),
                                     encoding="utf-8")
    rc, run = deep(tmp_path, "--rounds", "3", "--hours", "1")
    assert rc == 0
    totals = json.loads((run / "totals.json").read_text(encoding="utf-8"))
    assert totals["stop_reason"] == "converged: round 2 added no supported claim"
    assert not any(n.startswith("r3-") for n in names(env))


def test_resume_finishes_the_interrupted_round_from_the_cache(tmp_path, env):  # noqa: F811
    (env / "planner.py").write_text(PLANNER_NEW, encoding="utf-8")
    (env / "verifier.py").write_text(VERIFIER.replace(
        "    out = []\n", "    out = []\n    if 'C11' in ids: return 5, ''\n"), encoding="utf-8")
    rc, run = deep(tmp_path)
    assert rc == 4                                                # r2's verifier dropped
    (env / "verifier.py").write_text(VERIFIER, encoding="utf-8")
    (env / "calls.jsonl").unlink()
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--resume", str(run)]) == 0
    again = names(env)
    assert again and all(n.startswith(("r2-verify", "r2-synth")) for n in again), again
    assert json.loads((run / "rounds.json").read_text(encoding="utf-8")) == [1, 2]


def test_overnight_is_open_ended_but_bounded():
    from lib.swarm_engine import manifest, runner
    m = manifest.load(runner.BUILTIN / "research")
    assert m.presets["overnight"]["rounds"] == "until" and m.presets["overnight"]["hours"] == 8
    assert m.presets["deep"]["rounds"] == 2
    assert m.presets["quick"]["rounds"] == m.presets["standard"]["rounds"] == 1
    assert m.roles["planner"].fence == "none"


def test_an_empty_answer_is_an_empty_plan():
    from lib.swarm_engine import runner
    wf = runner.load_module(runner.BUILTIN / "research")
    st = wf._State({"angles": [{"id": "A1", "angle": "a", "queries": ["q"]}], "urls": [],
                    "claims": [], "votes": {}, "fetched": 0}, 3)
    for text in ("```json\n[]\n```", "```json\n{}\n```"):
        assert wf.parse_plan(st, 3)(text) == {"angles": [], "recheck": []}
    got = wf.parse_plan(st, 1)('```json\n{"angles": [{"angle": "A", "queries": ["q", "new"]},'
                               ' {"angle": "b", "queries": ["x"]}, {"angle": "c", "queries": ["y"]}]}\n```')
    assert got["angles"] == [{"id": "A2", "angle": "b", "queries": ["x"], "reason": ""}]   # cap 1


# ---------------------------------------------------------------- the review's findings

# a fresh angle every round (named for how many the prompt already lists), C2 re-checked
# every time the planner speaks
RECHECK_PLANNER = r'''
import json, re
def answer(p, r):
    n = len(re.findall(r"^- A\d+:", p, re.M))
    return 0, "```json\n" + json.dumps({"angles": [
        {"angle": "facet e%d" % n, "queries": ["qe %d" % n], "reason": "the gap"}],
        "recheck": ["C2"]}) + "\n```"
'''


def test_resume_after_a_dropped_round_has_unique_claim_ids(tmp_path, env):  # noqa: F811
    # round 1 loses a reader: its lists are short and unsaved, so no round 2 may be
    # built on ids --resume will renumber, and the resume gets one clean set of them
    (env / "planner.py").write_text(PLANNER_NEW, encoding="utf-8")
    (env / "reader.py").write_text(DROPPING_READER, encoding="utf-8")
    rc, run = deep(tmp_path)
    assert rc == 4
    assert not (run / "round-2").exists()                 # no round built on short lists
    (env / "reader.py").write_text(READER, encoding="utf-8")
    (env / "calls.jsonl").unlink()
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE),
                    "--resume", str(run)]) == 0
    ids = [c["id"] for f in (run / "claims.json", run / "round-2" / "claims.json")
           for c in json.loads(f.read_text(encoding="utf-8"))]
    assert ids and len(ids) == len(set(ids))              # C10 appears once, not twice
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "| units dropped | 0 |" in report


def test_stale_plan_is_recomputed(tmp_path, env):  # noqa: F811
    # a plan.json made from a different planner prompt than this replay rebuilds is
    # stale: its ids can collide with round 1's, so the round forgets its files and
    # recomputes them instead of trusting them
    (env / "planner.py").write_text(PLANNER_NEW, encoding="utf-8")
    rc, run = deep(tmp_path)
    assert rc == 0
    plan_file = run / "round-2" / "plan.json"
    good = json.loads(plan_file.read_text(encoding="utf-8"))
    assert len(good["basis"]) == 64                       # sha256 hex of the prompt
    plan_file.write_text(json.dumps(
        {"angles": [{"id": "A99", "angle": "stale angle", "queries": ["stale query"],
                    "reason": "from an older round"}], "recheck": [], "basis": "0" * 64}),
        encoding="utf-8")
    before = {k: (run / "round-2" / ("%s.json" % k)).stat().st_mtime_ns
              for k in ("urls", "claims", "votes")}
    original = {k: (run / "round-2" / ("%s.json" % k)).read_text(encoding="utf-8")
                for k in ("urls", "claims", "votes")}
    (env / "calls.jsonl").unlink()
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE),
                    "--resume", str(run)]) == 0
    now = json.loads(plan_file.read_text(encoding="utf-8"))
    assert "stale angle" not in plan_file.read_text(encoding="utf-8")
    assert [a["id"] for a in now["angles"]] == ["A9"]     # the real plan, recomputed
    assert now["basis"] == good["basis"]
    for k in ("urls", "claims", "votes"):                 # the whole round was redone
        f = run / "round-2" / ("%s.json" % k)
        assert f.stat().st_mtime_ns > before[k], k
        assert f.read_text(encoding="utf-8") == original[k]


def test_prompt_budget_caps_listings(tmp_path, env, monkeypatch):  # noqa: F811
    from lib.swarm_engine import runner
    wf_mod = runner.load_module(runner.BUILTIN / "research")
    (env / "planner.py").write_text(PLANNER_NEW, encoding="utf-8")
    monkeypatch.setattr(wf_mod, "PROMPT_BUDGET", 300)
    rc, run = deep(tmp_path)
    assert rc == 0
    prompt = (run / "agents" / "r2-plan-1.prompt.md").read_text(encoding="utf-8")
    blank = wf_mod.PLAN_P.format(q="what is x?", angles="(none)", supported="(none)",
                                 unclear="(none)", refuted="(none)", gaps="(none)", n=8)
    assert len(prompt) <= len(blank) + wf_mod.PROMPT_BUDGET + 80     # budget + template
    assert "more omitted for length" in prompt
    monkeypatch.setattr(wf_mod, "PROMPT_BUDGET", 120000)
    out = tmp_path / "std"
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--out", str(out),
                    "--depth", "standard", "what is x?"]) == 0
    for p in (out / "agents").glob("*.prompt.md"):        # the real budget never bites
        assert "more omitted" not in p.read_text(encoding="utf-8")   # golden pins the rest


def test_gaps_keeps_subheadings():
    from lib.swarm_engine import runner
    wf = runner.load_module(runner.BUILTIN / "research")
    body = ("# Answer\n\nText.\n\n## Gaps\n\nFirst gap.\n\n### Contested evidence\n\n"
            "Still open.\n\n## Sources\n\n[1] x\n")
    assert wf._gaps(body) == "First gap.\n\n### Contested evidence\n\nStill open."
    assert wf._gaps("## Gaps\n\ntail with no heading after it") == "tail with no heading after it"
    assert wf._gaps("# Answer\n\nno gaps section here") == "(none)"


def test_repeated_queries_in_one_angle_dedupe():
    from lib.swarm_engine import runner
    wf = runner.load_module(runner.BUILTIN / "research")
    st = wf._State({"angles": [{"id": "A1", "angle": "a", "queries": ["q"]}], "urls": [],
                    "claims": [], "votes": {}, "fetched": 0}, 3)
    got = wf.parse_plan(st, 3)('```json\n{"angles": [{"angle": "new", '
                               '"queries": ["x", " X ", "y", "z", "w", "x"]}]}\n```')
    assert got["angles"][0]["queries"] == ["x", "y", "z"]     # repeats gone, then the cap
    got = wf.parse_plan(st, 3)('```json\n{"angles": [{"angle": "new", '
                               '"queries": ["Q ", "x"]}]}\n```')
    assert got["angles"][0]["queries"] == ["x"]               # an earlier angle's query too


def test_recheck_limit(tmp_path, env):  # noqa: F811
    # the planner begs for C2 every round, but a claim is rechecked at most twice
    (env / "planner.py").write_text(RECHECK_PLANNER, encoding="utf-8")
    (env / "verifier.py").write_text(VERIFIER.replace(
        'v = "refuted" if c == "C1" else "supported"',
        'v = "unclear" if c == "C2" else ("refuted" if c == "C1" else "supported")'),
        encoding="utf-8")
    rc, run = deep(tmp_path, "--rounds", "4", "--hours", "1")
    assert rc == 0
    plan4 = json.loads((run / "round-4" / "plan.json").read_text(encoding="utf-8"))
    assert plan4["recheck"] == ["C2"]                     # the plan still asks
    assert "C2" in json.loads((run / "round-2" / "votes.json").read_text(encoding="utf-8"))
    assert "C2" in json.loads((run / "round-3" / "votes.json").read_text(encoding="utf-8"))
    assert "C2" not in json.loads((run / "round-4" / "votes.json").read_text(encoding="utf-8"))


def test_planner_deadline_is_not_reported_as_converged(tmp_path, env):  # noqa: F811
    # a resume whose deadline has already passed: round 2's planner never starts, and
    # the totals must say "deadline", not that the run converged
    (env / "planner.py").write_text(PLANNER_EMPTY, encoding="utf-8")
    run = tmp_path / "run"
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--out", str(run),
                    "--depth", "overnight", "--max-agents", "5", "--hours", "1",
                    "what is x?"]) == 0
    (run / "round-2" / "plan.json").unlink()
    # --effort re-keys the cached planner unit; the (floored, ~now) deadline stops it
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--resume", str(run),
                    "--hours", "0.000001", "--effort", "high"]) == 4
    totals = json.loads((run / "totals.json").read_text(encoding="utf-8"))
    assert totals["stop_reason"] == "deadline"


# ---------------------------------------------------------------- the deadline is not a drop

# a planner that starts while there is still time and answers after the deadline passed,
# so the rest of its round is left unrun and nothing at all is dropped
PLANNER_SLOW = r'''
import json, time
def answer(p, r):
    time.sleep(5)
    return 0, "```json\n" + json.dumps({"angles": [
        {"angle": "facet new", "queries": ["fresh query"], "reason": "the gap"}],
        "recheck": []}) + "\n```"
'''


def test_deadline_cut_round_reports_deadline(tmp_path, env, capsys):  # noqa: F811
    # --hours is an absolute deadline: the items a deadline left unrun are not dropped
    # units and --resume retries them only with a new --hours, so a round cut short this
    # way is ended by the engine's deadline path, not by the dropped-round message
    run = tmp_path / "run"
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--out", str(run),
                    "--depth", "overnight", "--max-agents", "5", "--hours", "1",
                    "what is x?"]) == 0
    (run / "round-2" / "plan.json").unlink()
    (run / "agents" / "r2-plan-1.json").unlink()       # the planner has to answer again
    (env / "planner.py").write_text(PLANNER_SLOW, encoding="utf-8")
    (env / "calls.jsonl").unlink()
    capsys.readouterr()
    # round 1 replays from the cache in well under the 2 s of runway, so the planner
    # starts before its deadline and the round's own work is what the deadline stops
    assert rs.main(["--agent", sys.executable, "--agent", str(FAKE), "--resume", str(run),
                    "--hours", "0.0006"]) == 4
    assert names(env) == {"r2-plan-1", "r2-synth-1"}   # no search, fetch or vote ran
    totals = json.loads((run / "totals.json").read_text(encoding="utf-8"))
    assert totals["stop_reason"] == "deadline"
    assert "stopped at deadline | yes" in (run / "report.md").read_text(encoding="utf-8")
    captured = capsys.readouterr().err
    assert "units dropped" not in captured
    assert "deadline reached" in captured


# ---------------------------------------------------------------- what the budget keeps

def test_budget_keeps_earlier_angles(monkeypatch):
    from lib.swarm_engine import runner
    wf_mod = runner.load_module(runner.BUILTIN / "research")

    class Cfg:                                        # _plan_prompt reads nothing else
        goal = "what is x?"

        def knob(self, name):
            assert name == "angles"
            return 8

    angles = [{"id": "A%d" % i, "angle": "facet %d" % i, "queries": ["q%d" % i]}
              for i in range(1, 9)]
    claims = [{"id": "C%d" % i, "claim": "claim number %02d" % i} for i in range(1, 61)]
    votes = {c["id"]: {"votes": [], "status": "supported" if i % 2 else "refuted"}
             for i, c in enumerate(claims, 1)}
    st = wf_mod._State({"angles": angles, "urls": [], "claims": claims, "votes": votes,
                        "fetched": 0}, 3)
    listed = "\n".join("- %s: %s\n  queries: %s" % (a["id"], a["angle"],
                                                    "; ".join(a["queries"])) for a in angles)
    # room for all eight angles and for some of the 60 claims: the angles and their
    # queries are short and keep the planner from proposing repeats, so they are kept
    # ahead of the claim listings, which are trimmed against each other
    monkeypatch.setattr(wf_mod, "PROMPT_BUDGET", 4 * len(listed))
    prompt = wf_mod._plan_prompt(Cfg(), st)

    def section(title):                               # one "# <title>" section's body
        return prompt.split("# " + title, 1)[1].split("\n# ", 1)[0]

    angles_s = section("Angles searched so far")
    for a in angles:                                  # every earlier angle, queries and all
        assert "- %s: %s" % (a["id"], a["angle"]) in angles_s
        assert a["queries"][0] in angles_s
    assert "more omitted" not in angles_s
    assert prompt.count("more omitted for length") == 2       # the two claim listings
    for title, total in (("Supported claims", 30), ("Refuted claims", 30)):
        body = section(title)
        kept = len(re.findall(r"^- C\d+: ", body, re.M))
        assert 0 < kept < total                               # both listings were trimmed
        m = re.fullmatch(r"\((\d+) more omitted for length\)", body.strip().splitlines()[-1])
        assert m and int(m.group(1)) == total - kept          # each counts its own omissions
