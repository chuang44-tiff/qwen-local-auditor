# qwen-local-auditor

Claude Code, driving a model you serve yourself. `qwen-agent` starts a separate, headless
Claude Code process pointed at your own server (vLLM serving Qwen, or anything that speaks
the Anthropic Messages API), fenced so that by default it never writes your files. It runs
on your own GPU at no API cost, and only the research commands need the internet. The
"qwen" in the name is historical; any model works.

It is for people who already use Claude Code and have a machine serving a model: keep the
frontier tokens for the hard parts and hand the rest to the local model through six commands.

- `qwen-agent`: one question, one headless session, one answer.
- `qwen-sweep`: the same question over many files, a diff, a set of documents or a large log.
- `qwen-test`: your test command in a throwaway git worktree, the one shell command a `--test` run is granted.
- `qwen-cc`: an interactive session in tmux that Claude Code can read, type into and stop.
- `qwen-deep-research`: local sessions research a question on the web and write a cited report.
- `qwen-swarm`: a workflow (built-in `research`, `debug`, `ui-test` or `optimize`, or your own) run by many local agents in rounds, often overnight.

Six skills (`local-agent` routes; `local-auditor`, `local-sweep`, `local-coder`,
`local-deep-research` and `local-swarm` do the work) teach your Claude Code session when
and how to use them, so "have the local model review this" is enough.

## Requirements

- Claude Code (`claude` on `PATH`) and a server that serves the Anthropic Messages API at
  `/v1/messages`. vLLM does, and also lists models at `/v1/models`, which the tools use to
  pick the model and read its context window.
- bash (Linux, macOS, or Git Bash on Windows), Python 3.8+ (standard library only; CI exercises 3.10 and 3.12), curl >= 7.55, git.
- Pillow (`pip install pillow`) for `--desktop` only (screenshots).
- tmux for `qwen-cc`. A search backend (SearXNG or a Brave key) is needed only by research
  (`qwen-deep-research`, or a `qwen-swarm` workflow whose roles have a `search` or `web`
  fence). Nothing else asks for one, and no other fence adds a search or fetch tool; a swarm
  `browser` agent drives a real browser, which opens whatever URL it is told to, and a swarm
  `sandbox` agent does have a shell, and that shell is yours.

Servers without `/v1/models`, Windows and macOS notes:
[reference/configuration.md](skill/local-auditor/reference/configuration.md).

## Install

```bash
git clone https://github.com/chuang44-tiff/qwen-local-auditor.git
cd qwen-local-auditor
./install.sh
```

This symlinks the six skills into `~/.claude/skills/`, writes the six commands into
`~/.local/bin` (keep it on `PATH`) and copies `config.example` to
`~/.config/qwen-agent/config` once, never overwriting it. Point the config at your server
and check it:

```bash
$EDITOR ~/.config/qwen-agent/config   # QWEN_BASE_URL, and QWEN_MODEL if it serves several
qwen-agent --preflight-only           # exit 0 = server up, model served, python and claude run
```

`git pull` upgrades; `./install.sh --uninstall` removes the skills and commands and keeps
the config. Preflight names what is wrong ([troubleshooting](skill/local-auditor/reference/configuration.md#troubleshooting)).

## Try it

A second opinion, scoped tightly with `-C` and asked for extraction, not judgment:

```bash
qwen-agent -r auditor -C ./src "Which functions here write to disk? Cite path:line."
```

The same question over every file in a diff, and a coding task looped until the checklist
in `task.md` passes:

```bash
qwen-sweep --builder diff --repo . --base main --dry-run   # drop --dry-run to run it
qwen-agent --until-done task.md -C . --max-rounds 6
```

`--help` on any command is its complete flag reference; `--test`, `qwen-cc`, research and
swarms are on the reference pages below.

## Safety

Everything runs on your machine against your server; the child process does not inherit
your session's API key, provider routing or model overrides from the environment (one known
gap: an `env.ANTHROPIC_BASE_URL` in a Claude Code settings file can still redirect it; a fix
that pins the endpoint is planned). **Depth is the default:** a bare `qwen-agent` run never writes
your files, but inside a git repo it gets a shell, Edit/Write and subagents in a throwaway
sandbox copy of your tree, then reviews its own answer; the timeout is 3600 s. The copy
is not a jail: the shell runs as you, with your network. `--shallow` (or `--read-only`)
keeps the strict fence, `Read,Glob,Grep`; editing, shell, web and subagents are then each
a separate flag, and any run that can modify files or run a shell prints a warning.

Some modes act on real things. `--test` grants one command, `qwen-test`, but the tests it
runs are your repository's code running as you. `--browser` (also `--headed`,
`--browser-eval`, `--scenarios`, `-r tester`) is a real browser that opens whatever URL it
is told to; `--web` adds `WebFetch` and nothing else. `--record` and `--replay` run
model-written JavaScript with node, as you, unsandboxed: replay only folders you recorded
or have read. `--desktop APP` drives one native application (Windows, macOS, Linux X11)
with real mouse and keyboard input on your desktop, unsandboxed; its Bash is fenced to one
command, `qla-desktop`. Research goes online on purpose. A swarm never writes its `--target`; its
`sandbox` role works in a throwaway clone, again a copy and not a jail. The fence at a
glance, and the measured failure modes: [reference/limits.md](skill/local-auditor/reference/limits.md).

