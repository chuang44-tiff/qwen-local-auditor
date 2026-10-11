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
import re

from lib.swarm_engine import sandbox, steps
from lib.swarm_engine.patches import patch_paths


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


PROFILE_P = """# Goal

{goal}

# The bench

{bench}

Baseline: {passed} in {base:.2f}s (best of two runs; noise {noise:.2f}s).

# Profile of measure

{fence}
{profile}
{fence}

# Earlier ideas (idea -> what the engine measured)

{history}

# Your task

Propose exactly {n} new, distinct ideas with the largest total saving, structural ones
included, each inside focus. "location" is a real path:line; "profile_line" is one line of
the profile above, copied exactly. You may also name one place OUTSIDE focus that the
profile shows is slow (it goes in the report; nobody implements it). Answer with:

{fence}json
{{"ideas": [{{"location": "path:line", "profile_line": "a line copied from the profile", "change": "what to change, concretely", "why": "where the time goes", "saving": "expected seconds saved"}}], "outside_focus": null}}
{fence}
("outside_focus" is null or {{"location": "path:line", "profile_line": "...", "why": "..."}})
"""

IMPL_P = """# Goal

{goal}

# The bench

{bench}

Baseline: {passed} in {base:.2f}s.

# Your idea

- {id}: at {location}: {change}
  Why: {why}   Expected saving: {saving}

Implement it fully in your working directory, only in files inside focus, run the affected
tests, and answer with:

{fence}json
{{"id": "{id}", "done": true, "summary": "what you changed", "note": "anything the reviewer should know"}}
{fence}
(set "done" to false and undo your edits when the idea does not work)
"""

REVIEW_P = """# Goal

{goal}

# What must not change

{keep}

# The measured command

{measure}

# Patches that made it faster (each already passes the command, with the same pass count)

{items}

# Your task

For each patch: does it keep what the tests check? Cite the diff lines your verdict rests on,
copied exactly from the patch. A patch whose summary or diff loosens, skips, removes or widens
a check, a threshold, a target or an expected value is "weakens": cite the diff line that
makes that change, and quote the summary's own words in "reason". Answer with:

{fence}json
[{{"patch": "P1", "verdict": "keeps|weakens|unclear", "lines": ["a diff line copied from the patch"], "reason": "one sentence"}}]
{fence}
"""

WRITE_P = """# Goal

{goal}

# Facts (engine-measured; use only these)

{facts}

Write the report: what was measured, the stacked result, each kept patch with its saving,
the others briefly with why they did not qualify, any note about a slow place outside
focus, and what to check before applying patches/stacked.diff.
"""
# The writer's answer IS the report, so its repair round asks for the whole report --
# not for a json block, which is what the swarm's default REPAIR asks for. A fence
# "none" role has no tools: a review round once told it to re-check with probes it does
# not have, and it answered with the tool call itself; the repair says not to.
WRITE_REPAIR = ("Your last answer could not be used: {why}\n\n"
                "Reply again with ONLY the complete markdown report, following the "
                "format you were given. Do not answer with a tool call: you have no "
                "tools, and the report itself is the answer.")


def _bench_text(b):
    return "\n".join("- %s: %s" % (k, b[k] if k != "focus" else (", ".join(b[k]) or "(whole target)"))
                     for k in ("measure", "guard", "focus", "target", "keep"))


def _idea_problem(idea, root, focus, texts):
    if not benchlib.quote_found(idea["profile_line"], texts):
        return "profile_line not found in the profile"
    if root is not None:
        path, why = benchlib.location_ok(root, idea["location"])
        if path is None:
            return why
    else:
        path = benchlib.norm_path(idea["location"].split(":")[0])
    if not benchlib.in_focus(path, focus):
        return "outside focus: %s" % path
    return None


