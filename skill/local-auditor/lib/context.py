"""Evidence extraction: what the model is allowed to see, and how it is ranked.

Language-agnostic core with language refinements. The refinement matters: it is
the fix for the one recorded FAIL-OPEN, where the model cited a module docstring
DESCRIBING a fix as evidence the fix had landed.

MEASURED. The previous per-line test recognised only a docstring's OPENING
delimiter, so every subsequent line of a multi-line docstring was classified as
code -- and because excerpt() ranks code hits ABOVE prose hits, the ranking meant
to demote docstrings actively PROMOTED their bodies. In one 611-line module a
symbol's hits were [1, 10, 28, 238] and the "code" hits were [10, 28, 238]: all
three inside module docstrings.

Python docstrings are located with `ast`, so a string used as a VALUE
(`raise ValueError("CONFIG_ROOT unset")`) still counts as CODE -- only bare
string EXPRESSION STATEMENTS are prose. C-family files get a comment state
machine for // and /* */ (JSDoc included); it ignores string literals, so it is
approximate. Everything else falls back to `#` comments and triple quotes.
"""
import ast
import os
import re

WIN = 18             # lines of context each side of a hit
MAX_HITS = 8         # per symbol
MAX_CTX_LINES = 700  # bounded, but deep enough to reach code in a 4,000-line file

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "vendor", "third_party",
    "target", "build", "dist",
})

C_FAMILY = frozenset({
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".java", ".kt", ".kts",
    ".scala", ".groovy", ".go", ".rs", ".swift", ".dart", ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx", ".vue", ".svelte", ".php", ".m", ".mm",
})

TRIPLE = re.compile(r'"""' + "|" + r"'''")
# Test FILES outside a test directory: go's x_test.go, jest's x.test.ts / x.spec.ts, pytest's test_x.py.
_TEST_FILE = re.compile(r"(?:_test\.[^.]+|\.(?:test|spec)\.[^.]+|^test_[^.]*\.py)$", re.I)


def _prose_mask_fallback(lines):
    """Delimiter state machine for `#`-comment languages and unparseable Python.

    KNOWN HAZARD: this understands only Python-style triple quotes and `#`
    comments. A block-comment language outside C_FAMILY will have its comment
    blocks classified as CODE and promoted into evidence -- the exact fail-open
    class the other paths fix. Treat such findings with less trust.
    """
    mask, delim = [], None
    for line in lines:
        starts_inside = delim is not None
        for m in TRIPLE.finditer(line):
            tok = m.group(0)
            if delim is None:
                delim = tok
            elif delim == tok:
                delim = None
        t = line.strip()
        mask.append(bool(starts_inside or (not t) or t[0] == "#"
                         or t[:3] in ('"""', "'''")))
    return mask


def _prose_mask_c(lines):
    """// and /* */ comments. A line is prose when it holds no code outside comments."""
    mask, in_block = [], False
    for s in lines:
        i, has_code = 0, False
        while i < len(s):
            if in_block:
                j = s.find("*/", i)
                if j < 0:
                    break
                in_block, i = False, j + 2
                continue
            j, k = s.find("/*", i), s.find("//", i)
            if k >= 0 and (j < 0 or k < j):
                has_code = has_code or bool(s[i:k].strip())
                break
            if j < 0:
                has_code = has_code or bool(s[i:].strip())
                break
            has_code = has_code or bool(s[i:j].strip())
            in_block, i = True, j + 2
        mask.append(not has_code)
    return mask


def prose_mask(lines, src, path=""):
    """Per line: True where it is a docstring body, a comment, or blank."""
    ext = os.path.splitext(path)[1].lower()
    if ext in C_FAMILY:
        return _prose_mask_c(lines)
    if ext != ".py":
        return _prose_mask_fallback(lines)
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return _prose_mask_fallback(lines)

    mask = [bool((not l.strip()) or l.strip()[0] == "#") for l in lines]
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for stmt in body:
            if (isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)):
                for ln in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1):
                    if 1 <= ln <= len(mask):
                        mask[ln - 1] = True
    return mask


def excerpt(path, symbols, win=WIN, max_hits=MAX_HITS, max_lines=MAX_CTX_LINES):
    """Windows around each symbol hit, CODE hits ranked above prose hits."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return []
    lines = src.splitlines()
    prose = prose_mask(lines, src, path)
    keep, out = set(), []
    for s in symbols:
        all_hits = [i for i, l in enumerate(lines) if s in l]
        code_hits = [i for i in all_hits if not prose[i]]
        for h in (code_hits or all_hits)[:max_hits]:
            keep.update(range(max(0, h - win), min(len(lines), h + win + 1)))
    for i in sorted(keep):
        out.append("%6d| %s" % (i + 1, lines[i]))
        if len(out) >= max_lines:
            out.append("   ...| [TRUNCATED at %d lines]" % max_lines)
            break
    return out


def symbol_census(symbols, root, test_pat="tests", exts=(".py",), skip_dirs=SKIP_DIRS):
    """Repo-wide hit counts per symbol, production vs tests.

    This is what answers an ABSENCE claim ("X is set by nothing"); a window
    cannot, because a window only shows what IS present. A symbol appearing only
    under tests is positive evidence FOR such a claim.

    `test_pat` is a directory name or a list of them; files named like tests
    (x_test.go, x.test.ts, x.spec.js, test_x.py) count as tests anywhere.
    """
    test_dirs = {test_pat} if isinstance(test_pat, str) else set(test_pat or ())
    test_dirs = {d.lower() for d in test_dirs if d}
    exts = tuple(e.lower() for e in exts)
    counts = {s: [0, 0] for s in symbols}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        rel = os.path.relpath(dirpath, root).lower()
        in_test_dir = any(part in test_dirs for part in rel.split(os.sep))
        for fn in filenames:
            if not fn.lower().endswith(exts):
                continue
            is_test = in_test_dir or bool(_TEST_FILE.search(fn))
            try:
                with open(os.path.join(dirpath, fn), encoding="utf-8",
                          errors="replace") as fh:
                    body = fh.read()
            except OSError:
                continue
            for s in symbols:
                c = body.count(s)
                if c:
                    counts[s][1 if is_test else 0] += c
    rows = ["%-38s %10s %7s" % ("symbol", "production", "tests")]
    for s in symbols:
        p, t = counts[s]
        if t and not p:
            flag = "   <-- TESTS ONLY (not wired into production)"
        elif not p and not t:
            flag = "   <-- ABSENT from the scanned file types"
        else:
            flag = ""
        rows.append("%-38s %10d %7d%s" % (s, p, t, flag))
    rows.append("(scanned %s; test dirs: %s)" % (" ".join(exts), ", ".join(sorted(test_dirs))))
    return "\n".join(rows)
