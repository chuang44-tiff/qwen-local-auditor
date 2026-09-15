# Batch sweeps: mechanics and writing a builder

## The pipeline

```
enumerate_items(args) -> [item]      # builder
build(item) -> Built                 # builder, pure
plan_batches(builts, budget)         # engine: fill a BYTE budget
write_batch(dir, builts)             # engine: only the engine writes
qwen-agent -C <batch dir> -f brief   # dispatch, per batch
batch_status(dir)                    # engine: every expected block present? else retry once
collate(root, repo)                  # engine: parse, completeness, invariant 8
```

Run `--dry-run` first on any new sweep. It builds everything and dispatches nothing, so
you can see what was withheld and how the batches fell before spending time.

## The builder contract

```python
DEFAULT_BRIEF = "review"
REQUIRED_ARGS = {"glob": "--glob PATTERN"}   # checked before enumerate_items runs

def enumerate_items(args) -> list[dict]      # optional
def build(item) -> Built                     # required; pure, writes nothing
item_budget()                                # the per-item byte budget; read it at call time

Built(body, context, size, withheld, clauses, label)
```

- **`withheld`** is `None` (dispatch) or a **reason string** (do not). Never a bool —
  `Built` raises `TypeError` on one, because a bool is an int in Python and would pass an
  `is None` check while carrying no reason.
- **`clauses`** is `None`, or a list of at least two clause ids. Only clause-shaped
  questions (claims) use it; a diff review has no claim to adjudicate.
- **The builder enumerates clauses, never the model.** If the model decided how many
  clauses an item had, a dropped block would be indistinguishable from an item that
  simply had fewer — and completeness checking is the only thing that catches a dropped
  block.
- `build` must not write files. The engine writes, which is what guarantees a withheld
  item can never reach a batch directory and that surviving items are numbered densely.

## Why the engine owns so much

Each of these was a real failure before it was a rule:

- **`-C` at the batch dir, not the repo.** Prose telling the model not to read whole
  files failed twice; a batch dir with nothing else in it does not. Note this is defense
  in depth, not a fence — `Read` takes absolute paths. The actual fence is the read-only
  toolset plus `--strict-mcp`.
- **Byte budget, not a fixed count.** Seven small files and seven files with huge hunks
  are not the same batch. The budgets are scaled from the model's context window, which
  `qwen-agent --preflight-only` reports.
- **`mkdir` lock**, because it is atomic. Three concurrent runners once deleted each
  other's batch directories.
- **Batch dirs live under the user cache dir** (`$QWEN_SWEEP_CACHE`, else
  `$XDG_CACHE_HOME/qwen-sweep`, else `~/.cache/qwen-sweep`), never in the target repo — otherwise a diff sweep
  sees its own output as a change.
- **Nothing audited is a failure** (exit 9): a glob that matches nothing, a bad ref, or a
  corpus where every item was withheld must never read as a clean pass.
- **`{{N}}` templating,** not `sed` against brief prose, which broke silently whenever
  anyone reworded the brief.

## Writing a new builder

1. Create `lib/builders/<name>.py` with `DEFAULT_BRIEF`, `REQUIRED_ARGS`, `enumerate_items`
   and `build`, and add the name to `BUILDER_NAMES` in `lib/builders/base.py`.
2. Return a `withheld` **reason** whenever the item has no usable evidence. This is the
   single most important thing you will write: it is what stops the sweep asking a
   question it cannot answer.
3. Add the name to `BUILDERS` in `tests/test_builders.py` — the contract test then
   applies to you automatically, including "an item with no evidence must be withheld".
4. Point `DEFAULT_BRIEF` at a brief whose context shape matches what you actually build.
   Briefs and builders are not interchangeable: the claims brief promises a symbol
   census, and pointing it at diff output is nonsense.
5. A team's own brief does not need a fork: put `NAME.md` in `$QWEN_BRIEF_DIR` and run
   with `--brief NAME`. It must declare `{{N}}` and `{{ITEMS}}` and ask for the block
   format in `lib/blocks.py`.