def parse_ideas(n, seen, root, focus, texts):
    def parse(text, batch=None):
        data = steps.extract_json(text)
        if isinstance(data, list):
            data = {"ideas": data}
        if not isinstance(data, dict) or not isinstance(data.get("ideas"), list):
            raise ValueError('expected {"ideas": [...], "outside_focus": null}')
        ideas, dropped = [], []
        for d in data["ideas"]:
            if not isinstance(d, dict):
                continue
            idea = {"location": _text(d.get("location"), 200), "profile_line": _text(d.get("profile_line"), 300),
                    "change": _text(d.get("change"), 1200), "why": _text(d.get("why"), 800),
                    "saving": _text(d.get("saving"), 100)}
            key = (idea["location"].lower(), idea["change"].lower())
            if not idea["change"] or key in seen or key in {(i["location"].lower(), i["change"].lower())
                                                            for i in ideas + dropped}:
                continue
            why = _idea_problem(idea, root, focus, texts)
            if why:
                dropped.append(dict(idea, why=why))
            else:
                ideas.append(idea)
        if data["ideas"] and not ideas and not dropped:
            raise ValueError("no usable new idea (each needs a location and a change, not repeating earlier ones)")
        bad = [d["why"] for d in dropped if not d["why"].startswith("outside focus")]
        if not ideas and bad:
            # one round of slightly misquoted ideas is a slip, not the profiler running
            # dry: raise so the swarm's repair round re-asks, with these reasons to fix
            raise ValueError("every idea was dropped: %s" % "; ".join(bad[:3]))
        note = data.get("outside_focus")
        out = None
        if isinstance(note, dict):
            n_ = {"location": _text(note.get("location"), 200), "profile_line": _text(note.get("profile_line"), 300),
                  "why": _text(note.get("why"), 500)}
            real = benchlib.quote_found(n_["profile_line"], texts) and (
                root is None or benchlib.location_ok(root, n_["location"])[0] is not None)
            if real and not benchlib.in_focus(n_["location"].split(":")[0], focus):
                out = n_
        return {"ideas": ideas[:n], "dropped": dropped, "outside": out}
    return parse


def parse_impl(text, batch, patch):
    data = steps.extract_json(text)
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError('expected {"id": ..., "done": ..., "summary": ...}')
    return [{"id": batch[0]["id"], "done": data.get("done") is True,
             "summary": _text(data.get("summary"), 1500), "note": _text(data.get("note"), 500),
             "patch": patch or ""}]


_REVIEW = {"keeps": "supported", "weakens": "refuted", "unclear": "unclear"}


def parse_review(patches):
    def parse(text, batch):
        ids = {c["id"] for c, _ in batch}
        data = steps.extract_json(text)
        if not isinstance(data, list):
            raise ValueError("expected a JSON list, one entry per patch")
        out, seen = [], set()
        for d in data:
            if not isinstance(d, dict) or d.get("patch") not in ids or d["patch"] in seen:
                continue
            seen.add(d["patch"])
            verdict = str(d.get("verdict")).strip().lower()
            lines = d.get("lines") if isinstance(d.get("lines"), list) else []
            real = benchlib.cited_lines(lines, patches.get(d["patch"], ""))
            if verdict in ("keeps", "weakens") and not real:
                verdict, reason = "unclear", "no cited line is in the patch"
            else:
                reason = _text(d.get("reason"))
            out.append({"claim": d["patch"], "reason": reason, "verdict": _REVIEW.get(verdict, "unclear")})
        if data and not out:
            raise ValueError("no entry names one of your patches (%s)" % ", ".join(sorted(ids)))
        return out
    return parse


def parse_report(text):
    t = (text or "").strip()
    if not t:
        raise ValueError("the report is empty")
    return t


def _history(cands):
    if not cands:
        return "(none yet)"
    return "\n".join("- %s at %s: %s -> %s" % (
        c["idea"], c["location"], c["change"][:200],
        ("%.2fs (saved %.2fs), review %s" % (c["seconds"], c["base"] - c["seconds"], c["verdict"] or "-"))
        if c["ok"] and c["seconds"] is not None else (c["why"] or "failed"))
        for c in cands)


def _review_item(c, patch):
    p = patch if len(patch) <= PATCH_CLIP else patch[:PATCH_CLIP] + "[clipped]"
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", p)), default=0) + 1)
    return "- %s (idea %s, %.2fs -> %.2fs): %s\n%sdiff\n%s\n%s" % (
        c["id"], c["idea"], c["base"], c["seconds"], c["summary"], fence, p, fence)


def _measure(wf, cmd, patch, label, bar):
    """One run; a second only when the first beats `bar`. Both must pass with the same count."""
    a = _run(wf, cmd, label + " run 1", patch)
    if not a["ok"] or a["wall"] is None or a["wall"] > bar:
        return {"ok": a["ok"], "passed": a["passed"], "seconds": a["wall"], "runs": [a["wall"]],
                "why": benchlib.why(a)}
    c = _run(wf, cmd, label + " run 2", patch)
    ok = c["ok"] and a["passed"] == c["passed"]
    secs = [s for s in (a["wall"], c["wall"]) if s is not None]
    return {"ok": ok, "passed": a["passed"], "seconds": max(secs) if secs else None,
            "runs": [a["wall"], c["wall"]], "why": None if ok else benchlib.why(c)}


