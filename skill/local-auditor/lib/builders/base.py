"""The builder contract.

A builder turns ONE item into evidence and is otherwise pure: it does not write
files, does not know its index, and does not know about batching. The engine
owns all of that, which is what lets the engine guarantee two things no builder
can accidentally break -- withheld items are never written, and surviving items
are numbered contiguously.

`withheld` is the load-bearing field. It is a REASON string, not a count.
MEASURED: items with no evidence returned CANNOT_DETERMINE 100% of the time,
and 30 of 72 items (42%) in one real corpus were in that class while the runner
dispatched them anyway. A reason (not a bare 0) is what `needs-human.txt`
actually needs in order to route the item to a person.

A builder module declares:

    DEFAULT_BRIEF = "review"                       # brief name in lib/briefs/
    REQUIRED_ARGS = {"glob": "--glob PATTERN"}     # arg key -> how the user supplies it
    def enumerate_items(args) -> list[dict]
    def build(item) -> Built
"""
import importlib
import re
from dataclasses import dataclass

MAX_ITEM_BYTES = 120_000     # default per-item context budget; qwen-sweep scales it to the window
_item_budget = MAX_ITEM_BYTES

BUILDER_NAMES = ("claims", "diff", "files", "logs")


def set_item_budget(n):
    """Set the per-item byte budget for this process (the engine does, per run)."""
    global _item_budget
    _item_budget = max(1000, int(n))


def item_budget():
    """The per-item byte budget in force. Read at call time, never imported by value."""
    return _item_budget


@dataclass
class Built:
    body: str                # the item, restated for the model
    context: str             # the evidence, with path headers
    size: int                # bytes, for the engine's byte budget
    withheld: "str | None"   # None = dispatch. A string = the reason not to.
    clauses: "list[str] | None"   # clause-shaped questions enumerate; others None
    label: str

    def __post_init__(self):
        # A bool is an int in Python and would silently pass an `is None` check
        # while carrying no reason. Reject the old `windows`-shaped mistake loudly.
        if self.withheld is not None and not isinstance(self.withheld, str):
            raise TypeError("withheld must be None or a reason string, got %r"
                            % type(self.withheld).__name__)
        if isinstance(self.clauses, list) and len(self.clauses) < 2:
            raise ValueError("clauses must be None or hold at least two clauses")


_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")


def load_builder(name):
    """Import a builder by bare name. Rejects anything that is not a plain, known name."""
    if not _NAME_RE.match(name or ""):
        raise ValueError("bad builder name: %r" % (name,))
    try:
        return importlib.import_module("lib.builders.%s" % name)
    except ModuleNotFoundError as exc:
        if exc.name != "lib.builders.%s" % name:
            raise
        raise ValueError("unknown builder %r (known: %s)"
                         % (name, ", ".join(BUILDER_NAMES))) from None
