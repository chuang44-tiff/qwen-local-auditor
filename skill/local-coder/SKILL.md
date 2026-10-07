---
name: local-coder
description: Have the local model implement a change against a written task and checklist, looping until every check passes, via `qwen-agent --until-done`. Use when the work is a well-specified code change whose completion can be checked by tests or commands - implement a feature to a spec, fix failing tests, apply a migration across files. Claude writes the task file and checklist; the harness, not the model, decides when it is done.
---

# Local coder

## 1. Write the task file (you, in this session)

```markdown
# Goal
<one paragraph>

# Spec
<the spec, or a path to it>

# Checklist
- [ ] <item> -- check: test <test id or path>
- [ ] <item> -- check: cmd <command that exits 0 when the item is done>
- [ ] <item> -- check: none          (allowed; reported as UNVERIFIED)
```

Write the ` -- check:` suffix only once per line, at the end. A line with no valid suffix
whose prose contains "check:" is refused. When item text quotes the syntax, the last valid
` -- check:` on the line is the check and anything before it is item text; anything that
still reads like a marker after it, or text after `check: none`, is refused.

Make every item checkable. The local model cannot tick items or edit this file.
For batch edits across many files, give one item per file.

## 2. Run it

```bash
qwen-agent --until-done task.md -C <repo>            # commit first, or add --allow-dirty
```

It always runs as `-r coder --test`, and needs `QWEN_TEST_CMD` in
`~/.config/qwen-agent/config` for `test` checks. A `test` selector is a test id or
path, or `-k EXPR`: no options, no absolute paths, no `..`.
Supervisor options: `--max-rounds N` (default 8), `--budget-tokens N`,
`--budget-seconds N`, `--allow-dirty`, `--no-deviation-audit`.
It refuses a prompt and the per-run flags (`-f`/`--prompt-file`, `--stdin`,
`--resume`, `-w`, `-o`, `--dry-run`, `--json`) with exit 2.
DEPTH IS THE DEFAULT: every coder round already gets the deep coder text, a review
round after the checks first pass, and subagent delegation — `--probe` is NOT implied
for a coding loop and stays typed: it runs the whole loop in a sandbox and returns a
`patch:` instead of editing your tree. `--shallow` restores the plain loop; `--deep`
types all four (`reference/coding.md`, "Depth: the rounds are deep by default").

## 3. Read the result

The last stdout line is `report: <path>`. `report.md` has the final checklist with
evidence, the stop reason, the depth mode and switches used, the decision log, the new
files and the denied tool calls.

| exit | meaning | what to do |
|---|---|---|
| 0 | every check passed, no unlogged deviation | review the diff (local-auditor), then commit |
| 2 | usage error or refused flag | fix the command |
| 4, 8 | server API error, or harness problem | run `qwen-agent --preflight-only` |
| 11 | round limit or budget hit | read the report; raise the limit or split the task |
| 12 | no progress: same failing checks two rounds running, and the agent changed nothing | read the evidence, fix the task or do it yourself |
| 13 | dirty tree at start | commit, or pass `--allow-dirty` |
| 14 | another run holds this repo | wait, or remove a stale lock named in the message |
| 130 | interrupted (Ctrl-C) | re-run when ready |

## 4. Deviations

The coder records every deliberate departure from the spec as a `## DEVIATION`
block (SPEC, DID, WHY, EVIDENCE). A departure without one keeps the task open.
Decide each recorded deviation yourself: accept it, or change the spec.
To audit a finished run against the spec, use `local-sweep` with `--builder deviations`.