def _recheck(wf, b, bl, r):
    """None when measure still passes on the unchanged tree near the baseline; else why.
    A failure or a drift must happen twice in a row to count."""
    problem = None
    for k in (1, 2):
        a = _run(wf, b["measure"], "measure recheck round %d try %d" % (r, k))
        if not a["ok"]:
            problem = "measure fails on the unchanged tree in round %d: %s" % (r, benchlib.why(a))
        elif abs(a["wall"] - bl["base"]) > DRIFT * bl["noise"]:
            problem = "the baseline drifted in round %d: %.2fs vs %.2fs (noise %.2fs)" % (
                r, a["wall"], bl["base"], bl["noise"])
        else:
            return None
        wf.log("recheck %d: %s" % (k, problem))
    return problem


def _join(stack, patch):
    return stack + ("" if not stack or stack.endswith("\n") else "\n") + patch


def _same_count(bl, m):
    return bl["passed"] is None or m["passed"] == bl["passed"]


def _grow_stack(wf, b, bl, st, new):
    """Add each newly approved patch, fastest first, when the stack gets faster by more
    than the noise. True when one was added."""
    added = False
    for c in sorted([c for c in new if c.get("approved")], key=lambda c: c["seconds"]):
        trial = _join(st["stack"], st["patches"][c["id"]])
        m = _measure(wf, b["measure"], trial, "stack with %s" % c["id"], st["best"] - bl["noise"])
        if m["ok"] and _same_count(bl, m) and m["seconds"] is not None and m["seconds"] <= st["best"] - bl["noise"]:
            st["stack"], st["best"] = trial, m["seconds"]
            st["kept"].append(c["id"])
            st["times"][c["id"]] = m["seconds"]
            added = True
            wf.log("stack: kept %s -> %.2fs" % (c["id"], st["best"]))
        else:
            c["why"] = "approved; not added to the stack: %s" % (
                m["why"] or "not faster than the stack by more than the noise")
            wf.log("stack: dropped %s (runs %s, %s)" % (c["id"], m["runs"], m["why"] or "not faster"))
    return added


def _ideas(wf, b, bl, st):
    prof = bl["pm"]["profile"]
    seen = {(i["location"].lower(), i["change"].lower()) for i in st["ideas"]}
    got = wf.agent("profile", "profiler", PROFILE_P.format(
        goal=wf.goal, bench=_bench_text(b), passed=_count(bl["m1"]), base=bl["base"], noise=bl["noise"],
        fence=FENCE, profile=prof[-3800:], history=_history(st["cands"]), n=wf.knob("ideas")),
        parse_ideas(wf.knob("ideas"), seen, wf.target, b["focus"], [prof])) or \
        {"ideas": [], "dropped": [], "outside": None}
    for i in got["ideas"]:
        i["id"] = "I%d" % (len(st["ideas"]) + 1)
        st["ideas"].append(i)
    if got["ideas"]:
        wf.save("ideas", got["ideas"])
    for d in got["dropped"]:
        wf.log("idea dropped: %s (%s)" % (d["location"], d["why"]))
    return got


def _implement_and_measure(wf, b, bl, st, ideas, bar):
    res = wf.fan_out("implement", "implementer", ideas, lambda batch: IMPL_P.format(
        goal=wf.goal, bench=_bench_text(b), passed=_count(bl["m1"]), base=bl["base"], fence=FENCE,
        **batch[0]), parse_impl, max_items=1)
    by_id = {i["id"]: i for i in ideas}
    new = []
    for row in res.rows:
        idea = by_id[row["id"]]
        c = {"id": "P%d" % (len(st["cands"]) + len(new) + 1), "idea": row["id"], "location": idea["location"],
             "change": idea["change"], "summary": row["summary"], "base": bl["base"], "seconds": None,
             "runs": [], "passed": None, "ok": False, "qualifies": False, "verdict": None,
             "approved": False, "why": None}
        new.append(c)
        if not row["done"] or not row["patch"].strip():
            c["why"] = "no change kept"
            continue
        outside = benchlib.outside_focus(patch_paths(row["patch"]), b["focus"])
        if outside:
            c["why"] = "changes files outside focus: %s" % ", ".join(outside[:3])
            continue
        if len(row["patch"]) > PATCH_CLIP:
            c["why"] = "patch too long to review (%d characters)" % len(row["patch"])
            continue
        st["patches"][c["id"]] = row["patch"]
        m = _measure(wf, b["measure"], row["patch"], "candidate %s" % c["id"], bar)
        c.update(seconds=m["seconds"], runs=m["runs"], passed=m["passed"], ok=m["ok"])
        if not m["ok"]:
            c["why"] = "measure failed: %s" % m["why"]
        elif not _same_count(bl, m):
            c["why"] = "pass count changed: %s vs %s" % (m["passed"], bl["passed"])
        elif m["seconds"] is None or m["seconds"] > bar:
            c["why"] = "not faster than the bar (%.2fs)" % bar
        else:
            c["qualifies"] = True
    return new


