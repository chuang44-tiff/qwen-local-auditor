"""The optimize workflow's bench file and the checks that need no model: loading the
bench, B1-B4 on the baseline runs, focus matching, and the evidence checks applied to
every claim a local agent makes (quotes, path:line locations, cited diff lines).

Standard library only; loaded by file path from workflow.py and the tests."""
import fnmatch
import json
import pathlib
import re

FIELDS = ("measure", "guard", "focus", "target", "keep")
DEFAULT_KEEP = ("no test removed, skipped or loosened; assertions, expected values and the set "
                "of tests run unchanged")
MIN_QUOTE = 8
_TARGET = re.compile(r"^-(\d+(?:\.\d+)?)%$|^(\d+(?:\.\d+)?)s$")
_LOC = re.compile(r"^(.+?)(?::(\d+)(?:-(\d+))?)?$")


def parse_target(text):
    m = _TARGET.match((text or "").strip())
    if not m:
        raise ValueError("target must look like -30%% or 60s (got %r)" % (text,))
    if m.group(1) is not None:
        p = float(m.group(1)) / 100
        if not 0 < p < 1:
            raise ValueError("a percentage target must be between -0%% and -100%% (got %r)" % (text,))
        return ("pct", p)
    s = float(m.group(2))
    if s <= 0:
        raise ValueError("a seconds target must be above 0 (got %r)" % (text,))
    return ("sec", s)


def default(cmd):
    """The bench of a run given only --set tests=CMD: the whole suite, no focus, no target."""
    return {"measure": cmd, "guard": cmd, "focus": [], "target": None, "keep": DEFAULT_KEEP}


def _absolute(p):
    return p.startswith("/") or bool(re.match(r"^[A-Za-z]:", p))


def from_data(data):
    """(bench, None) for a valid parsed bench object, else (None, why)."""
    if not isinstance(data, dict):
        return None, "the bench must be a JSON object"
    extra = sorted(set(data) - set(FIELDS))
    if extra:
        return None, "unknown bench field(s): %s (known: %s)" % (", ".join(extra), ", ".join(FIELDS))
    measure = data.get("measure")
    if not isinstance(measure, str) or not measure.strip():
        return None, 'the bench needs "measure": the test command to time'
    guard = data.get("guard", measure)
    if not isinstance(guard, str) or not guard.strip():
        return None, '"guard" must be a non-empty command'
    focus = data.get("focus", [])
    if not isinstance(focus, list) or not all(isinstance(f, str) and f.strip() for f in focus):
        return None, '"focus" must be a list of paths or globs'
    focus = [norm_path(f) for f in focus]
    for f in focus:
        if _absolute(f) or ".." in f.split("/"):
            return None, "focus entry %r must be relative to the target, without .." % f
    target = data.get("target")
    if target is not None:
        if not isinstance(target, str):
            return None, '"target" must be a string like -30% or 60s'
        try:
            parse_target(target)
        except ValueError as e:
            return None, str(e)
    keep = data.get("keep", DEFAULT_KEEP)
    if not isinstance(keep, str) or not keep.strip():
        return None, '"keep" must be a non-empty sentence'
    return {"measure": measure.strip(), "guard": guard.strip(), "focus": focus,
            "target": target.strip() if target else None, "keep": keep.strip()}, None


