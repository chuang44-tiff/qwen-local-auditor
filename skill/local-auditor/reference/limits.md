# What this lane gets wrong

Measured over ~91 verdicts on one real codebase, with one self-hosted model and one
Claude Code version. The failure MODES below are general; the rates are that setup's,
so re-measure on yours. Read this before acting on any output.

## The one recorded fail-open, and what actually caused it

One `APPEARS_FIXED` on a still-open defect. The model cited a module docstring
*describing* a fix and stopped at the favourable half — two lines above that docstring's
own "closes the MEASURED defect, NOT the class".

For a long time this was written up as a multi-clause item being crushed into one label.
**That was wrong.** The root cause, found later, was deterministic and lived in the
context builder, not the model:

`_is_prose` recognised only a docstring's OPENING delimiter line, so every *subsequent*
line of a multi-line docstring was classified as code — and because the extractor ranks
code hits ABOVE prose hits, the ranking meant to demote docstrings actively **promoted
their bodies into the evidence window**. Measured on one 611-line module: the symbol's
hits were lines [1, 10, 28, 238] and the "code" hits were [10, 28, 238] — all three
inside module docstrings.

Fixed by `prose_mask` in `lib/context.py`, which locates docstrings with `ast`, so a
string used as a *value* still counts as code and only bare string expression statements
are prose.

**Two consequences that remain your problem:**

1. **Require a code line.** The collator now flags any favourable verdict citing a prose
   line ("invariant 8": a favourable verdict must cite code, not a comment or docstring), but that check is fail-safe, not complete — it cannot resolve the
   ~40% of citations that are bare filenames.
