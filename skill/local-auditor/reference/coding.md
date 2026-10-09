# Tests and coding loops: `--test`, `qwen-test`, `--until-done`

Three pieces, each built on the previous one: `qwen-test` runs your test command in a
throwaway git worktree; `--test` is the only way a headless run gets a shell, and the
shell it gets is exactly that one command; `--until-done` loops a coder run with `--test`
until a checklist's checks pass. The skill your session reads for the loop is
`skill/local-coder/SKILL.md`. What the fence does and does not protect is in
[`limits.md`](limits.md); read it before the first `--test` run.

## `qwen-test`

```bash
qwen-test tests/test_x.py -k parse      # you can run it yourself, too
```

Runs `QWEN_TEST_CMD` (set it in the config; it is the one piece of setup `--test` needs)
in a throwaway git worktree of the repository, so a test's side effects on the checkout's
files never touch the checkout. The arguments are **selectors only**, an allowlist: a test
id or path, or `-k EXPR`. Options, `@file` arguments, absolute paths, `..` and shell
characters are refused with exit 2, and the maintenance modes of the underlying script
(`lib/testrun.py`) are not reachable through `qwen-test`.

| exit | meaning |
|---|---|
| 0 | passed |
| 1 | failed, or an error (including a test command that cannot run) |
| 2 | usage: no `QWEN_TEST_CMD`, a refused selector, no working Python 3.8+, a non-positive `QWEN_TEST_TIMEOUT` |
| 5 | timed out after `QWEN_TEST_TIMEOUT` seconds (default 600) |
| 130 | interrupted |

Output is capped at `QWEN_TEST_MAX_BYTES` (default 20000). On Windows write the command
with forward slashes (`C:/Python312/python.exe -m pytest`): it is split like a POSIX shell
line, which eats backslashes.

