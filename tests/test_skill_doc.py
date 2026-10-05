"""SKILL.md is loaded on every trigger, so it must stay small, current and generic."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "local-auditor" / "SKILL.md"
REF = ROOT / "skill" / "local-auditor" / "reference"
DR_SKILL = ROOT / "skill" / "local-deep-research" / "SKILL.md"
DR_REF = REF / "deep-research.md"


def _skill():
    return SKILL.read_text(encoding="utf-8")


def test_skill_is_machine_agnostic():
    t = _skill()
    assert not re.search(r"\b[A-Za-z]:\\", t), "no Windows drive paths"
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+", t), "no hard-coded server address"


def test_skill_starts_with_preflight():
    assert "qwen-agent --preflight-only" in _skill()


def test_skill_is_not_bloated():
    n = len(_skill().splitlines())
    assert n < 200, f"SKILL.md is {n} lines; depth belongs in reference/"


def test_frontmatter_has_name_and_description():
    assert re.match(r"^---\nname: local-auditor\ndescription: .{200,}", _skill(), re.S)


def test_reference_files_exist_and_carry_the_depth():
    assert (REF / "sweep.md").exists() and (REF / "limits.md").exists()
    assert len((REF / "limits.md").read_text(encoding="utf-8").splitlines()) > 30


def test_limits_records_the_failopen_and_its_correction():
    t = (REF / "limits.md").read_text(encoding="utf-8").lower()
    assert "fail-open" in t and "prose_mask" in t
    assert "not a disposition" in t


def test_skill_lists_every_builder():
    t = _skill()
    for b in ("claims", "diff", "files", "logs", "history", "deviations"):
        assert b in t


FAMILY = ("local-agent", "local-coder", "local-auditor", "local-sweep", "local-deep-research",
          "local-swarm")


def test_every_family_skill_exists_and_is_small():
    for name in FAMILY:
        p = ROOT / "skill" / name / "SKILL.md"
        t = p.read_text(encoding="utf-8")
        assert re.match(r"^---\nname: %s\ndescription: .{120,}" % name, t, re.S), name
        assert len(t.splitlines()) < 200, name


def test_router_names_every_sub_skill_and_preflights():
    t = (ROOT / "skill" / "local-agent" / "SKILL.md").read_text(encoding="utf-8")
    assert "qwen-agent --preflight-only" in t
    for name in ("local-coder", "local-auditor", "local-sweep", "local-deep-research",
                 "local-swarm"):
        assert name in t


def test_router_documents_the_interactive_launch():
    # qwen-cc is the one job the router does itself, and the one place where a
    # permission prompt could be answered on the user's behalf.
    t = (ROOT / "skill" / "local-agent" / "SKILL.md").read_text(encoding="utf-8")
    for needle in ("Launch", "qwen-cc --peek", "qwen-cc --say", "qwen-cc --stop",
                   "attach:", "permission prompt"):
        assert needle in t, needle
    assert "never answer" in t.lower()


def test_coder_documents_until_done_and_exit_codes():
    t = (ROOT / "skill" / "local-coder" / "SKILL.md").read_text(encoding="utf-8")
    assert "--until-done" in t and "DEVIATION" in t
    for code in ("11", "12", "13", "14"):
        assert code in t


def test_deep_research_skill_documents_exit_codes_and_check():
    t = DR_SKILL.read_text(encoding="utf-8")
    assert "qwen-deep-research --check" in t and "--resume" in t
    for code in ("0", "2", "3", "4", "5", "130"):
        assert re.search(r"^\| %s \|" % code, t, re.M), "exit %s not in a table row" % code


def test_deep_research_reference_covers_setup():
    t = DR_REF.read_text(encoding="utf-8")
    for needle in ("QWEN_SEARCH_URL", "QWEN_SEARCH_KEY", "formats", "--max-agents", "--seats",
                   "run.log", "internet"):
        assert needle in t, needle


def test_deep_research_docs_track_interface():
    # --web-seats and the per-item --timeout budget replaced the old flat per-agent seconds;
    # the docs must say so and must not still quote 900 as that old flat default.
    t = DR_REF.read_text(encoding="utf-8")
    for needle in ("--web-seats", "QWEN_DR_WEB_SEATS", "per-item", "240"):
        assert needle in t, needle
    assert "default 900" not in t, "the reference still quotes the old flat per-agent default"
    s = DR_SKILL.read_text(encoding="utf-8")
    assert "--web-seats" in s
    assert re.search(r"^\| 8 \|", s, re.M), "exit 8 not in a table row"


def test_deep_research_docs_cover_retries_and_effort():
    # the depth presets grew retries and an overnight rung, and effort tuning joined them;
    # both reference and SKILL.md must document them
    t = DR_REF.read_text(encoding="utf-8")
    for needle in ("--retries", "--role-effort", "--effort", "overnight"):
        assert needle in t, needle
    s = DR_SKILL.read_text(encoding="utf-8")
    for needle in ("--retries", "--role-effort", "--effort", "overnight"):
        assert needle in s, needle


def test_deep_research_docs_bound_long_runs():
    # waves (--max-items), the 4 h cap on one unit's --timeout, and the retry rule
    # naming the exits that retrying can fix (QWEN_DR_BACKOFF waits out "exit 3 or 4")
    t = DR_REF.read_text(encoding="utf-8")
    for needle in ("--max-items", "QWEN_DR_MAX_ITEMS", "QWEN_DR_MAX_UNIT_SECONDS",
                   "exit 3 or 4"):
        assert needle in t, needle
    assert "14400" in t or "4 h" in t, "the per-unit timeout cap is not documented"
    assert "--max-items" in DR_SKILL.read_text(encoding="utf-8")


def test_deep_research_docs_cover_the_deadline():
    # the run deadline (--hours) joined the caps, the backoff text names the exits it
    # waits out, and every --role-effort metavar spells the full form
    t = DR_REF.read_text(encoding="utf-8")
    for needle in ("--hours", "exit 3 or 4", "ROLE=LEVEL[,ROLE=LEVEL...]",
                   "seconds before re-spawning an agent after a server error (exit 3 or 4)",
                   "Each agent is capped at 4 h; --hours bounds the whole run."):
        assert needle in t, needle
    s = DR_SKILL.read_text(encoding="utf-8")
    assert "--hours" in s


def test_deep_research_docs_no_longer_halve_the_web_seats():
    # --web-seats now defaults to --seats: web agents call one tool at a time, and the
    # docs must not still tell anyone to run the web phases at half of --seats.
    for p in (DR_REF, DR_SKILL):
        assert "half of" not in p.read_text(encoding="utf-8"), p


def _code_blocks(text):
    """The fenced blocks of a markdown file, in order."""
    blocks, cur, inside = [], [], False
    for line in text.splitlines():
        if line.startswith("```"):
            if inside:
                blocks.append("\n".join(cur))
                cur = []
            inside = not inside
        elif inside:
            cur.append(line)
    return blocks


def test_searxng_snippet_is_valid_yaml_merge():
    # settings.yml is generated with use_default_settings and server: already in it, so the
    # snippet must merge into that file, not show a second server: key beside search:.
    t = DR_REF.read_text(encoding="utf-8")
    assert "under the existing" in t
    assert "docker logs" in t
    for block in _code_blocks(t):
        if "search:" in block:
            assert not re.search(r"^\s*server:\s*$", block, re.M), (
                "a block that adds search: also shows a server: line; the generated "
                "settings.yml already has one"
            )


def test_reference_does_not_claim_measured_slots():
    t = DR_REF.read_text(encoding="utf-8")
    assert "slot count the README measures" not in t


def test_skill_guides_each_outcome():
    t = DR_SKILL.read_text(encoding="utf-8")
    for needle in ("run.log", "--resume", "--stdin", "codebase"):
        assert needle in t, needle


SW_SKILL = ROOT / "skill" / "local-swarm" / "SKILL.md"
SW_REF = REF / "swarm.md"


def test_swarm_skill_documents_the_loop_and_exit_codes():
    t = SW_SKILL.read_text(encoding="utf-8")
    for needle in ("qwen-swarm --list", "qwen-swarm --check", "--resume", "--target",
                   "--set repro=", "reference/swarm.md", "run.log", "run_in_background"):
        assert needle in t, needle
    for code in ("0", "2", "3", "4", "5", "8", "130"):
        assert re.search(r"^\| %s \|" % code, t, re.M), "exit %s not in a table row" % code


def test_swarm_reference_covers_the_api_and_the_fences():
    t = SW_REF.read_text(encoding="utf-8")
    for needle in ("wf.agent", "wf.fan_out", "wf.vote", "wf.rounds()", "wf.converged",
                   "wf.steps.run_cmd", "wf.save", "wf.report", "wf.fail", "wf.goal_unmet",
                   "validate(cfg)", "check.py", "--keep-sandboxes", "stop_reason"):
        assert needle in t, needle
    for fence in ("`none`", "`search`", "`web`", "`read`", "`sandbox`"):
        assert fence in t, fence


def test_swarm_reference_matches_the_debug_manifest():
    import json
    m = json.loads((ROOT / "skill" / "local-auditor" / "lib" / "workflows" / "debug"
                    / "workflow.json").read_text(encoding="utf-8"))
    t = SW_REF.read_text(encoding="utf-8")
    for depth, p in m["presets"].items():
        rounds = ("until, %g h" % p["hours"]) if p["rounds"] == "until" else str(p["rounds"])
        row = "| %s | %d | %d | %d | %d | %s |" % (depth, p["hypotheses"], p["voters"],
                                                  p["budget"], p["retries"], rounds)
        assert row in t, row


def _qwen_swarm_examples(doc):
    """Every `qwen-swarm ...` example a page shows, a wrapped one joined into a single
    line (a synopsis or example continues on the indented `[...]` lines under it)."""
    lines = doc.read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(lines):
        s = line.strip().strip("`").strip("$ ")
        if not s.startswith("qwen-swarm "):
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip().startswith("["):
            s += " " + lines[j].strip().strip("`")
            j += 1
        out.append(s)
    return out


def test_swarm_examples_put_the_run_folder_outside_the_target():
    # a run folder inside --target is refused (exit 2): an example that passes --target
    # without saying where the run goes is an example that does not run
    for doc in (README, SW_SKILL, SW_REF):
        for ex in _qwen_swarm_examples(doc):
            if "--target" in ex:
                assert "--out" in ex, "%s: %s" % (doc.name, ex)
    assert "outside the target" in SW_REF.read_text(encoding="utf-8")


def test_deep_research_docs_list_the_stop_reasons_the_engine_writes():
    t = DR_REF.read_text(encoding="utf-8")
    for needle in ("`rounds`", "`hours`", "`deadline`", "`converged: <reason>`",
                   "the planner produced no plan", "the planner found no new angle",
                   "added no supported claim", "units dropped in round"):
        assert needle in t, needle


def test_deep_research_docs_cover_rounds():
    t = DR_REF.read_text(encoding="utf-8")
    for needle in ("planner", "--rounds", "round-2/", "report-round-", "qwen-swarm"):
        assert needle in t, needle
    assert "planner" in DR_SKILL.read_text(encoding="utf-8")


README = ROOT / "README.md"
CONFIG_REF = REF / "configuration.md"
INSTALL = ROOT / "install.sh"
# the counts are prose in the pages a reader sees, so they go stale one skill at a time;
# install.sh is the only thing that knows what it installs
NUMBER_WORDS = "one two three four five six seven eight nine ten eleven twelve".split()


def _installs():
    """(skill names, command names) install.sh installs, read out of the installer."""
    t = INSTALL.read_text(encoding="utf-8")
    skills = re.search(r'SKILLS="([^"]*)"', t).group(1).split()
    commands = re.findall(r"^forwarder\s+(\S+)", t, re.M)
    assert skills and commands, "install.sh names nothing it installs"
    return skills, commands


def _number_word(n):
    assert 0 < n <= len(NUMBER_WORDS), "install.sh installs %d; no word for that count" % n
    return NUMBER_WORDS[n - 1]


def test_readme_and_configuration_count_what_install_sh_installs():
    skills, commands = _installs()
    stated = {"skills": _number_word(len(skills)), "commands": _number_word(len(commands))}
    for doc in (README, CONFIG_REF):
        t = doc.read_text(encoding="utf-8")
        for thing, word in stated.items():
            assert "%s %s" % (word, thing) in t, \
                "%s never says it installs %s %s" % (doc.name, word, thing)
            stale = [w for w in NUMBER_WORDS if w != word and "%s %s" % (w, thing) in t]
            assert not stale, "%s still says '%s %s'" % (doc.name, stale[0], thing)
        for name in skills + commands:
            assert name in t, "%s never names %s" % (doc.name, name)
