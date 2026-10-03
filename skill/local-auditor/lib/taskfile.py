"""Task files: the goal, the spec, and a checklist whose items carry their own checks.

Claude writes the task file in the main session. The local model never edits it:
the supervisor copies it into the run directory and reads only that copy.
"""
import re
import shlex
from dataclasses import dataclass, field

_ITEM = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$")
_CHECK = re.compile(r"\s+--\s+check:\s*(test|cmd|none)\b\s*(.*?)\s*$")
_CHECKISH = re.compile(r"check:", re.I)


@dataclass(frozen=True)
class Item:
    index: int
    text: str
    kind: str
    arg: str


@dataclass(frozen=True)
class Task:
    text: str
    items: list = field(default_factory=list)


def parse(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    items = []
    for line in text.split("\n"):
        m = _ITEM.match(line)
        if not m:
            continue
        body, kind, arg = m.group(1), "none", ""
        n = len(items) + 1
        c = _CHECK.search(" " + body)
        if c:
            kind, arg = c.group(1), c.group(2)
            body = (" " + body)[:c.start()].strip()
        elif _CHECKISH.search(body):
            # A line that LOOKS like a check but does not read exactly as one
            # must not be downgraded to `none`: that would let the item pass
            # without anything ever checking it.
            raise ValueError("item %d: unknown check kind in %r "
                             "(expected exactly '-- check: test ID', '-- check: cmd CMD' "
                             "or '-- check: none')" % (n, body))
        if kind in ("test", "cmd") and not arg:
            raise ValueError("item %d: 'check: %s' needs an argument" % (n, kind))
        if kind == "cmd":
            try:
                shlex.split(arg)
            except ValueError as exc:
                raise ValueError("item %d: cannot split the command: %s" % (n, exc)) from None
        items.append(Item(n, body, kind, arg))
    if not items:
        raise ValueError("no checklist items found (expected lines like '- [ ] text -- check: test ID')")
    return Task(text, items)