2. **Non-Python files are only approximately covered.** C-family files (C/C++, Java,
   JS/TS, Go, Rust, C#, ...) get a `//` and `/* */` comment state machine that ignores
   string literals. Other block-comment languages fall back to `#`-style handling, where
   a comment block describing a fix WILL be ranked as code and promoted. Trust
   non-Python findings less until measured on your code.

## `STILL_PRESENT` is not a disposition

Context is built by grepping around symbols **the item itself names**. So the verdict
establishes only: *the named mechanism is unmoved at its named site.* It cannot see a
remediation that landed at a caller, in a new module, or under a different name. Never
close or escalate on `STILL_PRESENT` alone.

## Empty context is worthless — and now refused

Items yielding no evidence returned `CANNOT_DETERMINE` 100% of the time. The builder now
withholds them with a reason and the engine routes them to `needs-human.txt`.

Measured on one real 72-item corpus: **30 withheld (42%)**. The `windows == 0` test is
stricter than the older `files == 0` — it also catches items that name files their
symbols never appear in.

## Citations

- Reliable when the context carries **path headers**: 35/35 correct.
- Unreliable in free exploration: ~40% are bare filenames with no directory, so any
  consumer needs a resolver and must tolerate failure.
- Path *segments* can be assimilated to neighbours (a `_` becoming `-` to match an
  adjacent hyphenated path). Verify the path, not just the line.

## Success is content, not exit code

A tiny body with none of the expected blocks (~259 bytes when it was observed) is the
autocompact self-destruct, and it has been seen at **both rc=0 and rc=8**. The exit code
is unreliable in both directions -- and so is size alone, because a correct short answer
is small too. `qwen-sweep` checks that every expected block is present, and retries an
incomplete batch once.

## Verdict per clause

Nearly half of one real corpus (18 of 43 dispatched items) enumerates multiple numbered
claims. Collapsing those into one label loses the disagreement between them — which is
the whole signal. The `claims` builder enumerates clauses and each gets its own key and
verdict; do not undo that by asking a single blended question.

## The rule that explains all of the above

**Ask for extraction, not judgment.** Every failure mode here is the harness asking the
model to decide something when it should have asked it to report something.

## The fence at a glance

What each command can reach, and which flag opens what. The sections that follow give
the measured detail.

- **A bare `qwen-agent` run never writes your files, but under default depth it has a
  shell.** Inside a git repo, depth (the default) runs the session in a throwaway
  sandbox copy of your tree with Bash, Edit, Write and `Task` subagents, then resumes it
  once for a review call. The copy is not a jail: the shell runs as you, with your
  network and every path you can reach. Outside a git repo, or where the sandbox cannot
  be built, the run stays `Read,Glob,Grep` (plus `Task` and the review call).
- **`--shallow` (or `QWEN_DEPTH=shallow`) or `--read-only` keeps the strict fence.** The
  toolset is `Read,Glob,Grep` and configured MCP servers are dropped
  (`--strict-mcp-config`): a schema-level restriction, the model has no Bash and no
  Write tool at all. Editing then needs `--write` (or the `mechanic` or `coder` role);
  Bash needs `--test` (which grants only `qwen-test`), `--all-tools`, `--probe` or an
  explicit `--toolset`, and each prints a warning.
- **Subagents come with depth** (`Task`, same model, same tool limits, one more
  concurrent request against your server); under `--shallow` they are opt-in
  (`--subagents`, `QWEN_SUBAGENTS=1`).
- **Web tools are opt-in** (`--web`, `QWEN_WEB=1`, which adds only `WebFetch`, never
  `WebSearch`; or `--browser`). Keep them off for test-driven work. The sandbox shell of
  a depth run is not a web tool, but it can reach the network like any shell you run.
- **`--record` and `--replay` run model-written JavaScript** with `node`, as you,
  unsandboxed and with your network. Replay only folders you recorded or have read.
- **`--test` limits the shell, not what code runs**: the tests `qwen-test` runs, including
  any the model wrote or edited, execute as you. Every `--test` run passes
  `claude --restricted`, so your own Claude settings cannot widen the fence.
- **The child never inherits the parent session's control channel**, its provider routing
  (Bedrock, Vertex, Foundry), `ANTHROPIC_API_KEY`, custom headers or model overrides, so a
  prompt cannot silently go to a cloud provider instead of your server. `--dry-run` shows
  the exact command and environment with secrets redacted.
- **Sweep runs are written to your cache directory**, never into the repository under
  audit; `--until-done` state must live outside the repo too.
- **`qwen-cc` reads, types into and kills only the tmux sessions it created** (tagged
  `@qwen_cc=1`) and refuses every other session. What it cannot limit is what a typed line
  does: `--say` is how an interactive session's permission prompts get answered, so that
  stays a human decision (the skills tell your Claude Code session to answer one only when
  the user explicitly asks). See `interactive.md`.
- **`qwen-deep-research` is the one command that needs the internet.** Its agents run in
  empty folders with no file tools and no Bash; only the searcher, reader and verifier
  reach the network. See `deep-research.md`.

## Running tests runs the repository's code

**With `--test`, the model can run arbitrary code as you.** The fence limits the SHELL
(the only command is `qwen-test`), not what code runs: the auditor writes tests in the
worktree and `qwen-test` runs them, and the coder edits both code and tests in your tree
and then runs them. Whatever those files contain executes with your user's permissions,
your network and your credentials. Use `--test` only on code, and with a model, you would
let run on this machine.

`qwen-test` runs only the configured command (`QWEN_TEST_CMD`), in a throwaway git
worktree, so a test's side effects on the checkout's files never touch the checkout. It
does not protect the machine: a hostile repo, or a hostile test the model wrote, can do
harm through the test run.

What the model controls through `qwen-test` is only WHICH tests run. Selectors are an
allowlist: a test id or path, or `-k EXPR`. Options, `@file` arguments, absolute paths and
`..` are refused (exit 2), and the maintenance modes of the underlying script are not
reachable from `qwen-test`.

The fence itself does not depend on your Claude Code settings: every `--test` run passes
`claude --restricted` (user, project and local settings files are ignored, and the file
tools are confined to the working directories) and the fixed `--permission-mode dontAsk`
— write runs keep editing because `--allowed-tools` grants the edit tools up front, so
nothing needs accepting. A claude without `--restricted` is refused (exit 2). `--test`
cannot be combined with `-w`, `--all-tools`, `--toolset`, `--read-only`, `-t/--tools`
or any `--permission-mode` — an explicit `-t` grant list would replace the
qwen-test-only grants and `Bash(*)` under `dontAsk` is an open shell — and a
read-only `--test` run prints a warning, even under `-q`,
that it gains Bash and worktree writes.
`--setting-sources` is likewise never passed under `--test`: it would only re-open the
settings files `--restricted` exists to ignore.

**Under `--test` the file tools see only the working directories** — that is what
`--restricted` confinement means, and which directories depends on the run kind: a
READ-ONLY run sees the `-C` directory, any `-D`/`--add-dir` directories, **and** the
test worktree (where it may write reproduction tests); a write/coder run sees the `-C`
directory and the `-D` directories only — no worktree. A coder's worktree is the
harness's scratch space and is not even granted or shared with the coder that runs it,
so which tree the coder edits never blurs. A `qwen-sweep` batch runs with `-C` at its
batch directory, so a sweep batch sees the repo only through the test worktree, and the
worktree holds HEAD's content until its first `qwen-test` run syncs it: anything the model
reads before that first run describes HEAD, not your working tree. The same confinement
makes a spec path outside the working directories unreadable by the coder — an
`--arg spec=...` document that lives outside them is invisible to the very run meant to
implement it, not merely inconvenient.

On Windows, write `QWEN_TEST_CMD` with forward slashes (`C:/Python312/python.exe -m
pytest`): it is split like a POSIX shell line, which eats backslashes.

How the worktree is kept in step with your checkout:

- It is synced from the working tree before every run. Files that are new or changed in
  the source are copied in, and files tracked at the worktree's HEAD that are missing from
  the source are deleted, so a tracked file you removed is gone from the next run. An
  UNTRACKED file you delete from your checkout stays in the worktree (and keeps running,
  if it is a test) until the worktree is removed.
- Untracked files that exist only in the worktree (an auditor's reproduction tests) are
  kept for the rest of the run, and are listed under `## REPRO FILES` in the result.
- Gitignored dependencies (`.venv`, `node_modules`, `.env`) are NOT synced. The test
  command must work without them, or find them by absolute path.
- No `git worktree prune` is ever run, because it would also touch your unrelated
  worktrees. Ctrl-C lets the run remove its worktree before it exits; a run that was
  killed outright (SIGKILL, a crash) can leave one behind. `git worktree list` shows it;
  clear it with `git worktree remove --force <path>`. (`git worktree prune` does nothing
  while the directory still exists.)

## What `--test` really allows in Bash

The only Bash grant is `Bash(qwen-test:*)`, under `--permission-mode dontAsk` and
`--restricted`. Claude Code also auto-approves shell commands it classifies as read-only
when they stay inside the working directory, independent of the grants: with Claude Code
2.1.288, `cat README.md` and `grep -r hello .` ran, while `cat /etc/hostname`, `ls /`,
`echo hi > file`, `python3 -c ...` and `curl` were denied, for the main agent and for a
subagent alike. The fence therefore guarantees no writes outside the edit tools, no
execution other than `qwen-test`, no network (unless `--web`), and no reads outside the
working directory (and the test worktree): the same reach as `Read`/`Grep`/`Glob`, not "no other command
ever runs". The model is still told that only `qwen-test` runs, which keeps it from
wasting turns on commands that are denied.

## Subagents come with depth

Under the default depth, `Task` is part of the run and the model is pushed to hand broad
reading and searching to a subagent. With `--shallow` they are opt-in: `--subagents`
(`QWEN_SUBAGENTS=1`) adds the `Task` tool and a short note telling the model to delegate
and keep its own context for edits and tests. A subagent runs on the same local model,
inherits the run's tool restrictions and grants (and `--restricted` under `--test`), and
adds no capability. It is one more concurrent request against the server; on a small GPU,
`--shallow` without `--subagents` keeps it to one. The
until-done report's `## Tool use` table shows whether it was used (when the session
transcript is readable; otherwise it says "(no transcript found)"); Claude Code 2.1.288
records the tool as `Agent` (`Task` is its older name, still accepted in `--tools`).
Without `--test`, the model may also pick user, plugin or repo-defined agents, each with
its own prompt; they still get only this run's tools.

## Web access is opt-in — and keep it off for test-driven work

`--web` (`QWEN_WEB=1`) is the only way a run reaches the web: web access is opt-in,
and by default no role offers `WebFetch` or `WebSearch` — read-only runs, `--write`
runs, every role, with or without `--test`. `--web` adds only `WebFetch`, to the
toolset and to the grants (including the fixed `--test` grant list);
`--all-tools`/`--unrestricted` stay as unrestricted as they were. `WebSearch` is
never added: it is a server-side Claude Code tool that vLLM rejects with a 400
(`body.tools.0.input_schema Field required`) — search needs an MCP server. Keep web
off for test-driven work: in the benchmark, given web access the model went looking
for the exercises' upstream tests and reference solutions. `--web`
together with `--test` prints a warning about exactly that. A second risk is worse than
gamed tests: a page the model fetches can instruct it to put repository text into the URL of
its next fetch, which sends that text out. Use `--web` only on repositories you would not
mind leaking, and never together with secrets in the working directory.

## The checklist decides "done", and only as well as its checks

`--until-done` stops when every check passes. An item with `check: none` is reported
UNVERIFIED, never as done. A weak check (a test that does not exercise the item) makes
a weak "done". Write the checks you would trust.

Stops other than success: exit 11 at the round limit (default 8), a budget, or when the
checks pass but the deviation audit was unusable twice (review the diff manually), exit 12
when the same checks fail and the working tree is unchanged two rounds in a row, exit 13
for a dirty tree at the start (`--allow-dirty` overrides), exit 14 when another run holds
the repo's lock. The report lists new (untracked) files and any denied tool calls: read
both before trusting a "done". The state directory (`QWEN_AGENT_STATE`, default under
`$XDG_CACHE_HOME/qwen-agent/runs`) must be outside the repo, or the run exits 2.

## DEVIATION_EXPLAINED is a record, not an approval

It says a reason was recorded and its test still behaves as the reason claims. Whether
the deviation was a good idea stays with you.

The collator requires the verdict's last TEST line to be shaped `TEST ... PASSED` (a
re-run that came back `FAILED`, `ERROR` or `TIMEOUT` is flagged). A pattern match cannot prove the test was actually re-run rather
than copied from the decision log or a transcript, so a fabricated TEST line passes it.
Treat the verdict as a lead to confirm, not as evidence that the test ran.

## Transcript evidence has a narrow, partly unverified scope

The `history` and `deviations` builders read only
`<claude config dir>/projects/<project path with every non-alphanumeric character
replaced by '-'>/*.jsonl`, for the current project. The encoding is verified on Linux.
Windows drive-letter paths and macOS paths reached through the `/tmp` to `/private/tmp`
symlink are unverified. If the folder is not found, `history` withholds the item and
names the expected path. `deviations` does NOT: it treats the missing folder as no
sessions, records "(no Claude transcript folder for this repo)" as the transcript
evidence, and still judges the item on the decision log and the diff alone. On an
unverified platform a `deviations` sweep may therefore lack transcript evidence, so run
`--builder history` on one file first to check that the folder is found.
`QWEN_TRANSCRIPT_DIR` can point at it.

Sessions are matched to a file by basename or path suffix, so similarly named files
(`utils.py` in two packages) can pull in a session about an unrelated file. Check the
`[session:... timestamp]` headers against what you know.