def _review(wf, b, st, new):
    fast = [c for c in new if c["qualifies"]]
    if not fast:
        return
    v = wf.vote("review", "reviewer", fast, wf.knob("voters"), lambda batch: REVIEW_P.format(
        goal=wf.goal, keep=b["keep"], measure=b["measure"], fence=FENCE,
        items="\n".join(_review_item(c, st["patches"][c["id"]]) for c, _ in batch)),
        parse_review(st["patches"]))
    for c in fast:
        c["verdict"] = v[c["id"]]
        c["approved"] = v[c["id"]] == "supported"
        if not c["approved"]:
            c["why"] = "review: %s" % c["verdict"]


def _final_guard(wf, b, bl, st):
    """Guard on the stack, dropping patches from the end until it passes. Returns the
    report's guard line and an optional warning."""
    gp, gw = bl["g"]["passed"], bl["g"]["wall"]
    dropped = []
    while st["kept"]:
        stack = ""
        for cid in st["kept"]:
            stack = _join(stack, st["patches"][cid])
        g = _run(wf, b["guard"], "guard final %s" % "+".join(st["kept"]), stack)
        # an unreadable count (gp known, g's None) is NOT a pass: the guard must show it
        if g["ok"] and (gp is None or (g["passed"] is not None and g["passed"] >= gp)):
            st["stack"] = stack
            if dropped:
                m = _measure(wf, b["measure"], stack, "measure final %s" % "+".join(st["kept"]), float("inf"))
                if m["ok"] and m["seconds"] is not None:
                    st["best"] = m["seconds"]
                else:
                    st["best"] = st["times"][st["kept"][-1]]
                    wf.log("final measure of the reduced stack failed (%s); the stacked time "
                           "stays %.2fs, measured when %s joined" % (m["why"], st["best"], st["kept"][-1]))
            line = "guard: passed on the stack (%s) in %.2fs; baseline %.2fs" % (_count(g), g["wall"], gw)
            if dropped:
                line += "; dropped from the stack because guard failed with them: %s" % ", ".join(dropped)
            gn = benchlib.noise(gw, gw, gw)
            warn = ("WARNING: the full guard got slower (%.2fs vs %.2fs): the speed-up on measure may "
                    "cost time elsewhere." % (g["wall"], gw)) if g["wall"] > gw + 2 * gn else None
            return line, warn
        dropped.append(st["kept"].pop())
        wf.log("guard failed (%s); dropping %s" % (benchlib.why(g) if not g["ok"] else "fewer tests", dropped[-1]))
    st["stack"] = ""
    if dropped:
        st["guard_dropped_all"] = True
        return "guard: failed with every stack; nothing kept (%s)" % ", ".join(dropped), None
    return "guard: not run (nothing stacked)", None


def _rounds_and_finish(wf, b, bl):
    st = {"cands": [], "ideas": [], "stack": "", "best": bl["base"], "kept": [], "patches": {},
          "times": {}, "notes": []}
    bar = min(bl["base"] * (1 - wf.knob("min_gain")), bl["base"] - 2 * bl["noise"])
    target = benchlib.target_seconds(b, bl["base"])
    reason, last = None, 1
    for r in wf.rounds():
        last = r
        if r > 1:
            reason = _recheck(wf, b, bl, r)
            if reason:
                break
        got = _ideas(wf, b, bl, st)
        if got["outside"]:
            st["notes"].append(got["outside"])
        if not got["ideas"]:
            if got["dropped"] and all(d["why"].startswith("outside focus") for d in got["dropped"]):
                reason = "round %d: every idea was outside focus (%s)" % (
                    r, "; ".join(d["location"] for d in got["dropped"][:3]))
                break
            wf.converged("the profiler has no new idea that passed its checks")
            continue
        new = _implement_and_measure(wf, b, bl, st, got["ideas"], bar)
        _review(wf, b, st, new)
        st["cands"] += new
        wf.save("candidates", [dict(c) for c in new])
        added = _grow_stack(wf, b, bl, st, new)
        if target is not None and st["kept"] and st["best"] <= target:
            wf.log("target reached: %.2fs <= %.2fs" % (st["best"], target))
            wf.converged("target reached")
        elif not added:
            wf.converged("round %d added no speed-up to the stack" % r)
    _finish(wf, b, bl, st, target, reason, last)


