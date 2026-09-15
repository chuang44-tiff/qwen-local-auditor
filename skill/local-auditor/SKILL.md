---
name: local-auditor
description: Run a free, LAN-local second opinion by dispatching a headless Claude Code session to a locally-served model via the `qwen-agent` CLI, or a batch sweep over many items via `qwen-sweep`. Use this whenever work would benefit from a genuinely independent pair of eyes, or when a job is bulk reading that would be expensive in-session - reviewing a diff or a file you just wrote, sanity-checking a script before it runs, a cross-model check on your own conclusions, adjudicating claims a document makes about code, or digesting a large log. Trigger on "audit this", "second opinion", "have the local model look at it", "cross-model check", "review this with qwen", "run a local review", "check my reasoning", "sweep these files", "review this diff locally", "summarise this log", and on any request to review code where the user mentions the local/LAN/offline model, or where you are about to assert something is correct and have no independent check.
---

# Local auditor

## What this is

`qwen-agent` spawns a **separate headless Claude Code process** pointed at a locally
served model instead of the Anthropic API. Claude Code routes providers session-wide, so
a subagent cannot be pointed elsewhere — a separate process is the mechanism.

It costs no frontier tokens. That changes what is worth doing: you can afford to check
things you would otherwise assert, and to read things you would otherwise skim.

**`qwen-sweep`** runs the same lane over many items at once, with the reliability
machinery that a batch needs (locking, withholding, retry, collation).

## The design rule that makes it work

**Ask it for extraction, not judgment.** It is excellent at "what does this line say,
cite it" and unreliable at "is this good". Every measured failure of this lane came from
asking for judgment where evidence was what was needed. Build the context and ask what
is in it; do not hand it a question and hope.

Corollary: **hand it path headers.** Citations were 35/35 correct when the context
carried real paths, and ~40% were bare filenames when it explored freely.

(The numbers in this skill were measured on one self-hosted model and one codebase.
They show which failure modes exist; the rates on your setup will differ.)

## Preflight — every session, before trusting anything

```bash
qwen-agent --preflight-only
```

Exit 0 means the server is up, a model is actually served (the configured one, or the
only one), and a working python and `claude` were found by RUNNING them. Exit 3 means
it is not usable — the message names what failed and lists the models that ARE served.
The endpoint lives in `~/.config/qwen-agent/config` (`QWEN_BASE_URL`, optionally
`QWEN_MODEL`).

## Single-shot: the common case

```bash
qwen-agent -r auditor -C <dir> "Name the three largest Python files and their line counts."
qwen-agent -r auditor -C <dir> -f brief.md -o out.md
```

- `-C <dir>` is the working directory. **Scope it tightly.** A bounded directory is the
  single biggest lever on output quality.
- Roles: `auditor` (grounded, cites `path:line`), `mechanic` (may edit), `plain`. Pass
  `-r auditor` for review work: with no `-r` there is no role prompt at all.
- A bare run is read-only with MCP dropped. Writing requires `--write` or an explicit
  `--toolset` naming Edit/Write/Bash.
- `-o` is relative to your current directory, never to `-C`.

## Batch: `qwen-sweep`

```bash
qwen-sweep --builder diff   --repo . --base main
qwen-sweep --builder files  --repo . --glob 'src/**/*.py'
qwen-sweep --builder claims --repo . --docs docs/issues --items list.json  # list.json: ["a.md", ...]
qwen-sweep --builder logs   --input big.log
```

| builder | one item is | asks |
|---|---|---|
| `diff` | a changed file in a range | what the change does, what contradicts its intent |
| `files` | a path from a glob | what the file does, demonstrable defects |
| `claims` | a document asserting things about code | a VERDICT per claim, with evidence |
| `logs` | a chunk of a large text | what is in it, what recurs |

Useful flags: `--dry-run` (build and report, dispatch nothing — always do this first on a
new sweep), `--resume` (skip batches that already passed), `-m NAME` (model for every
batch), `--arg KEY=VALUE` (builder settings, e.g. `test_dirs=test,spec` for `claims`).
Batch and item budgets scale to the model's context window automatically; override with
`--budget` / `--item-budget`.

`claims` understands most source languages, but its guard against citing a comment as
evidence is exact only for Python and approximate for C-family files.

**Items with no evidence are withheld, never dispatched**, and land in
`needs-human.txt` with a reason (the final summary lists them and prints the path). That is not a failure; it is the tool refusing to ask a
question it cannot answer. Measured: 42% of one real corpus.

`qwen-sweep` exits non-zero if any block was missing, unparseable, or flagged, and also
when nothing at all was audited — so it can gate a commit or a CI step.

## Reading the result

Read `reference/limits.md` before you act on a verdict. The short version:

- `APPEARS_FIXED` **requires a code line**. A docstring describing a fix is not evidence
  the fix landed; the collator flags favourable verdicts whose citations are prose.
- `STILL_PRESENT` is **not a disposition** — it says only that the named mechanism is
  unmoved at its named site.
- `CANNOT_DETERMINE` is a legitimate answer and usually an honest one.
- Success is judged by **content, not exit code**: an answer missing its expected blocks
  is a failure even at rc=0 (a tiny block-less body is the autocompact death signature).
  `qwen-sweep` checks every batch this way.

## Exit codes

`qwen-agent`: 0 ok · 2 usage · 3 preflight (server, model or key; the message says which)
· 4 API error from the server · 5 timeout · 6 empty result · 7 a tool call was denied ·
8 harness (claude or python missing, or unparseable output).

`qwen-sweep`: 0 every batch complete · 1 collation problems (missing blocks, prose
evidence) · 2 usage · 3 / 8 preflight, as above · 9 nothing audited (build failed, no
items, all withheld) · 10 another sweep holds the lock.

## Deeper

- `reference/sweep.md` — batch mechanics, the builder contract, writing a new builder.
- `reference/limits.md` — what this lane gets wrong, measured, and how to compensate.
