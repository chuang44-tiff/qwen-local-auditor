"""deviations: an item is one changed file; the question is whether each departure
from the SPEC is recorded (decision log or transcript) and still holds.

Verdicts: DEVIATION_EXPLAINED | DRIFT_UNEXPLAINED | MATCHES_SPEC | CANNOT_DETERMINE.
A DEVIATION_EXPLAINED must cite a re-run TEST line (the collator enforces it).
"""
import glob
import os

from lib import decisions

from . import diff, history
from .base import Built, item_budget

DEFAULT_BRIEF = "deviation"
REQUIRED_ARGS = {"spec": "--arg spec=PATH (the spec the code is held to)"}
MAX_SPEC = 40_000


def _state_dir(repo):
    from lib import supervisor
    return supervisor.repo_state_dir(repo)


def enumerate_items(args):
    repo = args["repo"]
    with open(args["spec"], encoding="utf-8", errors="replace") as fh:
        spec = fh.read()
    if len(spec) > MAX_SPEC:
        # Name the cut: a clipped spec that reads as the whole one lets the auditor
        # rule on clauses it was never shown.
        spec = spec[:MAX_SPEC] + "\n[spec truncated at %d characters]" % MAX_SPEC
    log = []
    for p in sorted(glob.glob(os.path.join(_state_dir(repo), "*", "decisions.jsonl"))):
        run = os.path.basename(os.path.dirname(p))
        log += [dict(e, run=run) for e in decisions.load(p)]
    tdir = history.transcript_dir(repo)
    out = []
    for it in diff.enumerate_items(args):
        it.update({"spec": spec, "log": log, "tdir": tdir})
        out.append(it)
    return out


def _render_log(entries):
    """Entries come from several run dirs, so per-run `n` collide: renumber, and
    name the run under each header so the auditor can tell runs apart."""
    if not entries:
        return decisions.render([])
    blocks = []
    for i, e in enumerate(entries, 1):
        head, _, rest = decisions.render([dict(e, n=i)]).partition("\n")
        blocks.append("%s\nRUN: %s\n%s" % (head, e.get("run", "?"), rest))
    return "\n\n".join(blocks)


def build(item):
    base = diff.build(item)
    if base.withheld:
        return base
    ev = history.events_for(item["path"], history.sessions(item["tdir"]))
    # A missing folder is not an empty one: say which, so the auditor knows the
    # transcript evidence was unavailable, not absent.
    if not ev and not os.path.isdir(item["tdir"]):
        ev = ["(no Claude transcript folder for this repo)"]
    context = "\n".join([base.context, "# The spec", "", item["spec"], "",
                         "# Decision log (all recorded runs)", "", _render_log(item["log"]), "",
                         "# Transcript evidence for this file", ""] + (ev or ["(none)"]) + [""])
    if len(context) > item_budget():
        context = context[:item_budget()] + "\n[TRUNCATED at item budget]\n"
    return Built(base.body, context, len(base.body) + len(context), None, None, base.label)
