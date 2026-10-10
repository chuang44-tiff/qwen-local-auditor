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
    # cmd checks run without a shell: both pages must name the way to keep a shell
    assert "sh -c" in t, "local-coder SKILL.md never says to wrap a shell command in sh -c"
    assert "sh -c" in (REF / "coding.md").read_text(encoding="utf-8"), "coding.md never says it"


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
                   "wf.browser_dir", "validate(cfg)", "check.py", "--keep-sandboxes",
                   "stop_reason"):
        assert needle in t, needle
    for fence in ("`none`", "`browser`", "`search`", "`web`", "`read`", "`sandbox`"):
        assert fence in t, fence
    # the browser fence's own page: where its evidence goes, and that it is not a web fence
    for needle in ("QWEN_BROWSER_DIR", "RUN/browser/<unit>", "--seats"):
        assert needle in t, needle


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


DEPTH_SWITCHES = ("--role-variant", "--subagents-nudge", "--review-round", "--probe",
                  "--keep-sandbox", "--probe-here", "--deep")


def test_qwen_agent_help_and_reference_name_every_depth_switch():
    import os
    import shutil
    import subprocess
    agent = ROOT / "skill" / "local-auditor" / "qwen-agent.sh"
    bash = os.environ.get("TEST_BASH") or shutil.which("bash")
    help_text = subprocess.run([bash, str(agent).replace("\\", "/"), "--help"],
                               capture_output=True, encoding="utf-8").stdout
    ref = (REF / "qwen-agent.md").read_text(encoding="utf-8")
    for s in DEPTH_SWITCHES:
        assert re.search(r"^\s+%s\b" % re.escape(s), help_text, re.M), s
        assert "`%s" % s in ref, s
    assert '"qwen_agent": {' in ref and "QWEN_PROBE_DIR" in ref


def test_coding_and_swarm_references_cover_the_depth_switches():
    coding = (REF / "coding.md").read_text(encoding="utf-8")
    for needle in ("--review-round", "--probe", "RUN/probe.patch", "patch: PATH", "switches:"):
        assert needle in coding, needle
    t = SW_REF.read_text(encoding="utf-8")
    for needle in ("`roles.R.deep`", '"review_round"', '"subagents"', "--deep ROLE[,ROLE...]|all"):
        assert needle in t, needle


def _agent_help():
    import os
    import shutil
    import subprocess
    agent = ROOT / "skill" / "local-auditor" / "qwen-agent.sh"
    bash = os.environ.get("TEST_BASH") or shutil.which("bash")
    return subprocess.run([bash, str(agent).replace("\\", "/"), "--help"],
                          capture_output=True, encoding="utf-8").stdout


def _flat(text):
    return " ".join(text.split())


def test_safety_docs_describe_the_default_depth():
    # Wherever a bare run is described, the sandbox shell of default depth and the way
    # back to the strict fence are named with it.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    limits = (REF / "limits.md").read_text(encoding="utf-8")
    help_text = _agent_help()
    safety = help_text[help_text.index("SAFETY"):]
    for name, text in (("README", readme), ("limits.md", limits), ("--help SAFETY", safety)):
        flat = _flat(text)
        assert "sandbox" in flat, name
        assert "--shallow" in flat, name
        assert "not a jail" in flat or "not confined" in flat, name
    assert "Depth is the default" in readme and "3600" in readme
    assert "the model has no Bash" not in _flat(safety).split("--shallow")[0]
    assert "can only read" not in _flat(readme)


def test_record_and_replay_disclose_that_scripts_run_as_you():
    help_text = _flat(_agent_help())
    ref = _flat((REF / "qwen-agent.md").read_text(encoding="utf-8"))
    skill = _flat(_skill())
    for name, text in (("--help", help_text), ("qwen-agent.md", ref), ("SKILL.md", skill)):
        assert "as you" in text or "as the user" in text, name
        assert "unsandboxed" in text, name
        assert "same result every time" not in text, name
    assert "Nothing reaches the network" not in ref


