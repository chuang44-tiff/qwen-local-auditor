# Security Policy

## What runs where

Everything in this repository runs **on your own machine**, against a model server you run
yourself. The one exception is the optional Claude calls below, which are off on a fresh
install. The commands that run a model start a separate `claude` process on this host and point
it at your server: the child does not inherit your interactive session's API key,
provider routing or model overrides from the environment. One known gap: an
`env.ANTHROPIC_BASE_URL` set in a Claude Code settings file can still redirect the child;
a fix that pins the endpoint is planned. These tools add no telemetry, no update check and
no account of their own (the `claude` CLI's own background traffic follows Claude Code's
settings); the config file, run folders, logs and Claude-call records are written on this machine
and stay there.

What the fences are, and are not. They decide which tools a session gets; they are not
confinement. At the default depth a run gets a shell, Edit/Write and subagents inside a
throwaway copy of your tree, and that copy is not a jail: the shell runs **as you, with
your network**. `--shallow` keeps the strict read-only fence. The modes that act on real
things do so on purpose, as you: `--test` runs your repository's own code, `--browser` and
`--desktop` drive a real browser or a native application, `--record`/`--replay` run
model-written JavaScript under node unsandboxed, and a swarm's `sandbox` role has a shell
that is yours. The fence at a glance:
[reference/limits.md](skill/local-auditor/reference/limits.md).

## What can leave the machine

A fresh install sends nothing to Claude: no call is on until **your config file**
(`~/.config/qwen-agent/config`) turns one on. An exported `QWEN_PLAN`, `QWEN_ADVISOR`,
`QWEN_RESCUE` or `QWEN_CONFIRM` is ignored, and a typed `--plan`, `--advisor` or `--rescue`
affects one run only. Each call goes through your own `claude` login. Sweeps, swarms and deep
research follow the same config; neither a swarm's `workflow.json` nor its `workflow.py` (through the engine API) can turn a call on: `wf.claude_check` is refused unless the config's `QWEN_CONFIRM` names a model or `--set confirm=claude` is typed.
`--show-claude` prints what a command would use without running it.

1. **Plan** (`QWEN_PLAN`, `--plan`): once, before the session's first file edit, a digest
   of its messages and tool trail (no thinking) and the task.
2. **Advisor** (`QWEN_ADVISOR`, `--advisor`): the agent's `ask` tool, at most
   `QWEN_ADVISOR_MAX_CALLS` (4) calls per run: its question, the context it writes and up to
   5 non-secret files it attaches.
3. **Rescue** (`QWEN_RESCUE`, `--rescue`): after a failing `--until-done` round, the failing
   output, the round's diff and the task.
4. **Confirm** (`QWEN_CONFIRM`, `qwen-swarm ui-test`): the default is `local`, which keeps the
   re-check on your model. With `claude` (the config or a typed `--set confirm=claude`; a
   workflow preset cannot enable it) each FAIL/BLOCKED scenario, up to `confirm_max`, sends
   the scenario, the tester's report and screenshots, the fixture files, and a live browser
   session on the app's URL. The run says so on stderr before the first call.

For plan, advisor and rescue, Claude also gets **read tools** (Read, Grep, Glob) on your
repository whenever the call is on, so any file it chooses to read leaves the machine, not
only what is listed above. The secret-file filter covers attachments, not these reads.
`QWEN_CLAUDE_TOOLS=0` turns the read tools off. Every call is logged on this machine: `--until-done` runs write
`claude-calls.jsonl` in the run folder, single runs and swarm units write
`advisor-<stamp>-<pid>.md` / `plan-<stamp>-<pid>.md` in `QWEN_OUTDIR` or
`~/.cache/qwen-agent/advisor/`, and ui-test's confirm pass writes `RUN/claude/calls.jsonl`.

Separately, **the research commands' web access** is on purpose: `qwen-deep-research`, and a
`qwen-swarm` workflow whose roles have a `search` or `web` fence, send queries to the search
backend you configured and fetch pages. A `browser` fence opens whatever URL an agent is told
to open. See [limits.md](skill/local-auditor/reference/limits.md#claude-calls).

Two consequences worth stating plainly: the URL you point a run at is reached *from this
machine*, and a hostile page under test can try to prompt-inject an agent into sending what
it can read. Keep fixture files non-secret, and point tests and research at hosts you trust.

## Reporting a vulnerability

Report privately through **GitHub's private vulnerability reporting**, on the **Security**
tab of this repository (`Report a vulnerability`). Do not open a public issue, and do not
include secrets, credentials or anyone else's data in the report.

Useful in a report: the command line you ran and its fence or depth flags, the commit you
ran (`git log -1 --format=%h`), the exit code, and the relevant part of the run's log with
paths and content redacted. We reply privately; the report is visible only to you and the
repository's maintainers.
