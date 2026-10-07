# Batch sweeps: running `qwen-sweep`, its mechanics, and writing a builder

## Running a sweep

```bash
qwen-sweep --builder diff   --repo . --base main --dry-run       # always dry-run a new sweep first
qwen-sweep --builder diff   --repo . --base main                  # no --base: uncommitted changes vs HEAD
qwen-sweep --builder files  --repo . --glob 'src/**/*.py'
qwen-sweep --builder claims --repo . --docs docs/issues --items list.json   # list.json: ["ISSUE-12.md", ...]
qwen-sweep --builder logs   --input build.log
qwen-sweep --builder history    --repo . --arg files=src/a.py,src/b.py
qwen-sweep --builder deviations --repo . --base main --arg spec=SPEC.md
```

| builder | one item is | asks | needs |
|---|---|---|---|
| `diff` | a changed file in a range | what the change does, what contradicts its intent | `--repo` `[--base REF]` |
| `files` | a path from a glob | what the file does, demonstrable defects | `--repo --glob PAT` |
| `claims` | a document asserting things about code | a VERDICT per claim, with evidence | `--repo --docs DIR --items FILE` |
| `logs` | a chunk of a large text | what is in it, what recurs | `--input FILE` |
| `history` | what this project's Claude Code transcripts (not git) recorded about a file | why it is the way it is | `--repo --arg files=a,b` |
| `deviations` | a changed file, held against a spec; implies `--test` | does it differ from the spec, and is there a recorded reason | `--repo --arg spec=PATH` plus the diff options |

| option | meaning |
|---|---|
| `--arg KEY=VALUE` | a builder setting, repeatable. `claims`: `test_dirs=test,spec`, `strip_fields=Status,Owner`, `exts=.py,.go`, `census_root=DIR`; `logs`: `chunk_bytes=N` |
| `--brief NAME` | use brief NAME (`$QWEN_BRIEF_DIR/NAME.md`, else the bundled one) |
| `--role NAME` | the `qwen-agent` role (default `auditor`) |
| `-m NAME`, `--effort LEVEL`, `--timeout SECS` | model, effort and wall clock for every batch (set `QWEN_MODEL`, `QWEN_EFFORT`, `QWEN_TIMEOUT`) |
| `--budget BYTES`, `--item-budget BYTES` | bytes per batch (default: scaled to the model's context window) and per item (default: half the batch budget) |
| `--out DIR` | the run directory (default: a new run under the sweep cache) |
| `--dry-run` | build and report, dispatch nothing |
| `--resume` | continue the latest run for this repo, skipping complete batches |
| `--strict` | a builder exception aborts the run instead of withholding the item |
| `--allow-empty` | exit 0 even when there is nothing to audit |
| `--test` | let every batch run the project's tests via `qwen-test` (needs `QWEN_TEST_CMD`; implied by `--builder deviations`) |
| `--shallow` | batches answer once: no review round, no delegation nudge. Depth is the default (see below), so this is the opt-out for a large fan-out run |
| `--no-preflight` | skip the one-time server check (also `QWEN_PREFLIGHT=0`) |

### Depth

A sweep gives every batch the depth an auditor gets standing at the repo, and a batch is
worth the same care: each one reviews its own answer (`qwen-agent --review-round`) and is
nudged to delegate (`--subagents-nudge`), and an `--role auditor` or `--role coder` batch
reads the deep variant of that role (`--role-variant deep`). What a batch does **not** get
is the `--probe` sandbox that depth would otherwise imply: `--probe` copies a project tree,
and a batch's `-C` is a folder of extracted text, so every batch is dispatched with
`--shallow` and the depth is typed after it. `--shallow` to `qwen-sweep` itself is the
opt-out for a fan-out too wide to review twice: the batches then get plain `--shallow` and
answer once. `--timeout SECS` (or `QWEN_TIMEOUT`) is the wall clock of each qwen-agent call of a
batch: a review round is a second call with the same full timeout, so a deep batch can
take up to twice it. Batches are dispatched with `--shallow`, so their default stays
1800 s per call.

`claims` understands most source languages, but its guard against citing a comment as
evidence is exact only for Python and approximate for C-family files (see `limits.md`).
Batch edits are not a sweep job: use `--until-done` with one checklist item per file.

### Output

The run directory's path is printed at the start and the end. Runs live under
`$QWEN_SWEEP_CACHE`, else `$XDG_CACHE_HOME/qwen-sweep`, else `~/.cache/qwen-sweep`,
grouped per repo.

| file | contents |
|---|---|
| `collated.json` | every answer: `rows[]` of `{item, key, verdict, finding, evidence, why}`, plus `problems[]` and `withheld[]` |
| `needs-human.txt` | items never dispatched, with the reason (no evidence, over budget, empty): route these to a person. The final summary lists them and prints the path |
| `progress.log` | this run's console output |
| `bNN/` | one batch: `t*.md` and `context*.md` (what the model saw), `brief.md`, `out.md` (its answer), `stderr.txt` (`qwen-agent`'s messages) |

### Exit codes

| exit | meaning |
|---|---|
| 0 | every dispatched batch answered completely |
| 1 | collation problems: missing blocks, or prose cited as evidence |
| 2 | usage error (bad option, missing builder argument, unreadable `--items`) |
| 3 | preflight failed: server, model or key (the message says which) |
| 8 | no working Python 3.8+, or claude missing |
| 9 | nothing was audited: build failed, no items, or every item withheld |
| 10 | another sweep holds this run's lock |
| 130 | interrupted |

Because it exits non-zero on a missing block, a prose citation or an empty run, a sweep
can gate a commit or a CI step.

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

## Builders that read history

- **`history`** (`--arg files=PATH[,PATH...]`, relative to `--repo`): one item per file.
  The evidence is the extracted decision moments from the current project's Claude Code
  transcripts: user lines naming the file, edits to it, test commands and their first
  result line, and assistant lines that name it with a reason. The model never sees raw
  transcripts, and each line carries a session id and timestamp. A file with no transcript
  folder or no mention is withheld with a reason.
- **`deviations`** (`--arg spec=PATH`, plus the usual diff options such as `--base`): one
  item per changed file, held against the spec. The context adds the spec, every recorded
  `decisions.jsonl` from `--until-done` runs of this repo, and the transcript evidence for
  the file. Verdicts: `DEVIATION_EXPLAINED`, `DRIFT_UNEXPLAINED`, `MATCHES_SPEC`,
  `CANNOT_DETERMINE`. Implies `--test`, so batches may run the project's tests via
  `qwen-test`. `DEVIATION_EXPLAINED` must cite a `TEST ... PASSED|FAILED|ERROR|TIMEOUT`
  line or the collator flags it; that proves a line is present, not that the test was
  re-run.

Transcript scope is the current project only (see `limits.md`).

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
