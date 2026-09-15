"""The block schema: one generic shape every brief targets.

    ## t<N>[.<clause-id>]
    FINDING:  <one line>
    EVIDENCE: <path:line, ...>
    WHY:      <reasoning>
    [VERDICT: <token>]        # only when the builder declared clauses

VERDICT is OPTIONAL and typed. Only a clause-shaped builder (claims) declares a
verdict vocabulary; diff, files and logs omit it, because a diff review has no
claim to adjudicate.

Completeness is checkable for EVERY builder: the set of emitted keys must equal
the set the builder declared. That is what gives the "non-zero exit on a missing
block" invariant its teeth, and it works only because the BUILDER enumerates
clauses -- if the model decided how many clauses an item had, a dropped block
would be indistinguishable from an item that simply had fewer.
"""
import re

# Verdicts that assert the absence of a problem. Only these can fail OPEN, so
# only these are worth flagging when their evidence turns out to be prose.
FAVOURABLE = frozenset({"APPEARS_FIXED", "RESOLVED", "NOT_PRESENT"})

_HEAD = re.compile(r"^##\s*t(\d+)(?:\.([A-Za-z0-9_-]+))?\s*$", re.M)
_FIELD = re.compile(r"^(VERDICT|FINDING|EVIDENCE|WHY):\s*(.*?)\s*$", re.M)
_CITE = re.compile(r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+):(\d+)")


def parse(text):
    """Split emitted text into blocks. Tolerates ``` fences and field reordering."""
    blocks, heads = [], list(_HEAD.finditer(text))
    for i, m in enumerate(heads):
        seg = text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        seg = seg.replace("```", "")
        fields = {k.lower(): v for k, v in _FIELD.findall(seg)}
        item, clause = int(m.group(1)), m.group(2)
        blocks.append({
            "key": "t%d.%s" % (item, clause) if clause else "t%d" % item,
            "item": item,
            "clause": clause,
            "verdict": fields.get("verdict") or None,
            "finding": fields.get("finding", ""),
            "evidence": fields.get("evidence", ""),
            "why": fields.get("why", ""),
        })
    return blocks


def completeness(blocks, expected_keys, return_extra=False):
    """Keys the builder declared but the model did not emit (and optionally vice versa)."""
    got = {b["key"] for b in blocks}
    missing = [k for k in expected_keys if k not in got]
    if return_extra:
        return missing, sorted(got - set(expected_keys))
    return missing


def citations(evidence):
    """Extract usable (path, line) pairs. A bare filename has no line and is unusable.

    MEASURED: ~40% of evidence pointers are bare filenames with no directory, so a
    consumer needs a resolver and must tolerate failure.
    """
    return [(p, int(n)) for p, n in _CITE.findall(evidence or "")]


def flag_prose_evidence(blocks, is_prose):
    """Invariant 8: flag a favourable verdict whose cited line is prose.

    This targets the recorded fail-open directly -- the model cited a module
    docstring DESCRIBING a fix as evidence the fix had landed. A docstring
    describing a fix is not evidence the fix landed; a code line is.

    `is_prose(path, line) -> True | False | None`; None means "could not
    resolve", and an unresolvable citation NEVER produces a flag. This check is
    therefore fail-safe, not complete: it cannot see a bare-filename citation.
    """
    out = []
    for b in blocks:
        if (b["verdict"] or "").upper() not in FAVOURABLE:
            continue
        cites = citations(b["evidence"])
        if not cites:
            continue
        resolved = [(p, n, is_prose(p, n)) for p, n in cites]
        prose_hits = [(p, n) for p, n, v in resolved if v is True]
        code_hits = [(p, n) for p, n, v in resolved if v is False]
        if not prose_hits:
            continue
        # ANY prose citation, not only-prose. The recorded fail-open cited a
        # docstring ALONGSIDE real code and stopped at the favourable half, so an
        # only-prose rule would have missed the very case this exists to catch.
        kind = "only prose" if not code_hits else "prose among its evidence"
        out.append("%s: %s cites %s (%s) -- a docstring describing a fix is not "
                   "evidence the fix landed; require a code line"
                   % (b["key"], b["verdict"], kind,
                      ", ".join("%s:%d" % c for c in prose_hits)))
    return out
