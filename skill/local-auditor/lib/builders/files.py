"""files: an item is one path.

Decidable by construction: whole file if it fits the item budget, otherwise
WITHHELD with a reason telling the caller to narrow the query (or raise the
budget for a large-context model). The earlier draft said "symbol windows if
large", but a file item names no symbols to window around, so that was not
implementable as written.
"""
import glob as _glob
import os

from .base import Built, item_budget

DEFAULT_BRIEF = "review"
REQUIRED_ARGS = {"glob": "--glob PATTERN (relative to --repo)"}


def enumerate_items(args):
    """args: {glob, repo}. A relative glob is resolved under repo, not the cwd."""
    repo = args.get("repo", "")
    pat = args["glob"]
    if repo and not os.path.isabs(pat):
        pat = os.path.join(repo, pat)
    return [{"path": p, "repo": repo}
            for p in sorted(_glob.glob(pat, recursive=True))
            if os.path.isfile(p)]


def build(item):
    """Whole file under budget, else withheld. No verdict vocabulary."""
    path = item["path"]
    repo = item.get("repo") or ""
    label = os.path.basename(path)
    if repo:
        try:
            rel = os.path.relpath(path, repo)
        except ValueError:          # Windows: path and repo on different drives
            rel = ""
        if rel and not rel.startswith(".."):
            label = rel.replace("\\", "/")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return Built("", "", 0, "cannot stat the file: %s" % exc, None, label)
    budget = item_budget()
    if size > budget:
        return Built("", "", 0,
                     "file is %d bytes, over the %d byte item budget -- narrow the query "
                     "or raise --item-budget" % (size, budget), None, label)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError as exc:
        return Built("", "", 0, "cannot read the file: %s" % exc, None, label)
    if not src.strip():
        return Built("", "", 0, "file is empty", None, label)

    lines = src.splitlines()
    body = "File under review: %s (%d lines)" % (label, len(lines))
    context = "\n".join(["# Code context", "",
                         "### %s  (%d lines, complete)" % (label, len(lines)), "", "```"]
                        + ["%6d| %s" % (i + 1, l) for i, l in enumerate(lines)]
                        + ["```", ""])
    return Built(body, context, len(body) + len(context), None, None, label)
