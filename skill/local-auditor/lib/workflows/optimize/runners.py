"""Run one test command, time it and summarise its output: the optimize workflow's
measuring stick, for any test runner.

Used two ways. workflow.py loads it (wrap, outcome, PROFILE_FLAGS) and hands wrap()'s
command to run_cmd; inside that sandbox, python reads THIS FILE from stdin and runs
main(): `bash -c CMD` with its output in a temporary file outside the tree, timed, then a
short summary on stdout ending in one line `QLA_RESULT {json}`. run_cmd keeps only the
last 4000 characters of output, so the summary stays under that.

Standard library only, Python 3.8+, no imports from this repository: it runs under the
engine's python inside the user's project.
"""
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time

MARK = "QLA_RESULT "
EOF = "QLA_RUNNERS_EOF"
TAIL_CHARS = 600
TOP = 10
NAME_CHARS = 80
PROFILE_FLAGS = {"pytest": "--durations=0 --durations-min=0.01", "jest": "--verbose", "go": "-v"}

_PYTEST_WORDS = re.compile(r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")
_PYTEST_KEY = {"passed": "passed", "xpassed": "passed", "failed": "failed", "error": "failed",
               "errors": "failed", "skipped": "skipped", "xfailed": "skipped"}
_PYTEST_DUR = re.compile(r"^\s*([\d.]+)s (setup|call|teardown)\s+(\S+)", re.M)
_UNIT_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s", re.M)
_UNIT_RES = re.compile(r"^(OK|FAILED)\b(?: \(([^)]*)\))?", re.M)
_JEST = re.compile(r"^Tests:\s+(.*?\d+ total)\s*$", re.M)
_JEST_FILE = re.compile(r"^\s*(?:PASS|FAIL)\s+(\S+)\s+\(([\d.]+) ?(ms|s)\)", re.M)
_VITEST = re.compile(r"^\s*Tests\s+(.*\(\d+\))\s*$", re.M)
_WORDS = re.compile(r"(\d+) (passed|failed|skipped|todo|pending)")
_GO_CASE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+) \(([\d.]+)s\)", re.M)
_GO_PKG = re.compile(r"^(?:ok|FAIL)\s+\S+\s+[\d.]+s", re.M)
_CARGO = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", re.M)


def _zero():
    return {"passed": 0, "failed": 0, "skipped": 0}


def _cargo(t):
    rows = _CARGO.findall(t)
    if not rows:
        return False, None
    return True, {"passed": sum(int(a) for a, _, _ in rows), "failed": sum(int(b) for _, b, _ in rows),
                  "skipped": sum(int(c) for _, _, c in rows)}


def _word_counts(s):
    c = _zero()
    for n, word in _WORDS.findall(s):
        c["passed" if word == "passed" else "failed" if word == "failed" else "skipped"] += int(n)
    return c


def _jest(t):
    rows = _JEST.findall(t)
    return (True, _word_counts("\n".join(rows))) if rows else (False, None)


def _vitest(t):
    rows = _VITEST.findall(t)
    return (True, _word_counts("\n".join(rows))) if rows else (False, None)


def _go(t):
    cases = _GO_CASE.findall(t)
    if cases:
        c = _zero()
        for status, _, _ in cases:
            c[{"PASS": "passed", "FAIL": "failed", "SKIP": "skipped"}[status]] += 1
        return True, c
    return (True, None) if _GO_PKG.search(t) else (False, None)


def _unittest(t):
    ran = list(_UNIT_RAN.finditer(t))
    res = list(_UNIT_RES.finditer(t))
    if not ran or not res:
        return False, None
    c, paired = _zero(), 0
    for r in ran:                            # each "Ran N" pairs with the result line after it
        nxt = next((x for x in res if x.start() > r.start()), None)
        if nxt is None:
            continue
        paired += 1
        detail = dict((k.strip(), int(v)) for k, v in re.findall(r"([a-z ]+)=(\d+)", nxt.group(2) or ""))
        failed = detail.get("failures", 0) + detail.get("errors", 0) + detail.get("unexpected successes", 0)
        skipped = detail.get("skipped", 0) + detail.get("expected failures", 0)
        c["passed"] += int(r.group(1)) - failed - skipped
        c["failed"] += failed
        c["skipped"] += skipped
    return (True, c) if paired else (False, None)


def _pytest(t):
    c = None
    for line in t.splitlines():              # every summary line of every chained run
        if _CARGO.match(line):               # a cargo summary ends "... finished in 0.02s": cargo's, not pytest's
            continue
        if re.search(r" in [\d.]+s\b", line) and (_PYTEST_WORDS.search(line) or "no tests ran" in line):
            if c is None:
                c = _zero()
            for n, word in _PYTEST_WORDS.findall(line):
                if word in _PYTEST_KEY:
                    c[_PYTEST_KEY[word]] += int(n)
    return (True, c) if c is not None else (False, None)


_RUNNERS = (("cargo", _cargo), ("jest", _jest), ("vitest", _vitest), ("go", _go),
            ("unittest", _unittest), ("pytest", _pytest))


