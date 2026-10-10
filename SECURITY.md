# Security Policy

## What runs where

Everything in this repository runs **on your own machine**, against a model server you run
yourself. The commands that run a model start a separate `claude` process on this host and point
it at your server: the child does not inherit your interactive session's API key,
provider routing or model overrides from the environment. One known gap: an
`env.ANTHROPIC_BASE_URL` set in a Claude Code settings file can still redirect the child;
a fix that pins the endpoint is planned. These tools add no telemetry, no update check and
no account of their own (the `claude` CLI's own background traffic follows Claude Code's
settings); the config file, run folders, logs and advisor logs are written on this machine
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

## The three things that can leave the machine

1. **The cloud advisor — opt-in only.** `qwen-agent --advisor MODEL` lets a session ask a
   Claude model for advice through your own `claude` login: one question, its evidence and
   up to 5 non-secret files, at most 4 calls per run. No environment variable or config
   line turns it on, so sweeps and swarms never send code out through it.
2. **`qwen-swarm ui-test`'s confirm pass — ON by default.** With the default
   `confirm=claude`, each FAIL/BLOCKED scenario (up to `confirm_max` of them) goes to
   Claude through your own `claude` login: the scenario, the tester's report and screenshots,
   the fixture files, and a live browser session on the app's URL. `--set confirm=local`
   keeps the re-check on your local model, `--set confirm=none` skips it. The run says so
   on stderr before the first call.
3. **The research commands' web access.** `qwen-deep-research`, and a `qwen-swarm`
   workflow whose roles have a `search` or `web` fence, go online on purpose: queries go to
   the search backend you configured and pages are fetched. A `browser` fence opens
   whatever URL an agent is told to open.

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