def test_swarm_skill_watches_events_and_records_verdicts():
    # step 3 watches RUN/events.jsonl with the Monitor tool instead of polling run.log, and
    # step 4 hands the session the final say on ui-test's failures
    t = _flat(SW_SKILL.read_text(encoding="utf-8"))
    for needle in ("qwen-swarm: run folder:", "events.jsonl", "Monitor", "timeout_ms",
                   "TaskStop", "re-arm", "qwen-swarm --record-verdict", "CONFIRMED",
                   "FALSE_ALARM", "NEEDS_HUMAN", "--set confirm=local", "--set confirm=none",
                   "fixtures: DIR", "subagent", "never on the", "trips the breaker",
                   "confirmed failure", "false alarm on a tester FAIL"):
        assert needle in t, needle
    assert "While it runs, read `RUN/run.log`" not in t, "the old run.log poll is still there"
    assert "run folder: RUN" in t and "run_end" in t and "exit code" in t


def _watch_recipe():
    """The Monitor command step 3 of local-swarm prints, as one bash script."""
    import textwrap
    lines = SW_SKILL.read_text(encoding="utf-8").splitlines()
    start = next(i for i, x in enumerate(lines) if "RUN=/abs/run/folder; n=N" in x)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "done")
    return textwrap.dedent("\n".join(lines[start:end + 1]))


def test_swarm_skill_watch_recipe_runs(tmp_path):
    # the recipe as printed: whole lines only, attention/unit_dropped/run_end with their
    # line numbers, exit after run_end, and a re-arm from N sees only what came after N
    import os
    import shutil
    import subprocess
    bash = os.environ.get("TEST_BASH") or shutil.which("bash")
    run = tmp_path / "run"
    run.mkdir()
    events = ['{"kind": "run_start", "t": 1}', '{"kind": "attention", "item": "s2", "t": 2}',
              '{"kind": "unit_done", "unit": "scenario-1", "t": 3}',
              '{"kind": "unit_dropped", "unit": "scenario-2", "t": 4}',
              '{"kind": "run_end", "exit": 4, "t": 5}']
    (run / "events.jsonl").write_bytes(("\n".join(events) + "\n").encode("utf-8"))

    def watch(n):
        script = _watch_recipe().replace("RUN=/abs/run/folder; n=N",
                                         'RUN="%s"; n=%d' % (run.as_posix(), n))
        return subprocess.run([bash, "-c", script], capture_output=True, encoding="utf-8",
                              timeout=30).stdout.splitlines()

    assert watch(0) == ["2: " + events[1], "4: " + events[3], "5: " + events[4]]
    assert watch(4) == ["5: " + events[4]]


def test_swarm_skill_watch_recipe_survives_a_resume(tmp_path):
    # a --resume appends to the SAME events.jsonl: the doc's rule is that N is the line
    # count taken before the resume starts -- armed like that the recipe prints only the
    # resume's new lines and exits on the NEW run_end (armed at 0 it would replay the
    # previous run's attention lines and exit on its old run_end)
    import os
    import shutil
    import subprocess
    assert "line count" in _flat(SW_SKILL.read_text(encoding="utf-8"))
    bash = os.environ.get("TEST_BASH") or shutil.which("bash")
    run = tmp_path / "run"
    run.mkdir()
    events = run / "events.jsonl"
    old = ['{"kind": "run_start", "resumed": false, "t": 1}',
           '{"kind": "attention", "item": "s2", "t": 2}',
           '{"kind": "run_end", "exit": 4, "t": 3}']
    new = ['{"kind": "run_start", "resumed": true, "t": 4}',
           '{"kind": "attention", "item": "s1", "t": 5}',
           '{"kind": "run_end", "exit": 0, "t": 6}']
    events.write_text("\n".join(old) + "\n", encoding="utf-8")
    n = len(old)                                          # the line count, BEFORE the resume
    with events.open("a", encoding="utf-8") as fh:        # ... and then the resume runs
        fh.write("\n".join(new) + "\n")

    def watch(start):
        script = _watch_recipe().replace("RUN=/abs/run/folder; n=N",
                                         'RUN="%s"; n=%d' % (run.as_posix(), start))
        return subprocess.run([bash, "-c", script], capture_output=True, encoding="utf-8",
                              timeout=30).stdout.splitlines()

    assert watch(n) == ["5: " + new[1], "6: " + new[2]]   # only the resume's events
    assert watch(0)[:2] == ["2: " + old[1], "3: " + old[2]]   # armed at 0 the PREVIOUS
    # run's attention line is replayed to the session as if it had just happened: the trap
    # the line-count rule avoids