def load(path):
    """(bench, None) or (None, why) for a bench file."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, "cannot read the bench file %s: %s" % (path, e)
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, "%s is not valid JSON: %s" % (path, e)
    b, why = from_data(data)
    return (b, None) if b is not None else (None, "%s: %s" % (path, why))


def target_seconds(bench, base):
    if not bench.get("target"):
        return None
    kind, v = parse_target(bench["target"])
    return base * (1 - v) if kind == "pct" else v


def noise(a, b, base):
    """The timing noise floor: the spread of two runs, never under 2% of the baseline or 0.05 s."""
    return max(abs(a - b), 0.02 * base, 0.05)


def norm_path(p):
    p = (p or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def in_focus(path, focus):
    """True when focus is empty (the whole target) or `path` is a focus entry, under a focus
    folder, or matches a focus glob."""
    if not focus:
        return True
    p = norm_path(path)
    for f in focus:
        f = norm_path(f).rstrip("/")
        if p == f or p.startswith(f + "/") or fnmatch.fnmatchcase(p, f):
            return True
    return False


def outside_focus(paths, focus):
    return [p for p in paths if not in_focus(p, focus)]


def _squash(s):
    return " ".join((s or "").split()) if isinstance(s, str) else ""


def quote_found(quote, texts):
    """The quote (whitespace squashed, at least MIN_QUOTE characters) is in one of texts."""
    q = _squash(quote)
    return len(q) >= MIN_QUOTE and any(q in _squash(t) for t in texts)


def _brief(s):
    """A location safe to put in the one-line reason a bench problem reports: clipped, and on
    one line, since a claimed path can hold a line break (\\x85, \\u2028, \\r) that would split
    or fake that line."""
    s = str(s)
    if not s.isprintable():
        s = repr(s)[1:-1]                        # escapes every character splitlines() would break on
    return s if len(s) <= 80 else s[:77] + "..."


def location_ok(root, location):
    """(path, None) when `location` ("path", "path:line" or "path:a-b") names a file inside
    root, with the lines inside it; (None, why) otherwise."""
    m = _LOC.match((location or "").strip()) if isinstance(location, str) else None
    if not m:
        return None, "no location"
    rel = norm_path(m.group(1))
    if not rel or _absolute(rel) or ".." in rel.split("/"):
        return None, "location %r is not a path inside the target" % _brief(location)
    base = pathlib.Path(root)
    try:
        f = (base / rel).resolve()
        base = base.resolve()
    except (OSError, ValueError, RuntimeError):  # a NUL in a name, one the os cannot stat, or a symlink loop
        return None, "location %r is not a path inside the target" % _brief(location)
    try:
        f.relative_to(base)
    except ValueError:
        return None, "location %r leaves the target" % _brief(location)
    try:
        inside = f.is_file()
    except (OSError, ValueError):               # a name longer than the os can stat is not a file
        inside = False
    if not inside:
        return None, "no file %s in the target" % _brief(rel)
    if m.group(2):
        try:
            lo = int(m.group(2))
            hi = int(m.group(3)) if m.group(3) else lo
        except ValueError:                     # more digits than int parses: no file has that many lines
            return None, "location %s is out of range" % _brief(location)
        try:
            n = len(f.read_bytes().splitlines())
        except OSError as e:
            return None, "cannot read %s: %s" % (_brief(rel), e)
        if not 1 <= lo <= hi <= max(n, 1):
            return None, "%s has %d lines; %s is out of range" % (_brief(rel), n, _brief(location))
    return rel, None


def cited_lines(lines, patch):
    """The cited lines that are really in the patch: a whole diff line (with its +/-) or
    its text without the marker, at least 3 characters."""
    body = (patch or "").splitlines()
    whole = {x.strip() for x in body}
    bare = {x[1:].strip() for x in body if x[:1] in "+- "}
    out = []
    for c in lines or []:
        if not isinstance(c, str) or len(c.strip()) < 3:
            continue
        s = c.strip()
        if s in whole or s in bare:
            out.append(c)
    return out


def why(o):
    if not o.get("applied", True):
        return "the patch did not apply"
    if o.get("timed_out"):
        return "timed out"
    if o.get("rc") is None:
        return "no result line (the wrapper did not finish)"
    if o.get("rc") != 0:
        return "rc %s" % o["rc"]
    return "%s test(s) failed" % o.get("failed")


def _count(o):
    return "%s passed" % o["passed"] if o.get("passed") is not None else "pass count not checked"


def checks(bench, m1, m2, g, files):
    """B1-B4 on the baseline: measure passes twice, guard passes and runs at least as many
    tests, the measure profile reaches focus (advisory: None, never False), the target is
    above the noise."""
    out = []
    b1 = bool(m1["ok"] and m2["ok"])
    out.append({"id": "B1", "ok": b1, "detail": (
        "measure passed twice on the unchanged tree (%.2fs, %.2fs; %s)" % (m1["wall"], m2["wall"], _count(m1))
        if b1 else "measure did not pass on the unchanged tree: %s" % why(m2 if m1["ok"] else m1))})
    if not g["ok"]:
        out.append({"id": "B2", "ok": False, "detail": "guard did not pass on the unchanged tree: %s" % why(g)})
    elif g.get("passed") is not None and m1.get("passed") is not None and g["passed"] < m1["passed"]:
        out.append({"id": "B2", "ok": False, "detail": "guard runs fewer tests (%d passed) than measure (%d passed)"
                    % (g["passed"], m1["passed"])})
    else:
        out.append({"id": "B2", "ok": True, "detail": "guard passed (%.2fs; %s)" % (g["wall"], _count(g))})
    focus = bench.get("focus") or []
    if not focus:
        out.append({"id": "B3", "ok": True, "detail": "focus is the whole target"})
    elif files is None:
        out.append({"id": "B3", "ok": None, "detail": "skipped: the runner gives no per-file timing"})
    else:
        # advisory: a per-test profile names test files, so a focus on the code those tests
        # run is never "hit"; the checker judges that case, with quoted evidence
        hit = [f for f in files if in_focus(f, focus)]
        out.append({"id": "B3", "ok": True if hit else None, "detail": (
            "the measure profile names %s inside focus" % hit[0] if hit else
            "note: no profiled file is inside focus (profile: %s); expected when focus holds the "
            "code the tests run rather than the tests" % ", ".join(files[:5]))})
    if not bench.get("target"):
        out.append({"id": "B4", "ok": None, "detail": "no target"})
    elif not b1:
        out.append({"id": "B4", "ok": None, "detail": "skipped: measure did not pass"})
    else:
        base = min(m1["wall"], m2["wall"])
        n = noise(m1["wall"], m2["wall"], base)
        saving = base - target_seconds(bench, base)
        ok = saving > 2 * n
        out.append({"id": "B4", "ok": ok, "detail": (
            "target %s means saving %.2fs of %.2fs; noise %.2fs" % (bench["target"], saving, base, n) if ok else
            "target %s means saving %.2fs, not more than 2x the timing noise (%.2fs)" % (bench["target"], saving, n))})
    return out
