"""The ui-test workflow: a scripted UI suite run as a swarm, one browser agent per scenario.

The suite is the `scenarios` knob (`--set scenarios=PATH`): the same "# Suite:" markdown
`qwen-agent --scenarios` runs (reference/qwen-agent.md, "Scripted UI suites"), and the goal
names the run. Its scenarios are dealt ONE PER AGENT to `browser`-fenced tester units, each
driving a real browser (qwen-agent --browser, headless) through its own scenario.
lib/scenarios.py builds every prompt and scores every answer, so what a tester was handed and
what its answer is judged against are one description of one job and cannot drift apart. The
report is the suite's own result table plus where each unit's screenshots and page snapshots
live. A scenario whose unit failed is BLOCKED with that unit's own reason; a scenario the
deadline kept from starting is NOT RUN -- counted apart from the blocked ones, given no
browser folder (its unit never made one), and named for what will run it: a --resume.

Exits: 0 every scenario passed; 4 any scenario ended FAIL, BLOCKED or NOT RUN (a suite the
deadline only stopped, every unfinished scenario NOT RUN, counts as much as any failure), or
an agent was dropped; 2 no `--set scenarios=`, or a file that is
unreadable or does not parse -- before a run folder exists or an agent starts, exactly as
`qwen-agent --scenarios` validates its file first; 5 run(wf) could not get a suite at all and
wrote no report, which is a --check dry run (its preset knob is empty) or a resume whose
`suite.json` and scenario file are both gone.

`qwen-swarm --check ui-test` never calls validate(), so its dry run ends at that empty knob
with a clean, empty call list: the manifest is what it validates here, and the fake-agent run
of this workflow is tests/test_ui_test_workflow.py.
"""
import pathlib

from lib import scenarios

UNIT = "scenario"            # the unit name stem: scenario-1, scenario-2, ... one per scenario
NOT_RUN = "NOT RUN"          # this workflow's fourth status: the deadline kept the unit from
                             # starting -- no session, no browser folder, --resume runs it


def _suite_text(path):
    """(suite, None) for the scenario file `path` names, or (None, the whole reason it was
    refused): a file that is not there, is not UTF-8 text, or is refused by `scenarios.parse`
    with a message naming its line."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:        # as scenarios.py reads it
            text = fh.read()
    except UnicodeDecodeError as e:
        return None, "%s is not UTF-8 text (%s)" % (path, e)
    except OSError as e:
        return None, "cannot read %s (%s)" % (path, e.strerror or e)
    try:
        return scenarios.parse(text), None
    except scenarios.ScenarioError as e:
        return None, "%s: %s" % (path, e)


def validate(cfg):
    """The file is read and parsed BEFORE the run, as qwen-agent's own --scenarios does: no
    knob, or a suite that does not parse, is exit 2 naming the line, with no run folder and
    no agent started. A dry run (--check) never calls this: its cfg carries the preset's
    empty knob, and run(wf) ends there (`wf.fail`)."""
    if not cfg.get("scenarios"):
        return ("no scenario file given: --set scenarios=PATH/TO/SUITE.md (the '# Suite:'"
                " markdown qwen-agent --scenarios takes)")
    _, why = _suite_text(cfg["scenarios"])
    return why


def _absolute_scenarios(wf):
    """Store and print the `scenarios` knob as an absolute path. A relative
    `--set scenarios=PATH` is otherwise stored and printed as given: config.json would name
    a file whose location depends on the cwd the command happened to run in, and a --resume
    from another cwd could not find it again once suite.json is gone (the exit-5 path). The
    engine wrote config.json before run(wf) with the raw string, so the correction is made
    here: the cwd is the one validate() opened the file through (the engine never chdirs),
    wf.cfg holds the very dict config.json was rendered from, and wf.save writes it back in
    the runner's own bytes. Every later read -- _suite's open, the report's `Scenario file:`
    line, a resume's config -- then gets the same absolute path. A knob already absolute
    (every resume of a fixed run) changes and rewrites nothing, so the dry run's empty knob
    and a plain run's call sequence are untouched."""
    path = wf.knob("scenarios")
    if not path:
        return
    resolved = str(pathlib.Path(path).resolve())
    if resolved == path:
        return
    wf.cfg["scenarios"] = resolved
    knobs = (wf.cfg.get("summary") or {}).get("knobs")   # the summary block config.json echoes
    if isinstance(knobs, dict):
        knobs["scenarios"] = resolved
    wf.save("config", wf.cfg)


def _suite(wf):
    """The suite to run: the `suite` artifact of a run that already read the file -- so a
    --resume replays the very same scenarios against the cache, whatever has happened to the
    file since -- else the file the `scenarios` knob names (absolute since
    _absolute_scenarios), read once and saved as that artifact."""
    suite = wf.load("suite")
    if suite is not None:
        return suite
    path = wf.knob("scenarios")
    if not path:
        wf.fail("no scenario file given: --set scenarios=PATH/TO/SUITE.md")
    suite, why = _suite_text(path)
    if suite is None:
        wf.fail("cannot run the scenario file: %s" % why)     # exit 5; a fresh run refused it at 2
    wf.save("suite", suite)
    return suite


def _batch_suite(suite, base, batch):
    """One batch as a whole suite, in the module's own shape: the same value builds the
    tester's prompt and scores its answer. The `base` knob wins over the file's `base:` line,
    which is how one suite runs against a staging deployment."""
    return {"suite": suite["suite"], "base": base, "scenarios": list(batch)}