def _finish(wf, b, bl, st, target, reason, last):
    approved = [c["id"] for c in st["cands"] if c["approved"]]
    for n, cid in enumerate(approved, 1):
        wf.write("patches/%d-%s.diff" % (n, cid), st["patches"][cid])
    if reason:
        wf.event("bench", verdict="problem", phase="round %d" % last, reason=reason)
        path = wf.run_dir / "bench-review.md"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        wf.write("bench-review.md", old + "\n## Stopped in round %d\n\n%s\n" % (last, reason))
        wf.goal_unmet("bench problem: %s" % reason)
        wf.report(_dropout_report(reason, "in round %d" % last, approved))
        return
    guard_line, warn = _final_guard(wf, b, bl, st)
    if st["stack"]:
        wf.write("patches/stacked.diff", st["stack"])
    base = bl["base"]
    if st["kept"]:
        stacked = "%s -> %.2fs from %.2fs (%.0f%% faster), patches/stacked.diff" % (
            "+".join(st["kept"]), st["best"], base, 100 * (1 - st["best"] / base))
    else:
        stacked = "(nothing stacked)"
    unmet = None
    if not st["kept"]:
        unmet = ("the guard failed with every stack, so nothing was kept"
                 if st.get("guard_dropped_all") else "no approved patch made measure faster")
    elif target is not None and st["best"] > target:
        unmet = "target %s not reached: %.2fs (needed %.2fs)" % (b["target"], st["best"], target)
    if unmet:
        wf.goal_unmet(unmet)
    count = _count(bl["m1"])
    facts = "\n".join([
        "Bench: " + _bench_text(b).replace("\n", "; "),
        "Baseline: %s in %.2fs (best of two runs %s; noise %.2fs)" % (
            count, base, [bl["m1"]["wall"], bl["m2"]["wall"]], bl["noise"]),
        "Target: %s" % ("%s = %.2fs, %s" % (b["target"], target, "reached" if st["kept"] and st["best"] <= target
                                            else "not reached") if target is not None else "none"),
        "Stacked: " + stacked, guard_line] + [
        "- %s (idea %s at %s): %s | runs %s | %s" % (
            c["id"], c["idea"], c["location"], c["summary"], c["runs"],
            "kept in the stack" if c["id"] in st["kept"] else (c["why"] or "approved, not stacked"))
        for c in st["cands"]] + [
        "Slow outside focus (not implemented): %s -- %s" % (n_["location"], n_["why"]) for n_ in st["notes"]])
    body = wf.agent("write", "writer", WRITE_P.format(goal=wf.goal, facts=facts), parse_report,
                    repair=WRITE_REPAIR)
    table = ["| patch | idea | seconds (runs) | passed | review | stacked | why not |",
             "|---|---|---|---|---|---|---|"]
    for c in st["cands"]:
        table.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            c["id"], c["idea"], c["runs"] or "-", c["passed"] if c["passed"] is not None else "-",
            c["verdict"] or "-", "yes" if c["id"] in st["kept"] else "-",
            "" if c["id"] in st["kept"] else (c["why"] or "")))
    head = ["bench: ok", "result: %s" % (unmet or ("target reached" if target is not None else "stack kept"))]
    head += [warn] if warn else []
    notes = ["", "## Slow outside focus (report only)", ""] + [
        "- %s: %s" % (n_["location"], n_["why"]) for n_ in st["notes"]] if st["notes"] else []
    wf.report("\n".join(head) + "\n\n" + (body or "# Optimize\n\n(the writer failed; see the table)") +
              "\n\n## Measurements\n\n%s\n\n%s\n"
              % ("\n".join(["- " + x for x in facts.splitlines()[:5]]), "\n".join(table)) +
              ("\n".join(notes) + "\n" if notes else ""))


def run(wf):
    b = _load_bench(wf)
    bl = _baseline(wf, b)
    reason = _bench_phase(wf, b, bl)
    if reason:
        wf.goal_unmet("bench problem: %s" % reason)
        wf.report(_dropout_report(reason, "before round 1", []))
        return
    _rounds_and_finish(wf, b, bl)
