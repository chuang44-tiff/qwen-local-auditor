"""The sweep engine: everything that took measurement to get right.

The engine owns writing, batching, dispatch bookkeeping, parsing and collation.
Builders are pure. That split is what lets these hold for every builder, present
and future:

  * a withheld item is NEVER written, so it can never be dispatched;
  * surviving items are numbered contiguously, so key.txt is always dense;
  * the expected key set comes from the BUILDER (clauses included), so a missing
    block is detectable;
  * a sweep that would audit nothing FAILS instead of reporting success.

Batch directories live under the user cache dir, never inside the target repo: a
general tool must not write into an arbitrary user's tree, and `--builder diff`
on a dirty tree would otherwise see its own batch output as a change.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

from lib import blocks
from lib.builders import base as _base
from lib.builders.base import Built, load_builder

BYTE_BUDGET = 240_000      # per batch default; qwen-sweep scales it to the model's window
SUSPECT_BYTES = 1000       # an out.md this small with NO expected block is the autocompact death
_VAR = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")
_BRIEF_NAME = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def cache_root():
    """$QWEN_SWEEP_CACHE, else $XDG_CACHE_HOME/qwen-sweep, else ~/.cache/qwen-sweep."""
    explicit = os.environ.get("QWEN_SWEEP_CACHE")
    if explicit:
        return explicit
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(xdg, "qwen-sweep")


def batch_root(repo, base=None):
    """A run directory outside the target repo, namespaced by repo identity."""
    h = hashlib.sha1(os.path.abspath(repo).encode()).hexdigest()[:8]
    return os.path.join(base or cache_root(), h)


def plan_batches(builts, budget=BYTE_BUDGET):
    """Group item indices into batches by BYTE budget, not a fixed count.

    A fixed BATCH=7 was tuned for one corpus; seven small files and seven files
    with huge hunks are not the same batch. An item over budget on its own still
    gets a batch, because withholding it is the builder's call, not the engine's.
    """
    out, cur, used = [], [], 0
    for i, b in enumerate(builts):
        if b.withheld:
            continue
        if cur and used + b.size > budget:
            out.append(cur); cur, used = [], 0
        cur.append(i); used += b.size
    if cur:
        out.append(cur)
    return out


def expected_keys(built, n):
    """The keys this item must produce: one per clause, or one for the item."""
    if built.clauses:
        return ["t%d.%s" % (n, c) for c in built.clauses]
    return ["t%d" % n]


def _write(path, text):
    # newline="\n": on Windows text mode would write CRLF, and the shell side
    # reads these files line by line.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_batch(out_dir, builts):
    """Write t{n}/context{n} for surviving items; record the rest with reasons."""
    out_dir = str(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    keys, key_rows, skip_rows, n = [], [], [], 0
    for b in builts:
        if b.withheld:
            skip_rows.append("%s|%s\n" % (b.label, b.withheld))
            continue
        n += 1
        _write(os.path.join(out_dir, "t%d.md" % n), b.body)
        _write(os.path.join(out_dir, "context%d.md" % n), b.context)
        key_rows.append("t%d|%s\n" % (n, b.label))
        keys.extend(expected_keys(b, n))
    _write(os.path.join(out_dir, "key.txt"), "".join(key_rows))
    _write(os.path.join(out_dir, "skipped.txt"), "".join(skip_rows))
    _write(os.path.join(out_dir, "expected.txt"), "\n".join(keys) + ("\n" if keys else ""))
    return keys


def render_brief(text, variables):
    """Substitute {{VAR}}. An unfilled declared variable is a loud KeyError.

    The old runner did `sed s/Emit exactly 7 blocks.../` against brief prose -- a
    silent string dependency that broke the day anyone reworded the brief.
    """
    missing = [m for m in _VAR.findall(text) if m not in variables]
    if missing:
        raise KeyError("brief declares %s but no value was given" % ", ".join(sorted(set(missing))))
    return _VAR.sub(lambda m: str(variables[m.group(1)]), text)


def brief_path(name):
    """A brief by name: $QWEN_BRIEF_DIR/<name>.md first, then the bundled lib/briefs/."""
    if not _BRIEF_NAME.match(name or ""):
        raise ValueError("bad brief name: %r" % (name,))
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [d for d in (os.environ.get("QWEN_BRIEF_DIR"), os.path.join(here, "briefs")) if d]
    for d in dirs:
        p = os.path.join(d, "%s.md" % name)
        if os.path.isfile(p):
            return p
    raise ValueError("no brief named %r in %s" % (name, " or ".join(dirs)))


def _lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [l.rstrip("\r\n") for l in fh]


def _expected(d):
    exp = [e.strip() for e in _lines(os.path.join(d, "expected.txt")) if e.strip()]
    if exp:
        return exp
    return [l.split("|", 1)[0] for l in _lines(os.path.join(d, "key.txt")) if "|" in l]


def batch_status(d):
    """Judge one dispatched batch by CONTENT: ('ok' | 'suspect' | 'incomplete' | 'missing', detail).

    A short answer is not a failure. Only an output that is small AND carries none
    of the expected blocks matches the autocompact self-destruct signature.
    """
    outf = os.path.join(d, "out.md")
    expected = _expected(d)
    if not os.path.exists(outf) or os.path.getsize(outf) == 0:
        return "missing", "no output"
    with open(outf, encoding="utf-8", errors="replace") as fh:
        body = fh.read()
    missing = blocks.completeness(blocks.parse(body), expected)
    if not missing:
        return "ok", "%d/%d block(s)" % (len(expected), len(expected))
    if len(missing) == len(expected) and len(body) < SUSPECT_BYTES:
        return "suspect", ("output only %dB and no expected block -- autocompact thrash "
                           "signature" % len(body))
    return "incomplete", "%d of %d block(s) missing: %s" % (len(missing), len(expected), missing)


def _resolver(repo):
    """is_prose(path, line) for invariant 8, or None when unresolvable."""
    cache = {}

    def is_prose(path, line):
        from lib.context import prose_mask, SKIP_DIRS
        name = os.path.basename(path)
        if name not in cache:
            found = None
            for dirpath, dirnames, filenames in os.walk(repo):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                if name in filenames:
                    found = os.path.join(dirpath, name); break
            if found:
                with open(found, encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
                cache[name] = prose_mask(src.splitlines(), src, found)
            else:
                cache[name] = None
        mask = cache[name]
        if mask is None or line < 1 or line > len(mask):
            return None
        return mask[line - 1]
    return is_prose


def collate(root, repo=None):
    """Parse every batch, check completeness, apply invariant 8, write collated.json.

    Invariant 8: a favourable verdict must not rest on a cited line that is a
    comment or docstring (see blocks.flag_prose_evidence).
    """
    rows, problems, skipped, seen = [], [], [], set()

    def add_skip(label, why):
        if (label, why) not in seen:
            seen.add((label, why))
            skipped.append([label, why])

    is_prose = _resolver(repo) if repo else (lambda p, n: None)
    for d in sorted(os.path.join(root, x) for x in os.listdir(root)
                    if re.fullmatch(r"b\d\d+", x)):
        keyf, outf = os.path.join(d, "key.txt"), os.path.join(d, "out.md")
        if not os.path.exists(keyf):
            continue
        labels = dict(l.strip().split("|", 1) for l in _lines(keyf) if "|" in l)
        for l in _lines(os.path.join(d, "skipped.txt")):
            if "|" in l:
                add_skip(*l.strip().split("|", 1))
        expected = _expected(d)
        if not os.path.exists(outf):
            problems.append("%s: NO OUTPUT (%d items unaudited)" % (d, len(labels)))
            continue
        with open(outf, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        parsed = blocks.parse(body)
        missing, extra = blocks.completeness(parsed, expected, return_extra=True)
        if missing and len(missing) == len(expected) and len(body) < SUSPECT_BYTES:
            problems.append("%s: output only %dB and no expected block -- autocompact thrash "
                            "signature, %d items unaudited" % (d, len(body), len(labels)))
            continue
        if missing:
            problems.append("%s: %d block(s) missing: %s" % (d, len(missing), missing))
        if extra:
            problems.append("%s: emitted unexpected block(s): %s" % (d, extra))
        for flag in blocks.flag_prose_evidence(parsed, is_prose):
            problems.append("%s: PROSE EVIDENCE -- %s" % (d, flag))
        for b in parsed:
            rows.append({"batch": os.path.basename(d),
                         "item": labels.get("t%d" % b["item"], "?"),
                         "key": b["key"], "clause": b["clause"], "verdict": b["verdict"],
                         "finding": b["finding"], "evidence": b["evidence"], "why": b["why"]})
    for l in _lines(os.path.join(root, "needs-human.txt")):
        if "|" in l:
            add_skip(*l.strip().split("|", 1))
    with open(os.path.join(root, "collated.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"schema": 1, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "rows": rows, "problems": problems, "withheld": skipped}, fh, indent=1)
    return rows, problems, skipped


# ------------------------------------------------------------------ CLI

def _label_of(item):
    if isinstance(item, dict):
        for k in ("label", "doc", "path"):
            if item.get(k):
                return str(item[k])
    return str(item)[:60]


def _describe(args):
    parts = ["%s=%r" % (k, args[k]) for k in ("glob", "base", "input", "docs") if args.get(k)]
    return " (%s)" % ", ".join(parts) if parts else ""


def _cmd_build(a):
    try:
        b = load_builder(a.builder)
        args = json.loads(a.args) if a.args else {}
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    missing = [hint for key, hint in getattr(b, "REQUIRED_ARGS", {}).items()
               if args.get(key) in (None, "", [])]
    if missing:
        print("error: --builder %s needs %s" % (a.builder, " and ".join(missing)), file=sys.stderr)
        return 2
    if a.item_budget:
        _base.set_item_budget(a.item_budget)
    try:
        items = b.enumerate_items(args)
    except Exception as exc:                          # noqa: BLE001
        print("error: --builder %s could not list its items: %s" % (a.builder, exc),
              file=sys.stderr)
        return 9
    builts = []
    for it in items:
        try:
            builts.append(b.build(it))
        except Exception as exc:                      # noqa: BLE001
            if a.strict:
                print("FATAL: builder raised on %s: %s" % (_label_of(it), exc), file=sys.stderr)
                return 9
            builts.append(Built("", "", 0, "builder raised: %s" % exc, None, _label_of(it)))
    plan = plan_batches(builts, a.budget)
    os.makedirs(a.out, exist_ok=True)
    manifest = []
    for i, idxs in enumerate(plan, 1):
        d = os.path.join(a.out, "b%02d" % i)
        keys = write_batch(d, [builts[j] for j in idxs])
        manifest.append({"dir": d, "keys": keys, "n": len(keys)})
        print("b%02d: %d item(s), %d expected block(s)" % (i, len(idxs), len(keys)))
    withheld = [x for x in builts if x.withheld]
    needs_human = os.path.join(a.out, "needs-human.txt")
    _write(needs_human, "".join("%s|%s\n" % (x.label, x.withheld) for x in withheld))
    for x in withheld:
        print("SKIP  %-50s %s" % (x.label[:50], x.withheld))
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"builder": a.builder, "brief": a.brief or b.DEFAULT_BRIEF,
                   "items": len(items), "withheld": len(withheld), "batches": manifest},
                  fh, indent=1)
    print("built %d batch(es) from %d item(s), %d withheld" % (len(plan), len(items), len(withheld)))
    if not plan and not a.allow_empty:
        if not items:
            print("error: nothing to audit -- --builder %s found no items%s"
                  % (a.builder, _describe(args)), file=sys.stderr)
        else:
            print("error: nothing to audit -- all %d item(s) were withheld; reasons in %s"
                  % (len(items), needs_human), file=sys.stderr)
        return 9
    return 0


def _cmd_brief(a):
    try:
        with open(brief_path(a.name), encoding="utf-8") as fh:
            text = fh.read()
        variables = json.loads(a.vars)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if a.expected:
        # Computed here, not in shell: item keys must never be spliced into JSON
        # by string concatenation.
        keys = [k.strip() for k in _lines(a.expected) if k.strip()]
        variables.setdefault("N", len(keys))
        variables.setdefault("ITEMS", ",".join(keys))
    sys.stdout.write(render_brief(text, variables))
    return 0


def _cmd_check(a):
    state, detail = batch_status(a.dir)
    print("%s: %s" % (state, detail))
    return 0 if state == "ok" else 1


def _cmd_collate(a):
    rows, problems, skipped = collate(a.root, a.repo)
    print("=" * 70)
    print("COLLATED %d block(s)" % len(rows))
    verdicts = {}
    for r in rows:
        if r["verdict"]:
            verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    for k, v in sorted(verdicts.items(), key=lambda x: -x[1]):
        print("  %4d  %s" % (v, k))
    print("=" * 70)
    if skipped:
        print("\n%d item(s) WITHHELD (never dispatched) -> route to a human:" % len(skipped))
        for name, why in skipped:
            print("  ~ %s  [%s]" % (name, why))
    if problems:
        print("\n*** %d PROBLEM(S) ***" % len(problems))
        for p in problems:
            print("  ! %s" % p)
    else:
        print("\nno collation problems")
    print("\nresults:       %s" % os.path.join(a.root, "collated.json"))
    if skipped:
        print("needs a human: %s (%d item(s))" % (os.path.join(a.root, "needs-human.txt"),
                                                  len(skipped)))
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--builder", required=True)
    b.add_argument("--args", default="{}")
    b.add_argument("--out", required=True)
    b.add_argument("--brief", default=None)
    b.add_argument("--budget", type=int, default=BYTE_BUDGET)
    b.add_argument("--item-budget", type=int, default=None)
    b.add_argument("--strict", action="store_true")
    b.add_argument("--allow-empty", action="store_true")
    b.set_defaults(fn=_cmd_build)

    r = sub.add_parser("brief")
    r.add_argument("--name", required=True)
    r.add_argument("--vars", default="{}")
    r.add_argument("--expected", default=None, help="expected.txt of a batch; fills N and ITEMS")
    r.set_defaults(fn=_cmd_brief)

    k = sub.add_parser("check")
    k.add_argument("--dir", required=True)
    k.set_defaults(fn=_cmd_check)

    c = sub.add_parser("collate")
    c.add_argument("--root", required=True)
    c.add_argument("--repo", default=None)
    c.set_defaults(fn=_cmd_collate)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
