"""claims: an item is a document asserting things about code.

A ticket, a design note, a changelog entry, a review comment: anything that says
"X is fixed" or "nothing reads Y". It is the only builder whose question is
clause-shaped, so it is the only one that enumerates clauses and the only one
whose brief declares a verdict vocabulary.

Language coverage: files are found by any common source extension, and the
symbol census scans the extensions of the files the document names (so a
claim about `handler.go` is not "proved" by counting only .py files). The
docstring/comment guard is exact for Python and approximate for C-family
languages; see reference/limits.md.
"""
import os
import re

from lib.context import excerpt, symbol_census, SKIP_DIRS
from .base import Built, item_budget

DEFAULT_BRIEF = "claims"
REQUIRED_ARGS = {"docs": "--docs DIR",
                 "items": "--items FILE (a JSON list of file names inside --docs)"}

SOURCE_EXTS = (
    ".py", ".pyi", ".pyx", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".groovy", ".c", ".h", ".cc", ".cpp",
    ".cxx", ".hpp", ".hh", ".cs", ".fs", ".rb", ".php", ".swift", ".m", ".mm", ".dart",
    ".lua", ".pl", ".pm", ".r", ".jl", ".ex", ".exs", ".erl", ".hs", ".ml", ".clj",
    ".sh", ".bash", ".zsh", ".ps1", ".sql",
)
DEFAULT_TEST_DIRS = ("tests", "test", "__tests__", "spec", "specs")
# Metadata such as "- **Status:** done" or "Status: open" describes the document,
# not the code, and would bias the model toward the document's own conclusion.
DEFAULT_STRIP_FIELDS = ("Status",)

# Backticked identifiers, qualified names included: `Foo`, `obj.method()`, `Foo::bar`.
SYM = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::|->)[A-Za-z_][A-Za-z0-9_]*)*)(?:\(\))?`")
MIN_SYM_LEN = 3
CLAUSE = re.compile(r"^\s*(\d+)[.)]\s+\S", re.M)

MAX_SYMS, MAX_FILES = 14, 4


def _as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s) for s in v]


def _strip_re(fields):
    names = "|".join(re.escape(f) for f in fields)
    return re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:%s)(?:\*\*)?\s*:" % names, re.M | re.I)


def _file_re(exts):
    alts = "|".join(sorted((re.escape(e.lstrip(".")) for e in exts), key=len, reverse=True))
    return re.compile(r"(?<![\w./\\-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:%s))(?![\w])" % alts)


def enumerate_items(args):
    """args: {docs, repo, items, census_root?, test_dirs?, strip_fields?, exts?}

    docs is resolved under repo when relative, the same way --glob is.
    """
    repo = args["repo"]
    docs = args["docs"]
    if not os.path.isabs(docs):
        docs = os.path.join(repo, docs)
    names = args["items"]
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("--items must be a JSON list of file names inside --docs")
    test_dirs = _as_list(args.get("test_dirs") or args.get("test_pat")) or list(DEFAULT_TEST_DIRS)
    return [{"doc": os.path.join(docs, n.replace("\\", "/")), "repo": repo,
             "census_root": args.get("census_root", repo),
             "test_dirs": test_dirs,
             "strip_fields": _as_list(args.get("strip_fields")) or list(DEFAULT_STRIP_FIELDS),
             "exts": _as_list(args.get("exts")) or None}
            for n in names]


def _find_file(root, name):
    """Resolve a named file. A path with directories must match that path; a bare
    name matches by basename, shortest relative path first. Returns (path, n_matches)."""
    if "/" in name:
        direct = os.path.join(root, name)
        if os.path.isfile(direct):
            return direct, 1
    base = os.path.basename(name)
    suffix = "/" + name if "/" in name else None
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if base in filenames:
            p = os.path.join(dirpath, base)
            rel = "/" + os.path.relpath(p, root).replace("\\", "/")
            if suffix is None or rel.endswith(suffix):
                found.append(p)
    found.sort(key=lambda p: (p.count(os.sep), p))
    return (found[0], len(found)) if found else (None, 0)


def build(item):
    """Build the census + code windows for one claims document."""
    with open(item["doc"], encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    strip = _strip_re(item.get("strip_fields") or DEFAULT_STRIP_FIELDS)
    body = "\n".join(l for l in text.splitlines() if not strip.match(l))
    label = os.path.basename(item["doc"])
    if not body.strip():
        return Built("", "", 0, "the document body is empty after stripping metadata lines",
                     None, label)

    # Search code for the last component of a qualified name: `obj.method()` -> method.
    tails = {re.split(r"\.|::|->", s)[-1] for s in SYM.findall(text)}
    syms = sorted(s for s in tails if len(s) >= MIN_SYM_LEN)[:MAX_SYMS]
    exts = tuple(item.get("exts") or SOURCE_EXTS)
    named = sorted(set(_file_re(exts).findall(text)))[:MAX_FILES]
    repo = item["repo"]
    census_exts = tuple(sorted({os.path.splitext(n)[1].lower() for n in named})) or exts
    test_dirs = item.get("test_dirs") or [item.get("test_pat") or "tests"]

    chunks = [
        "# Code context", "",
        "## Repo-wide symbol census", "",
        "Occurrences across the tree. Use this for claims of the form \"X is set by",
        "nothing\" or \"nothing reads Y\": a symbol appearing ONLY under tests is not",
        "wired into production, which is positive evidence FOR such a claim. The census",
        "covers only the file types listed under it; a symbol used elsewhere is invisible.", "",
        "```",
        symbol_census(syms, item.get("census_root", repo), test_dirs, census_exts)
        if syms else "(no symbols named)",
        "```", "",
        "## Source windows", "",
        "Real line numbers. Code occurrences are preferred over comments AND over",
        "docstring bodies. Nothing outside these windows is available to you.", "",
    ]

    windows = 0
    for name in named:
        path, matches = _find_file(repo, name)
        if not path:
            chunks.append("### %s\n\nNOT FOUND IN TREE (file may be deleted).\n" % name)
            continue
        rel = os.path.relpath(path, repo).replace("\\", "/")
        note = "" if matches < 2 else "; %d files share this name, showing the shortest path" % matches
        with open(path, encoding="utf-8", errors="replace") as fh:
            total = sum(1 for _ in fh)
        ex = excerpt(path, syms)
        if not ex:
            chunks.append("### %s  (%d lines%s)\n\nNone of the named symbols appear here.\n"
                          % (rel, total, note))
        else:
            windows += 1
            chunks.append("### %s  (%d lines total; windows only%s)\n\n```\n%s\n```\n"
                          % (rel, total, note, "\n".join(ex)))

    if windows == 0:
        return Built(body, "", 0,
                     "no source windows (syms=%d files=%d)" % (len(syms), len(named)),
                     None, label)

    context = "\n".join(chunks)
    budget = item_budget()
    if len(context) > budget:
        context = context[:budget] + "\n[TRUNCATED at item budget]\n"

    # De-duplicated: two numbered lists in one document must not yield duplicate keys,
    # which completeness checking could never tell apart.
    nums = list(dict.fromkeys(CLAUSE.findall(text)))
    clauses = nums if len(nums) >= 2 else None
    return Built(body, context, len(body) + len(context), None, clauses, label)