How the worktree is kept in step with your checkout (synced before every run, tracked
deletions propagated, untracked files the auditor wrote kept, gitignored dependencies not
synced, no `git worktree prune` ever run) is spelled out in
[`limits.md`](limits.md#running-tests-runs-the-repositorys-code).

## `--test`: let a run execute the tests

```bash
qwen-agent --test -r auditor -C . "Does parse() reject an empty list? Show a failing test if not."
```

`--test` grants the run one Bash command, `qwen-test`; the model only chooses which
tests. It always passes `claude --restricted` (user, project and local settings files
are ignored, and the file tools are confined to the working directories) and
`--permission-mode dontAsk` (write/coder runs still edit, because `--allowed-tools`
grants them the edit tools). It cannot be combined with `-w`, `--all-tools`,
`--toolset`, `--read-only`, `-t`/`--tools` or any `--permission-mode`: an explicit `-t`
grant list would replace the qwen-test-only grants. A claude without `--restricted` is
refused (exit 2), and a read-only `--test` run prints a warning, even under `-q`, that it
gains Bash and worktree writes. `QWEN_SETTING_SOURCES` is never passed under `--test`.

What each kind of run sees under `--restricted`:

| run | file tools see |
|---|---|
| read-only `--test` (the auditor) | the `-C` directory, any `-D` directories, **and** the test worktree, where it may write reproduction tests. Files it writes there come back under `## REPRO FILES` in the result, for you or `local-coder` to adopt |
| write/coder `--test` | the `-C` directory and the `-D` directories only; no worktree. The coder edits your tree and `qwen-test` syncs and runs it |
| a `qwen-sweep --test` batch | `-C` is the batch directory, so the repo is visible only through the test worktree, which holds HEAD's content until the first `qwen-test` run syncs it |

`--test-repo DIR` names the repo whose tests run when it is not the `-C` directory.

Under `--test`, Claude Code itself also auto-approves shell commands it classifies as
read-only when they stay inside the working directory (seen with Claude Code 2.1.288:
`cat README.md` and `grep -r` ran; `cat /etc/hostname`, `ls /`, writes, `python3 -c` and
`curl` were denied). That is the same reach as the `Read`/`Grep` tools: no writes, no
execution other than `qwen-test`, no network unless `--web`, nothing outside the working
directory.

**Running tests runs the repository's code.** The fence limits the shell, not what code
runs: the tests `qwen-test` runs, including any the model wrote or edited, execute as you,
with your network and your credentials. Use `--test` only on code, and with a model, you
would let run on this machine. Keep `--web` off for test-driven work: in the benchmark,
given web access the model went looking for the exercises' upstream tests and reference
solutions.

## `--until-done`: a coding task, looped until its checks pass

```bash
qwen-agent --until-done task.md -C . --max-rounds 6     # commit first, or add --allow-dirty
```

### The task file

```markdown
# Goal
<one paragraph>

# Spec
<the spec, or a path to it>

# Checklist
- [ ] <item> -- check: test <test id or path>
- [ ] <item> -- check: cmd <command that exits 0 when the item is done>
- [ ] <item> -- check: none          (allowed; reported as UNVERIFIED, never as done)
```

Write the ` -- check:` suffix once per line, at the end. A line with no valid suffix whose
prose contains `check:` is refused. When item text quotes the syntax, the last valid
` -- check:` on the line is the check and anything before it is item text; anything that
still reads like a marker after it, or text after `check: none`, is refused. A `test`
selector obeys the `qwen-test` allowlist above: a test id or path, or `-k EXPR`. Make
every item checkable: the local model cannot tick items or edit this file. For batch
edits across many files, give one item per file.

### What the loop does

It always runs the `coder` role with `--test`, resumes one session each round with what
still fails, and decides "done" itself by running the checks; the model never does. The
coder records every deliberate departure from the spec as a `## DEVIATION` block (SPEC,
DID, WHY, EVIDENCE) in a decision log; a departure without one keeps the task open. When
every check passes, a read-only deviation audit compares the spec with the diff
(`--no-deviation-audit` skips it). The audit is an agent call like a round: if it fails
with a usage error or a server error, the run exits 2 or 4.

`--until-done` takes no prompt and refuses every option that would collide with the loop
it owns or widen a round's `--test` fence: `-f`, `--stdin`, `--resume`, `-w`, `-o`,
`--dry-run`, `--json`, any role other than `coder` (`-r`/`--role`), `--role-file` (it
would replace the coder role and turn the rounds read-only), `--toolset`, `--read-only`,
`--all-tools`, `--unrestricted`, any `--permission-mode`, `-t`/`--tools`, and
`-D`/`--add-dir` (a coder's file access stops at its own tree; `-D /` would open the whole
disk).

| option | meaning |
|---|---|
| `--max-rounds N` | default 8 |
| `--budget-tokens N`, `--budget-seconds N` | stop early when spent |
| `--allow-dirty` | start even with uncommitted changes |
| `--no-deviation-audit` | skip the spec-vs-diff audit after the checks pass |
| `--advisor MODEL` | every coder round may ask a Claude model for advice |

`--advisor MODEL` is allowed with the loop (typed, as ever): the supervisor makes one
advisor state directory for the run and hands it to every coder round, so all rounds
draw on ONE `QWEN_ADVISOR_MAX_CALLS` budget between them instead of one each, and the
deviation audit is called without the advisor — it never spends it and never sends the
diff out on a call nobody asked it to make. Questions and the files a round attaches
leave this machine; `report.md` gets a `## Advisor` section with the model, the budget,
how many calls the run made, their total cost and how many came back unavailable.

### Depth: the rounds are deep by default

Depth ([`qwen-agent.md`](qwen-agent.md#depth-the-default)) is the default for the loop
too, and it decomposes ONCE, in the shell before the loop starts: the coder rounds get
V, R and the delegation push (`--subagents-push`, which replaces the softer
`--subagents-nudge` text) — what `--deep` forwards, minus `--probe`, which stays the
loop's own sandbox and is only ever typed. Every round's agent call then runs with `--shallow` on its
command line and `QWEN_DEPTH=shallow` in its environment (the flag, because a
`QWEN_DEPTH=deep` in the config file would outrank the env var), so a round never
implies a depth switch of its own; `--shallow` (or `QWEN_DEPTH=shallow`)
restores the plain loop, and a typed token outranks the implied one. A single direct
depth run defaults its `--timeout` to 3600 s, and under depth the shell hands that
3600 default to every round through `QWEN_TIMEOUT` in the environment the rounds
inherit, unless `--timeout` or `QWEN_TIMEOUT` was given; a `--shallow` loop keeps the
1800 default.

- `--role-variant deep` and `--subagents-push` go to every round (coder-deep adds the
  edge-case pass and its finish check; the push text replaces the nudge text); the
  deviation audit stays the plain auditor — the supervisor strips the depth tokens
  from its passthrough.
- `--review-round`: when the checks first pass, one more round resumes the session with a
  "try to break your change" prompt, then every check (and the deviation audit) runs
  again. It counts toward `--max-rounds`; with no round or budget left it is skipped and
  the report says so.
- `--probe`, TYPED only (an implied `--probe` is for single read-only runs — a coding
  loop writes): the whole loop runs in one sandbox under the run folder, with your
  uncommitted and untracked files in it (no `--allow-dirty` needed); each round gets a
  shell there (`--probe-here`), the checks run there, and nothing in your work tree,
  index or refs is written (the sandbox shares your repository's object files, so git
  may refresh their mtimes; no content changes). On a dirty tree the report's
  `start commit:` and the
  commits in the decision log are sandbox commits: they record your uncommitted state
  and do not exist in your repo. The work comes back as `RUN/probe.patch`, printed as
  `patch: PATH` just before the `report:` line, also when the run is interrupted or
  ends in an error (exit 8); if the patch cannot be written the sandbox is kept instead
  and its path is printed as `sandbox kept: PATH`. Every printed apply command is
  shell-quoted and pastes into a shell as it stands. `--keep-sandbox` keeps
  `RUN/sandboxes/tree`.
- `--deep` is all four TYPED at once (it takes no value; combining it with another
  `--role-variant` is a usage error; with `--shallow`: exit 2). `report.md` records the
  mode on a `depth:` line and lists the switches used on a `switches:` line.

### The report

The last stdout line is `report: <path>`. `report.md` holds the final checklist with
evidence, the stop reason, the depth mode and switches used, the decision log, the new
(untracked) files, any denied tool
calls, and a `## Tool use` table that shows whether subagents were used (when the session
transcript is readable). Read the new files and the denials before trusting a "done".
Reports and run state live in `QWEN_AGENT_STATE` (default `$XDG_CACHE_HOME/qwen-agent/runs`),
which must be outside the repo or the run exits 2.

### Exit codes

| exit | meaning | what to do |
|---|---|---|
| 0 | every check passed, no unlogged deviation | review the diff (`local-auditor`), then commit |
| 2 | usage error or refused flag | fix the command |
| 4, 8 | server API error, or harness problem | run `qwen-agent --preflight-only` |
| 11 | stopped at the round limit or a budget, or the checks pass but the deviation audit was unusable twice (partial; report written) | read the report; raise the limit, split the task, or review the diff manually |
| 12 | no progress: the same checks failed two rounds in a row and the agent changed nothing | read the evidence, fix the task or do it yourself |
| 13 | working tree dirty at start | commit, or pass `--allow-dirty` |
| 14 | another `--until-done` run holds this repo's lock | wait, or remove a stale lock named in the message |
| 130 | interrupted (Ctrl-C); report written | re-run when ready |

The checklist decides "done", and only as well as its checks: a test that does not
exercise the item makes a weak "done". Write the checks you would trust. To audit a
finished run against its spec afterwards, run
`qwen-sweep --builder deviations --repo . --base <start> --arg spec=SPEC.md`
([`sweep.md`](sweep.md)); `DEVIATION_EXPLAINED` there is a record, not an approval.
