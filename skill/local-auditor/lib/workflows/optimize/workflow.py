"""optimize: make a test suite faster without changing what it checks, toward a target
the calling Claude session set in a bench file (--set bench=FILE; --set tests=CMD alone
times the whole suite with no focus and no target).

baseline: measure x2, guard x1, a profile of each (the runner's per-test timing when
runners.py knows the runner) -> bench check: B1-B4 (bench.py, no model) and the checker,
whose findings count only when their quote is found in the outputs it was shown. A failed
check or a surviving finding stops the run before round 1: exit 4, bench-review.md, and
bench-suggested.json, which is never run. Qwen never changes the bench.
-> rounds: profiler ideas (a path:line that exists, a profile line really in the profile,
inside focus) -> one sandbox per idea -> the engine measures each patch (a second run only
when the first beats the bar; a patch touching files outside focus is not measured) ->
reviewers vote keeps/weakens citing diff lines that must be in the patch -> an approved
patch joins the stack when the stack gets faster by more than the noise. Rounds end at the
target, when a round adds nothing, or at the preset's limit. From round 2 a recheck of
measure on the unchanged tree stops the run like a bench problem when it fails twice or
drifts past DRIFT x the noise twice; so does a round whose ideas are all outside focus.
-> guard once on the stack (patches dropped from the end until it passes) -> report.
--target is never written. Exit 0: a stack kept, guard passed, the target (if any)
reached; 4 otherwise, the report's first lines say why.
"""
import importlib.util
import json
import pathlib

from lib.swarm_engine import sandbox, steps


