"""Task files: the goal, the spec, and a checklist whose items carry their own checks.

Claude writes the task file in the main session. The local model never edits it:
the supervisor copies it into the run directory and reads only that copy.
"""
import re
import shlex
from dataclasses import dataclass, field

_ITEM = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$")
# A `-- check: cmd` runs as argv (checks._cmd -> testrun.split_command -> Popen), so
# shell syntax in it can never work and the loop would burn rounds on exit 11/12.
# Refuse it when the task file is parsed, with the way out in the message.
# A whole unquoted word that is an operator: &&, ||, |, ;, &, (, ), <, >, >>, 2>&1, &>.
# An operator INSIDE a word (--opt="a&&b", tests/a(1).py, foo$) is passed to the program
# as data by argv, so it is not refused.
_SHELL_OP = re.compile(r"^(?:[|&;()]+|[0-9]*[<>]{1,2}&?[0-9-]*|&>>?)$")
_SHELL_BUILTINS = ("cd", "export", "source", ".")   # shell builtins: no program to exec
_ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")    # FOO=1 cmd: an assignment, not a program
_SHELL_HINT = ("item %d: 'check: cmd' runs without a shell, so '%s' cannot work; "
               "wrap it: -- check: cmd sh -c 'cd sub && pytest -q'")
# One marker, not the whole tail: the LAST occurrence on the line is the check, and
# everything before it -- earlier " -- check:" text included -- is item text.
_CHECK = re.compile(r"\s+--\s+check:\s*(test|cmd|none)\b")
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


def _words(arg):
    """(word, quoted) pairs split the way a POSIX shell would, keeping whether any part
    of the word was quoted or backslash-escaped -- '&&' in quotes is data, not syntax."""
    words, cur, quoted, q, i = [], [], False, None, 0
    while i < len(arg):
        c = arg[i]
        if q:
            if c == q:
                q = None
            elif c == "\\" and q == '"' and i + 1 < len(arg):
                i += 1
                cur.append(arg[i])
            else:
                cur.append(c)
        elif c in "'\"":
            q, quoted = c, True
        elif c == "\\" and i + 1 < len(arg):
            i += 1
            cur.append(arg[i])
            quoted = True
        elif c.isspace():
            if cur or quoted:
                words.append(("".join(cur), quoted))
            cur, quoted = [], False
        else:
            cur.append(c)
        i += 1
    if cur or quoted:
        words.append(("".join(cur), quoted))
    return words


def _scan_cmd(n, arg):
    """Refuse a cmd argument that only a shell could run: an unquoted operator word, or a
    leading shell builtin or NAME=value assignment."""
    for k, (word, quoted) in enumerate(_words(arg)):
        if quoted:
            continue
        if k == 0 and (word in _SHELL_BUILTINS or _ENV_PREFIX.match(word)):
            raise ValueError(_SHELL_HINT % (n, word))
        if _SHELL_OP.match(word):
            raise ValueError(_SHELL_HINT % (n, word))


def parse(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    items = []
    for line in text.split("\n"):
        m = _ITEM.match(line)
        if not m:
            continue
        body, kind, arg = m.group(1), "none", ""
        n = len(items) + 1
        # The leading space: a suffix may sit at the very start of the item text.
        padded = " " + body
        hits = list(_CHECK.finditer(padded))
        if hits:
            # The LAST suffix is the check: prose quoting " -- check: none" must not
            # silently downgrade the item's real check to UNVERIFIED.
            c = hits[-1]
            kind, arg = c.group(1), padded[c.end():].strip()
            body = padded[:c.start()].strip()
        elif _CHECKISH.search(body):
            # A line that LOOKS like a check but does not read exactly as one
            # must not be downgraded to `none`: that would let the item pass
            # without anything ever checking it.
            raise ValueError("item %d: unknown check kind in %r "
                             "(expected exactly '-- check: test ID', '-- check: cmd CMD' "
                             "or '-- check: none')" % (n, body))
        if hits and _CHECKISH.search(arg):
            # Something after the chosen marker still reads like one ("-- check: foo"
            # with an unknown kind): two markers on a line is ambiguous, so say so.
            raise ValueError("item %d: more than one '-- check:' marker in %r; "
                             "put exactly one, at the end" % (n, " " + m.group(1)))
        if kind == "none" and arg:
            # "-- check: none" takes nothing after it: text there is either a cmd/test
            # argument that was cut in two, or prose that hides what was meant.
            raise ValueError("item %d: 'check: none' takes no argument (got %r)" % (n, arg))
        if kind in ("test", "cmd") and not arg:
            raise ValueError("item %d: 'check: %s' needs an argument" % (n, kind))
        if kind == "cmd":
            try:
                shlex.split(arg)
            except ValueError as exc:
                raise ValueError("item %d: cannot split the command: %s" % (n, exc)) from None
            _scan_cmd(n, arg)
        items.append(Item(n, body, kind, arg))
    if not items:
        raise ValueError("no checklist items found (expected lines like '- [ ] text -- check: test ID')")
    return Task(text, items)