def counts(text):
    """(runner, {"passed", "failed", "skipped"}) for a test command's output, the counts
    summed over EVERY runner whose summary is in it: a chained command (`pytest tests/a
    && pytest tests/b`, `python -m unittest && pytest`) is counted whole. The name is the
    first runner found, the one whose timings() reads the output. A runner found with
    counts None (go test without -v) adds nothing, and the counts are None when every
    found runner has none; the result is (None, None) when no known runner's summary is
    in the text."""
    t = text or ""
    name, total, counted = None, _zero(), False
    for runner_name, fn in _RUNNERS:
        found, c = fn(t)
        if not found:
            continue
        if name is None:
            name = runner_name
        if c is not None:
            for k in total:
                total[k] += c[k]
            counted = True
    if name is None:
        return None, None
    return name, (total if counted else None)


def timings(runner, text):
    """[(seconds, file, name)] per test or file, for the runners that print timings."""
    t = text or ""
    if runner == "pytest":
        return [(float(s), node.split("::")[0], node) for s, _, node in _PYTEST_DUR.findall(t)]
    if runner in ("jest", "vitest"):
        return [(float(s) / (1000 if unit == "ms" else 1), f, f) for f, s, unit in _JEST_FILE.findall(t)]
    if runner == "go":
        return [(float(s), "", name) for _, name, s in _GO_CASE.findall(t)]
    return []


def _clip(name):
    name = name if len(name) <= NAME_CHARS else "..." + name[-(NAME_CHARS - 3):]
    while len(name) > 3 and len(json.dumps(name)) > NAME_CHARS + 2:
        name = "..." + name[4:]
    return name


def summary(text, rc, wall):
    """The wrapper's stdout: time per file, the slowest entries, the output's tail, and
    the QLA_RESULT line last. Kept under 4000 characters."""
    text = text or ""
    runner, c = counts(text)
    rows = timings(runner, text)
    per_file = {}
    for s, f, _ in rows:
        if f:
            per_file[f] = per_file.get(f, 0.0) + s
    top = sorted(per_file, key=lambda f: -per_file[f])[:TOP]
    out = []
    if top:
        out.append("TIME PER FILE (seconds, summed):")
        out += ["  %8.2f  %s" % (per_file[f], _clip(f)) for f in top]
    if rows:
        out.append("SLOWEST:")
        out += ["  %8.2f  %s" % (s, _clip(n)) for s, _, n in sorted(rows, reverse=True)[:TOP]]
    out.append("OUTPUT TAIL:")
    out.append(text[-TAIL_CHARS:].rstrip())
    res = {"rc": rc, "wall": round(wall, 3), "runner": runner,
           "files": [_clip(f) for f in top] if top else None}
    res.update(c if c is not None else {"passed": None, "failed": None, "skipped": None})
    out.append(MARK + json.dumps(res, sort_keys=True))
    return "\n".join(out) + "\n"


def main(argv):
    """argv: [script, bash, cmd, label]; the label only keeps run_cmd's cache key distinct."""
    bash, cmd = argv[1], argv[2]
    fd, path = tempfile.mkstemp(prefix="qla-run-", suffix=".out")
    try:
        with os.fdopen(fd, "wb") as fh:
            t0 = time.perf_counter()
            rc = subprocess.call([bash, "-c", cmd], stdin=subprocess.DEVNULL, stdout=fh,
                                 stderr=subprocess.STDOUT)
            wall = time.perf_counter() - t0
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    sys.stdout.buffer.write(summary(text, rc, wall).encode("utf-8", "replace"))
    sys.stdout.buffer.flush()
    return rc


def wrap(cmd, label, python=None, bash="bash"):
    """A bash command that runs main() on `cmd`: this file's source fed to python on stdin."""
    python = python or pathlib.Path(sys.executable).as_posix()
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    return "%s - %s %s %s <<'%s'\n%s\n%s\n" % (shlex.quote(python), shlex.quote(bash), shlex.quote(cmd),
                                             shlex.quote(label), EOF, src, EOF)


def parse_result(tail):
    """The last QLA_RESULT line's object, or None."""
    for line in reversed((tail or "").splitlines()):
        if line.startswith(MARK):
            try:
                data = json.loads(line[len(MARK):])
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def outcome(res):
    """A run_cmd result as the workflow uses it. ok = the patch applied, no timeout, a
    QLA_RESULT line with rc 0, and no failed test."""
    tail = res.get("output_tail") or ""
    r = parse_result(tail) or {}
    applied = res.get("applied", True)
    ok = bool(applied and not res.get("timed_out") and r.get("rc") == 0 and not r.get("failed"))
    profile = tail.rsplit(MARK, 1)[0] if MARK in tail else tail
    return {"ok": ok, "applied": applied, "timed_out": bool(res.get("timed_out")),
            "rc": r.get("rc", res.get("rc")), "wall": r.get("wall"), "runner": r.get("runner"),
            "passed": r.get("passed"), "failed": r.get("failed"), "files": r.get("files"),
            "profile": profile.strip()}


if __name__ == "__main__":       # `python - ARGS <<EOF` runs this source as __main__
    sys.exit(main(sys.argv))
