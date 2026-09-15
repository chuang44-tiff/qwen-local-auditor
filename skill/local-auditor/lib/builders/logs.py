"""logs: an item is one chunk of a large text.

The long-context grunt-work lane. No verdict vocabulary; the brief asks for a
structured summary grounded in quoted lines.

Chunks are cut on LINE boundaries to fit the per-item byte budget, so nothing is
silently dropped: a log with long JSON lines simply yields more, smaller chunks.
(Chunking by a fixed line count used to truncate such chunks to a fraction of
their lines.) Only a single line longer than the whole budget is cut, and the
chunk says so.
"""
from .base import Built, item_budget

DEFAULT_BRIEF = "summarize"
REQUIRED_ARGS = {"input": "--input FILE"}

_PREFIX = 8            # "%6d| " line-number prefix, plus the newline


def enumerate_items(args):
    """args: {input, chunk_bytes?}"""
    path = args["input"]
    limit = int(args.get("chunk_bytes") or 0) or max(1000, item_budget() - 500)
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    items, cur, used, first = [], [], 0, 1
    for n, line in enumerate(lines, 1):
        cost = len(line.encode("utf-8")) + _PREFIX
        if cur and used + cost > limit:
            items.append(_item(path, first, cur))
            cur, used, first = [], 0, n
        cur.append(line)
        used += cost
    if cur:
        items.append(_item(path, first, cur))
    return items


def _item(path, first, lines):
    return {"lines": lines, "first": first,
            "label": "%s:%d-%d" % (path, first, first + len(lines) - 1)}


def build(item):
    """Pass the chunk through with line numbers. No verdict vocabulary."""
    label = item.get("label", "chunk")
    if "lines" in item:
        first, lines = item.get("first", 1), item["lines"]
    else:                                   # a caller may still hand over raw text
        first, lines = 1, item.get("text", "").splitlines()
    if not any(l.strip() for l in lines):
        return Built("", "", 0, "chunk is empty", None, label)
    budget = item_budget()
    numbered, used = [], 0
    for i, l in enumerate(lines):
        row = "%6d| %s" % (first + i, l)
        if used + len(row.encode("utf-8")) + 1 > budget:
            numbered.append(row.encode("utf-8")[:max(0, budget - used)].decode("utf-8", "ignore"))
            numbered.append("   ...| [a line longer than the item budget was cut here]")
            break
        numbered.append(row)
        used += len(row.encode("utf-8")) + 1
    body = "Log chunk: %s" % label
    context = "\n".join(["# Text", "", "```"] + numbered + ["```", ""])
    return Built(body, context, len(body) + len(context), None, None, label)