def test_docs_state_the_build_time_rules():
    # the rules this build settled, where a session will read them: swarm.md, and one
    # line each in the local-swarm SKILL.md
    t = _flat(SW_REF.read_text(encoding="utf-8"))
    for needle in ("advisory", "points outside", "non-secret", "saved but not applied"):
        assert needle in t, needle
    s = _flat(SW_SKILL.read_text(encoding="utf-8"))
    for needle in ("advisory", "non-secret", "saved but not applied", "fail only that row"):
        assert needle in s, needle


def test_swarm_reference_covers_claude_check_events_and_verdicts():
    t = _flat(SW_REF.read_text(encoding="utf-8"))
    for needle in ("wf.claude_check", "`ok`", "`failed`", "`unavailable`", "`over_cap`",
                   "`deadline`", "RUN/claude/calls.jsonl", "claude_calls", "claude_cost_usd",
                   "QWEN_EXEC_RETRY_BACKOFF", "wf.event", "RUN/events.jsonl", "`run_start`",
                   "`unit_done`", "`unit_dropped`", "`claude_call`", "`run_end`", "`attention`",
                   "qwen-swarm: run folder:", "RUN/.lock", "run is live", "--record-verdict",
                   "RUN/verdicts/<ID>.json", "apply_verdicts", "render(final, rows)",
                   "exit_for", "notice(cfg)", "`browser-probe`", "stage=DIR", "repair=TEXT",
                   "unit_ids=True", "confirm_model", "confirm_max", "CONFIRMED", "FALSE_ALARM",
                   "NEEDS_HUMAN", "fixtures: DIR", "200 MB", "--set confirm=local",
                   "--set confirm=none", "leave this machine", "## False alarms",
                   "Session verdicts", "no result block after repair",
                   "anything that can write the run folder", "confirmer unavailable:",
                   "confirm=claude:", "`scored`", "`verdict`", "needs human",
                   "confirmed failure", "false alarm on a tester FAIL",
                   "No confirm pass ran", "not on `run_end.exit`", "connection error",
                   "(a verdict does not clear a drop)"):
        assert needle in t, needle


def test_readme_safety_discloses_the_ui_test_confirm_pass():
    # The ui-test confirm pass is ON by default and hands evidence to Claude; the README's
    # Safety section is where a reader should learn the default and the two ways out of it.
    t = README.read_text(encoding="utf-8")
    safety = _flat(t[t.index("## Safety"):t.index("## Benchmark")])
    for needle in ("confirm=claude", "--set confirm=local", "--set confirm=none",
                   "FAIL/BLOCKED", "screenshots", "login"):
        assert needle in safety, needle


def test_advisor_section_names_the_ui_test_exception():
    ref = (REF / "qwen-agent.md").read_text(encoding="utf-8")
    advisor = ref[ref.index("## Advisor"):ref.index("## Model, context and effort")]
    for needle in ("ui-test", "on by default", "--set confirm=local", "--set confirm=none"):
        assert needle in advisor, needle
    assert "`fixtures: <dir>`" in ref


def test_coder_docs_need_the_test_command_only_for_test_checks():
    line = "QWEN_TEST_CMD is needed only when the checklist has `test` checks"
    for p in (REF / "coding.md", ROOT / "skill" / "local-coder" / "SKILL.md"):
        t = _flat(p.read_text(encoding="utf-8"))
        assert line in t, p.name
        assert "--test-no-cmd" in t, p.name
        # the precise condition: no `test` check AND no command configured
        assert "no check is a `test` check and QWEN_TEST_CMD is unset" in t, p.name
