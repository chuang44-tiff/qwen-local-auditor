"""diff: an item is one changed file in a commit range or in the uncommitted work.

Deliberately has NO clause vocabulary. A diff review has no claim to adjudicate,
and forcing a verdict onto one produces exactly the false confidence this lane
is prone to. The brief asks what each hunk does and whether any cited line
contradicts the stated intent -- extraction, not judgement.

Git is driven defensively, because user configuration changes its output:
colour and external diff drivers are turned off, paths are NUL-separated and
unquoted (non-ASCII names), and `--relative` keeps a --repo that points at a
subdirectory scoped to that subdirectory. Any git failure (not a repository, a
mistyped --base) is an ERROR, never "no changes".
"""
import os
import re
import subprocess

from .base import Built, item_budget

DEFAULT_BRIEF = "review"
REQUIRED_ARGS = {}

WIN = 12
HUNK_RE = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.M)
_GIT_CFG = ["-c", "core.quotePath=false", "-c", "color.ui=never", "-c", "diff.noprefix=false"]
_DIFF = ["diff", "--no-color", "--no-ext-diff", "--relative"]
MAX_INTENT = 4000


class GitError(RuntimeError):
    pass


def _git(repo, *args):
    try:
        p = subprocess.run(["git", "-C", repo, *_GIT_CFG, *args], capture_output=True,
                           encoding="utf-8", errors="replace", check=False)
    except OSError as exc:
        raise GitError("cannot run git: %s" % exc) from None
    if p.returncode != 0:
        why = (p.stderr or "").strip().splitlines()
        raise GitError("git %s failed: %s" % (args[0], why[-1] if why else "exit %d" % p.returncode))
    return p.stdout


def enumerate_items(args):
    """args: {repo, base?}. Without base: uncommitted changes (staged and unstaged) vs HEAD.

    Untracked files are not part of a diff; `git add -N` them to include them.
    """
    repo = args["repo"]
    _git(repo, "rev-parse", "--git-dir")                 # a clear error outside a repository
    base = args.get("base")
    rng = [base] if base else ["HEAD"]
    names = [n for n in _git(repo, *_DIFF, "--name-only", "-z", "--diff-filter=d",
                             *rng, "--").split("\0") if n]
    intent = ""
    if base:
        intent = _git(repo, "log", "--no-color", "--format=%B", "%s..HEAD" % base).strip()
        if len(intent) > MAX_INTENT:
            intent = intent[:MAX_INTENT] + "\n[commit messages truncated]"
    items = []
    for n in names:
        patch = _git(repo, *_DIFF, "--unified=0", *rng, "--", n)
        hunks = [(int(a), int(b or 1)) for a, b in HUNK_RE.findall(patch)]
        items.append({"path": n, "abspath": os.path.join(repo, n), "hunks": hunks,
                      "repo": repo, "patch": patch, "intent": intent})
    return items


def build(item):
    """The -U0 patch plus post-image windows around each hunk. No verdict vocabulary."""
    path, label = item["abspath"], item["path"]
    hunks = item.get("hunks") or []
    if not hunks:
        return Built("", "", 0, "no hunks in this file for the given range", None, label)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return Built("", "", 0, "cannot read the post-image: %s" % exc, None, label)

    keep = set()
    for start, count in hunks:
        lo = max(0, start - 1 - WIN)
        hi = min(len(lines), start - 1 + max(count, 1) + WIN)
        keep.update(range(lo, hi))
    if not keep:
        return Built("", "", 0, "hunks fell outside the post-image", None, label)

    body = "Changed file: %s\nChanged line ranges (post-image): %s" % (
        label, ", ".join("%d+%d" % h for h in hunks))
    if item.get("intent"):
        body += "\n\nCommit messages in this range (the stated intent):\n\n" + item["intent"]

    out = ["# Code context", ""]
    patch_lines = [l for l in (item.get("patch") or "").splitlines()
                   if l.startswith(("@@", "+", "-")) and not l.startswith(("+++", "---"))]
    if patch_lines:
        out += ["## Patch: removed (-) and added (+) lines", "", "```diff"] + patch_lines + ["```", ""]
    out += ["### %s  (%d lines total; windows around changed lines only)" % (label, len(lines)),
            "", "```"]
    out += ["%6d| %s" % (i + 1, lines[i]) for i in sorted(keep)]
    out += ["```", ""]
    context = "\n".join(out)
    budget = item_budget()
    if len(context) > budget:
        context = context[:budget] + "\n[TRUNCATED at item budget]\n"
    return Built(body, context, len(body) + len(context), None, None, label)