**Optional cloud advisor (experimental).** `qwen-agent --advisor opus` lets a session ask a
Claude model for advice on a hard decision through your own `claude` login, at most 4 calls
per run (`QWEN_ADVISOR_MAX_CALLS` sets the number; with `--until-done`, `0` turns the agent's own
asks off and leaves the harness's call after a failing round on, and without it `0` exits 2);
questions and the files the session attaches leave your machine, and nothing else changes. The idea comes from Claude's own [advisor tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool),
where a cheaper executor consults a stronger model at decision points. Under `--until-done`
the harness also asks after every failing round, with the failing output, and puts the advice into the next
round's prompt. On our benchmarks that call was the part that helped: it lifted Qwen from 17 to
19/20 on Aider and from 13 to 14/15 on hard Terminal-Bench tasks (one run each, $2-4 of Opus
calls). The coder's own asks mostly come at the wrong time, as Anthropic also reports for its
Haiku and Sonnet executors, so `--until-done --advisor opus` is the recommended use
([benchmark](skill/local-auditor/reference/benchmark.md#the-advisor),
[details](skill/local-auditor/reference/qwen-agent.md#advisor---advisor-model-experimental)).

`qwen-swarm ui-test` re-checks each FAIL/BLOCKED row with Claude **by default** (`confirm=claude`,
opus, up to `confirm_max` rows): the scenario, the tester's report and screenshots, the fixtures
and a browser on the app's URL go to Claude through your own `claude` login;
`--set confirm=local` keeps it on this machine, `--set confirm=none` skips it.

## Benchmark

Quality and price on 20 Aider polyglot exercises and 15 hard Terminal-Bench 2.0 tasks, two
rounds each, one run per setup (suggestive, not proven). The loop is the main gain: the same
local model in Claude Code alone solved 12-14/20 Aider exercises with a retry, and 17-18/20
under `--until-done`. The local model (Qwen3.8-Flash-Next,
$0 in API dollars) scored 17/20 and 13/15; with the Opus failure call, 19/20 and 14/15 for
$2.30 and $3.67. Claude Haiku 5.5 as the coder scored 20/20 for $1.30 and 15/15 for $7.07, a
strong result that moved the price bar: it is about as good as the local model on a first try,
better at fixing its own mistakes, and cheaper on easy tasks. The local setup's case is
offline, private and free per token, and it is still cheaper on the hard set. Method and
numbers: [reference/benchmark.md](skill/local-auditor/reference/benchmark.md).

## Reference pages

All under `skill/local-auditor/reference/`; the installed skills read the same files.

| page | covers |
|---|---|
| [configuration.md](skill/local-auditor/reference/configuration.md) | requirements, `install.sh`, the config file, every `QWEN_*` variable, troubleshooting |
| [qwen-agent.md](skill/local-auditor/reference/qwen-agent.md) | the core command: roles, tool policy, depth, browser testing, the advisor, exit codes |
| [coding.md](skill/local-auditor/reference/coding.md) | `qwen-test`, `--test`, `--until-done`: task files, the worktree, exit codes 11-14 |
| [sweep.md](skill/local-auditor/reference/sweep.md) | `qwen-sweep`: builders, flags, output files, writing a builder |
| [interactive.md](skill/local-auditor/reference/interactive.md) | `qwen-agent --interactive` and `qwen-cc` |
| [deep-research.md](skill/local-auditor/reference/deep-research.md) | `qwen-deep-research`: SearXNG or Brave setup, depth presets, run folder, resume |
| [swarm.md](skill/local-auditor/reference/swarm.md) | `qwen-swarm`: workflows, the manifest, fences, the `wf` API, the debug, ui-test and optimize workflows |
| [limits.md](skill/local-auditor/reference/limits.md) | the fence at a glance, and what this lane gets wrong, measured |
| [vllm.md](skill/local-auditor/reference/vllm.md) | serving Qwen on vLLM: the chat-template fix that keeps the prefix cache |
| [benchmark.md](skill/local-auditor/reference/benchmark.md) | full method and numbers |

Each skill's `SKILL.md` is the short version your Claude Code session reads:
[local-agent](skill/local-agent/SKILL.md), [local-auditor](skill/local-auditor/SKILL.md), [local-sweep](skill/local-sweep/SKILL.md),
[local-coder](skill/local-coder/SKILL.md), [local-deep-research](skill/local-deep-research/SKILL.md), [local-swarm](skill/local-swarm/SKILL.md).

## Development

```bash
python -m pytest            # unit tests plus offline end-to-end CLI tests
bash tests/test_install.sh  # install contract in a throwaway HOME
ruff check . && shellcheck install.sh skill/local-auditor/*.sh tests/*.sh tests/live/*.sh
```

The CLI tests use a fake `/v1/models` server and a fake `claude`, so nothing needs a GPU or
network; `TEST_BASH` picks the bash they run under. New sweep builders: [reference/sweep.md](skill/local-auditor/reference/sweep.md).

## License

[MIT](LICENSE)