def _scored(items, res):
    """One row per scenario, in file order, each with the `unit` that ran it (null when none
    did). `max_items=1` deals one scenario per unit, so `res.units` holds the units of the
    waves that started, in item order: a unit that failed carries its reason, a unit the
    deadline reached before it started carries `deadline` and started nothing at all, and a
    wave the deadline stops before it is dealt starts no units of its own -- its scenarios
    are the suffix of the file with no unit entry. A scenario whose unit failed is BLOCKED
    with exactly that unit's reason; one the deadline kept from starting is NOT RUN: no
    session, no browser folder, and a --resume runs it."""
    by_id = {row["id"]: row for row in res.rows}
    out = []
    for n, sc in enumerate(items):
        unit = res.units[n] if n < len(res.units) else None
        started = unit is not None and not unit.get("deadline")
        row = by_id.get(sc["id"])
        if row is None:                 # its unit never answered usefully at all
            row = {"id": sc["id"], "status": "BLOCKED" if started else NOT_RUN,
                   "failed_expectations": [], "evidence": [],
                   "notes": "agent failed: %s" % unit["why"] if started
                            else "deadline: not started; --resume runs it"}
        out.append(dict(row, unit=unit["name"] if started else None))
    return out


def _detail(row):
    """What the tester said did not hold, and the evidence it named for it -- every row's, not
    only the ones that failed: what a PASS rests on belongs in the report too. A row scored
    from an answer with no report in it carries neither key: scenarios.py emits only
    id/status/notes for those."""
    return "".join("\n  did not hold: %s" % e for e in row.get("failed_expectations") or []) \
        + "".join("\n  evidence: %s" % e for e in row.get("evidence") or [])


def _report(wf, suite, base, scored):
    titles = {sc["id"]: sc["title"] for sc in suite["scenarios"]}
    counts = {s: sum(1 for r in scored if r["status"] == s)
              for s in scenarios.STATUSES + (NOT_RUN,)}
    table = scenarios.summary(scored).splitlines()
    # the table is scenarios.py's own -- NOT RUN rows ride in it like any other status;
    # only its count line moves, because the module scores three statuses and this
    # workflow's fourth one must not be silently missing from the totals
    table[-1] = "PASS %d / FAIL %d / BLOCKED %d / NOT RUN %d" % (
        counts["PASS"], counts["FAIL"], counts["BLOCKED"], counts[NOT_RUN])
    parts = ["# UI test: %s\n" % suite["suite"],
             "Scenario file: `%s`\n" % wf.knob("scenarios"),
             "Base URL: %s\n" % ("`%s`" % base if base else
                                 "*none: the file has no `base:` line and no `--set base=` "
                                 "was given, so an `open` step needs a full URL*"),
             "\n".join(table).rstrip() + "\n"]
    parts.append("## Evidence\n\nEvery browser agent was given one folder of its own under "
                 "the run, and qwen-agent makes a fresh timestamped folder inside it for "
                 "every call it runs (a retry or a repair round gets one of its own): that is "
                 "where its screenshots and page snapshots are. Under each scenario, what its "
                 "tester named as its evidence and what did not hold -- PASS included, since "
                 "that is what a PASS rests on.\n\n" + "\n".join(
                     "- `%s` %s -- %s -- %s%s" % (
                         r["id"], titles.get(r["id"], ""), r["status"],
                         "`%s`" % wf.browser_dir(r["unit"]) if r["unit"] else "*no unit ran it*",
                         _detail(r))
                     for r in scored) + "\n")
    bad = [r for r in scored if r["status"] != "PASS"]
    if bad:
        parts.append("## What did not pass\n\n" + "\n".join(
            "- **%s** %s (%s): %s" % (r["id"], titles.get(r["id"], ""), r["status"],
                                       r["notes"] or "no note given")
            for r in bad) + "\n")
    cum = wf.totals()
    stats = [("scenarios", len(scored)), ("passed", counts["PASS"]), ("failed", counts["FAIL"]),
             ("blocked", counts["BLOCKED"]), ("not run", counts[NOT_RUN]),
             ("agents run", cum["agents_run"]),
             ("units dropped", wf.dropped), ("stopped at deadline", "yes" if wf.not_run else "no"),
             ("tokens", cum["tokens"]), ("invocations", cum["invocations"]),
             ("wall time", "%dm%02ds" % (cum["seconds"] // 60, cum["seconds"] % 60))]
    parts.append("## Run\n\n| | |\n|---|---|\n" + "\n".join("| %s | %s |" % s for s in stats) + "\n")
    wf.report("\n".join(parts))


def run(wf):
    _absolute_scenarios(wf)         # store and print the scenario file by absolute path
    suite = _suite(wf)
    items = suite["scenarios"]
    base = wf.knob("base") or suite.get("base")
    wf.log("suite '%s': %d scenarios, base %s" % (
        suite["suite"], len(items), base or "(none)"))

    def result_row(text, batch):
        """The unit's answer scored: one row, because `max_items=1` gives every unit exactly
        one scenario and `scenarios.results` returns one row per scenario of its suite."""
        return [scenarios.results(text, _batch_suite(suite, base, batch))[0]]

    res = wf.fan_out(UNIT, "tester", items,
                     lambda batch: scenarios.prompt(_batch_suite(suite, base, batch)),
                     result_row, item_id=lambda sc: sc["id"], max_items=1)
    scored = _scored(items, res)
    wf.save("results", scored)
    bad = [r for r in scored if r["status"] != "PASS"]
    for r in bad:
        wf.log("%s: %s -- %s" % (r["id"], r["status"], r["notes"] or "(no note)"))
    if bad:
        # which ones, and why, is what the lines above and the report are for
        wf.goal_unmet("%d of %d scenarios did not pass" % (len(bad), len(scored)))
    _report(wf, suite, base, scored)