def _sibling(name):
    """runners.py / bench.py next to this file, loaded by path: workflow folders are not
    packages, and a copied folder must keep working."""
    path = pathlib.Path(__file__).resolve().parent / ("%s.py" % name)
    spec = importlib.util.spec_from_file_location("qla_optimize_%s" % name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runners = _sibling("runners")
benchlib = _sibling("bench")

TIMEOUT = 2400
PATCH_CLIP = 20000
DRIFT = 3
CHECK_BENCH = benchlib.default("check-tests")
FENCE = "```"

CHECK_P = """# Goal

{goal}

# The bench (written by the Claude session that started this run)

{fence}json
{bench}
{fence}

# The engine's checks (mechanical; do not re-judge them)

{checks}

# measure, twice on the unchanged tree: {walls}

# Profile of measure

{fence}
{pm}
{fence}

# Profile of guard

{fence}
{pg}
{fence}

# Your task

Is this bench fit to optimise against: does measure spend its time where focus points, can
the target be reached, does guard cover what measure runs? Report a problem only with
evidence: each finding's "evidence" is one line copied exactly from the outputs above.
Answer with:

{fence}json
{{"verdict": "ok|problem", "findings": [{{"claim": "one sentence", "evidence": "a line copied from above"}}], "suggested_bench": null}}
{fence}
("suggested_bench" may be a full bench object you would propose instead; it is shown to the
Claude session and never run.)
"""


def _text(v, n=500):
    return steps.clip(steps.as_text(v), n)


def _count(o):
    return "%s passed" % o["passed"] if o.get("passed") is not None else "pass count not checked"


def _one(o):
    if o["ok"]:
        return "passed (%s) in %.2fs" % (_count(o), o["wall"])
    return "FAILED (%s)" % benchlib.why(o)


def validate(cfg):
    for k in ("ideas", "voters"):
        if k in cfg and cfg[k] < 1:
            return "%s must be at least 1" % k
    if "voters" in cfg and "max_agents" in cfg and cfg["voters"] > cfg["max_agents"]:
        return "--max-agents must be at least %d (the voters per patch)" % cfg["voters"]
    if "min_gain" in cfg and not 0 <= cfg["min_gain"] < 1:
        return "min_gain must be in [0, 1)"
    if cfg.get("bench"):
        b, why = benchlib.load(cfg["bench"])
        return why if b is None else None
    if not (cfg.get("tests") or "").strip():
        return ("no bench given: --set bench=PATH/TO/bench.json (the local-swarm skill says how "
                "to write one), or --set tests=CMD to time the whole suite")
    return None


def _load_bench(wf):
    """The bench saved at the start (so a resume uses the same one), else the knob's."""
    saved = wf.load("bench")
    if isinstance(saved, dict) and "measure" in saved:
        return saved
    if wf.check is not None:
        b = dict(CHECK_BENCH)
    elif wf.knob("bench"):
        b, why = benchlib.load(wf.knob("bench"))
        if b is None:
            wf.fail(why)
    else:
        b = benchlib.default(wf.knob("tests"))
    wf.save("bench", b)
    return b


def _run(wf, cmd, label, patch=None):
    res = wf.steps.run_cmd(runners.wrap(cmd, label, bash=sandbox.bash_path()), patch=patch,
                           timeout=TIMEOUT)
    return runners.outcome(res)


def _profile(wf, cmd, base, label):
    """cmd with its runner's per-test timing flags, when the runner is known and that run
    passes; the plain baseline run otherwise."""
    flags = runners.PROFILE_FLAGS.get(base.get("runner"))
    if flags and base["ok"]:
        p = _run(wf, "%s %s" % (cmd, flags), label)
        if p["ok"]:
            return p
    return base


def _baseline(wf, b):
    m1 = _run(wf, b["measure"], "measure baseline 1")
    m2 = _run(wf, b["measure"], "measure baseline 2")
    g = _run(wf, b["guard"], "guard baseline")
    pm = _profile(wf, b["measure"], m1, "measure profile")
    pg = pm if b["guard"] == b["measure"] else _profile(wf, b["guard"], g, "guard profile")
    ok = m1["ok"] and m2["ok"] and m1["wall"] is not None and m2["wall"] is not None
    base = min(m1["wall"], m2["wall"]) if ok else None
    return {"m1": m1, "m2": m2, "g": g, "pm": pm, "pg": pg, "base": base,
            "noise": benchlib.noise(m1["wall"], m2["wall"], base) if ok else None,
            "passed": m1["passed"]}


def parse_check(texts):
    def parse(text):
        data = steps.extract_json(text)
        if not isinstance(data, dict) or data.get("verdict") not in ("ok", "problem"):
            raise ValueError('expected {"verdict": "ok" or "problem", "findings": [...]}')
        kept, dropped = [], []
        for f in data.get("findings") or []:
            if not isinstance(f, dict):
                continue
            row = {"claim": _text(f.get("claim"), 400), "evidence": _text(f.get("evidence"), 400)}
            (kept if benchlib.quote_found(row["evidence"], texts) else dropped).append(row)
        sug = data.get("suggested_bench")
        return {"verdict": data["verdict"], "findings": kept, "dropped": dropped,
                "suggested": sug if isinstance(sug, dict) else None}
    return parse


def _review_md(b, bl, results, chk):
    out = ["# Bench review", "", "## The bench", "", FENCE + "json", json.dumps(b, indent=1), FENCE, "",
           "## Baseline", "",
           "- measure run 1: %s" % _one(bl["m1"]),
           "- measure run 2: %s" % _one(bl["m2"]),
           "- guard: %s" % _one(bl["g"]),
           "- noise floor: %s" % ("%.2fs" % bl["noise"] if bl["noise"] is not None else "-"),
           "", "## Checks", ""]
    out += ["- %s %s: %s" % (r["id"], {True: "pass", False: "FAIL", None: "skipped"}[r["ok"]], r["detail"])
            for r in results]
    out += ["", "## Checker (%s)" % ("did not answer" if chk.get("failed") else chk["verdict"]), ""]
    out += ['- %s\n  evidence: "%s"' % (f["claim"], f["evidence"]) for f in chk["findings"]] or \
        ["- no finding with evidence"]
    if chk["dropped"]:
        out += ["", "Dropped (evidence not found in the outputs):", ""]
        out += ["- %s" % f["claim"] for f in chk["dropped"]]
    if chk.get("suggested"):
        out += ["", "A suggested bench is in bench-suggested.json. It was not run."]
    return "\n".join(out) + "\n"


def _bench_phase(wf, b, bl):
    """The one-line reason the bench is not fit, or None."""
    results = benchlib.checks(b, bl["m1"], bl["m2"], bl["g"], bl["pm"]["files"])
    texts = [bl[k]["profile"] for k in ("m1", "m2", "g", "pm", "pg")]
    walls = ", ".join("%.2fs" % o["wall"] if o["wall"] is not None else "-" for o in (bl["m1"], bl["m2"]))
    chk = wf.agent("bench-check", "checker", CHECK_P.format(
        goal=wf.goal, fence=FENCE, bench=json.dumps(b, indent=1),
        checks="\n".join("- %s %s: %s" % (r["id"], r["ok"], r["detail"]) for r in results),
        walls=walls, pm=bl["pm"]["profile"][-3500:], pg=bl["pg"]["profile"][-3500:]),
        parse_check(texts))
    if chk is None:
        chk = {"verdict": "ok", "findings": [], "dropped": [], "suggested": None, "failed": True}
    bad = [r for r in results if r["ok"] is False]
    reason = None
    if bad:
        reason = "%s: %s" % (bad[0]["id"], bad[0]["detail"])
    elif chk["verdict"] == "problem" and chk["findings"]:
        reason = "checker: %s" % chk["findings"][0]["claim"]
    wf.write("bench-review.md", _review_md(b, bl, results, chk))
    if chk.get("suggested"):
        wf.write("bench-suggested.json", json.dumps(chk["suggested"], indent=1) + "\n")
    wf.event("bench", verdict="problem" if reason else "ok", phase="start", reason=reason)
    return reason


def _dropout_report(reason, when, approved_ids):
    kept = (" The approved patches so far (%s) are in patches/." % ", ".join(approved_ids)
            if approved_ids else "")
    return ("bench: problem (see bench-review.md): %s\n\n# Optimize\n\nThe run stopped %s: the "
            "bench is not fit to optimise against.%s Read bench-review.md (and bench-suggested.json, "
            "a suggestion that was never run), fix the bench, and start a new run.\n"
            % (reason, when, kept))


def run(wf):
    b = _load_bench(wf)
    bl = _baseline(wf, b)
    reason = _bench_phase(wf, b, bl)
    if reason:
        wf.goal_unmet("bench problem: %s" % reason)
        wf.report(_dropout_report(reason, "before round 1", []))
        return
    _rounds_and_finish(wf, b, bl)


def _rounds_and_finish(wf, b, bl):
    """Replaced in Task 4."""
    wf.report("bench: ok\n\n# Optimize\n\nThe bench passed its checks (bench-review.md).\n")
