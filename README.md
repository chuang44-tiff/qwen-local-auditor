# qwen-local-auditor

Run Claude Code against a model you serve yourself. `qwen-agent` starts a separate,
headless Claude Code process pointed at your own server (vLLM serving Qwen, or anything
that speaks the Anthropic Messages API), fenced so that by default it never writes your
files. On top of it sit six Claude Code skills and six commands: a free second opinion on a diff,
bulk reading you would rather not spend frontier tokens on, a coding loop that runs until
its checks pass, an interactive session you can watch, web research with every claim
checked by independent agents, and a swarm that runs a whole workflow — many agents, in
rounds, over a codebase or over the web. The "qwen" in the name is historical; any model
works.

- **`qwen-agent`**: one question, one headless session, one answer. Your files are never written unless you say otherwise.
- **`qwen-sweep`**: the same question over many files, a diff, a set of documents or a large log.
- **`qwen-test`**: your test command in a throwaway git worktree — the one shell command a `qwen-agent --test` run is granted.
- **`qwen-cc`**: an interactive session on the local model inside tmux, which Claude Code can read, type into and stop.
- **`qwen-deep-research`**: a swarm of local sessions that researches a question on the web and writes a cited report.
- **`qwen-swarm`**: a workflow — the built-in `research` or `debug`, or one you write — run by many local agents in rounds, often overnight.

**Depth is the default.** A bare `qwen-agent` run in a git repo works in a throwaway
sandbox copy of your tree with a shell (Bash), Edit/Write and Task subagents, then a
second call reviews its own answer; the timeout is 3600 s. The copy is not a jail: the
shell runs as you, with your network. Your real tree is never written. `--shallow` (or
`QWEN_DEPTH=shallow`) restores the plain read-only run: `Read,Glob,Grep`, one call, no
shell, no subagents, no web. Details: [reference/qwen-agent.md](skill/local-auditor/reference/qwen-agent.md#depth-the-default).

The skills (`local-agent` routes; `local-auditor`, `local-sweep`, `local-coder`,
`local-deep-research` and `local-swarm` do the work) teach your Claude Code session when
and how to use these, so "have the local model review this" is enough.

## Requirements

- Claude Code (`claude` on `PATH`) and a server that serves the Anthropic Messages API at
  `/v1/messages`. vLLM does, and also lists models at `/v1/models`, which the tools use to
  pick the model and read its context window.
- bash (Linux, macOS, or Git Bash on Windows), Python 3.8+ (stdlib only), curl, git.
- tmux for `qwen-cc`. A search backend (SearXNG or a Brave key) is needed only by research
  — `qwen-deep-research`, or a `qwen-swarm` workflow whose roles have a `search` or `web`
  fence. Nothing else asks for one, and no other fence adds a search or fetch tool; a swarm
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
`~/.config/qwen-agent/config` once, never overwriting it. Then point the config at your
server and check it:

```bash
$EDITOR ~/.config/qwen-agent/config   # QWEN_BASE_URL, and QWEN_MODEL if it serves several
qwen-agent --preflight-only           # exit 0 = server up, model served, python and claude run
```

`git pull` upgrades. `./install.sh --uninstall` removes the skills and commands, keeps the config.

## Try it

**A second opinion.** Scope `-C` tightly, and ask for extraction ("what does this line do,
cite it"), not judgment ("is this good"). Roles, tool flags, background runs and exit codes
are in [reference/qwen-agent.md](skill/local-auditor/reference/qwen-agent.md).

```bash
qwen-agent -r auditor -C ./src "Which functions here write to disk? Cite path:line."
```

**The same question over many files.** Dry-run first to see the batches. Answers land in
`collated.json`; items with no evidence go to `needs-human.txt` instead of being asked.
Builders (`diff`, `files`, `claims`, `logs`, `history`, `deviations`), flags and output:
[reference/sweep.md](skill/local-auditor/reference/sweep.md).

```bash
qwen-sweep --builder diff --repo . --base main --dry-run
qwen-sweep --builder diff --repo . --base main
```

**Let it run your tests.** Set `QWEN_TEST_CMD` in the config first. `--test` grants
exactly one command, `qwen-test`, which you can run yourself too:

```bash
qwen-agent --test -r auditor -C . "Does parse() reject an empty list? Show a failing test if not."
qwen-test tests/test_x.py -k parse
```

**A coding task, looped until it is done.** `task.md` holds a goal, a spec and a checklist
whose items end in `-- check: test ID`, `-- check: cmd COMMAND` or `-- check: none`; the harness re-runs the
checks after every round and decides when to stop. Task-file format, exit codes and the
deviation audit: [reference/coding.md](skill/local-auditor/reference/coding.md).

```bash
qwen-agent --until-done task.md -C . --max-rounds 6
```

**Watch it work.** An interactive session in tmux that you, or your Claude Code session,
can read and type into ([reference/interactive.md](skill/local-auditor/reference/interactive.md)):

```bash
qwen-cc ~/proj                     # detached; prints session: and attach:
qwen-cc --peek NAME 100            # what it is doing, and what it is asking
qwen-cc --stop NAME
```

**Research a question on the web.** Scope it into angles, search, read the sources, have
independent agents try to refute every claim, write a cited report. The report's path is
the last line of output. Search-backend setup, depth presets and `--resume`:
[reference/deep-research.md](skill/local-auditor/reference/deep-research.md).

```bash
qwen-deep-research --check                             # model and search backend reachable?
qwen-deep-research "PRECISE QUESTION" --depth quick    # quick, standard, deep or overnight
```

**A swarm for any job.** `qwen-swarm` runs a workflow (a manifest, a short Python script
and role files) on many local agents, in rounds, often overnight; `qwen-deep-research` is
its `research` workflow. The built-in `debug` workflow finds a bug's root cause and a
patch it has checked in throwaway copies of your repo, never in the repo itself. Writing
your own workflow: [reference/swarm.md](skill/local-auditor/reference/swarm.md).

```bash
qwen-swarm --list                                        # built-in workflows
qwen-swarm debug "add(2, 3) returns -1" --target . --out ../debug-run --set repro="python -m pytest tests/test_calc.py"
qwen-swarm --check ./my-workflow                         # validate before a long run
```

## Safety

A bare run never writes your files. Under default depth, inside a git repo, it gets a
shell, Edit/Write and subagents in a throwaway sandbox copy of the tree — a copy, not a
jail: the shell runs as you, with your network, so it can reach the web and any path
you can. `--shallow` (or `--read-only`) keeps the old fence: the toolset is
`Read,Glob,Grep`, MCP servers are dropped, and editing, shell access, web access and
subagents are each a separate flag. Any run that can modify files or run a shell prints a
warning. `--test` opens exactly one command,
`qwen-test`, under `claude --restricted`, but the tests it runs are your repository's code
running as you, so use it only on code you would run yourself. Apart from that sandbox
shell, a `qwen-agent` run gets web tools only when you pass `--web`, which adds `WebFetch`
and nothing else, or `--browser`,
which gives it a real browser that opens whatever URL it is told to — `--headed`,
`--browser-eval`, `--scenarios` and `-r tester` each turn that browser on too. `--record` and
`--replay` run model-written JavaScript with node, as you, unsandboxed: replay only folders
you recorded or have read. Research goes
online on purpose — `qwen-deep-research`, `qwen-swarm research`, or any workflow with a
`search` or `web` fence — and its agents work in empty folders with no file tools. A swarm
`browser` role is meant for a local UI suite, and what it sees is kept: its screenshots and
page snapshots are written under `RUN/browser/<unit>/`. A swarm never writes its
`--target`: `read` roles only read it, and the edits and shell of a
`sandbox` role, along with the commands the engine runs to reproduce the bug and check a
patch, happen in a throwaway clone of it; you apply a winning patch with `git apply`. That
clone is a copy, not a jail — its shell runs as you, with your network — so run such a
workflow only on code you would run yourself. The child process never inherits your
session's API key, provider routing or model overrides, so a prompt cannot silently go to
a cloud provider. The measured failure modes, and how to read a verdict:
[reference/limits.md](skill/local-auditor/reference/limits.md).

**Optional cloud advisor.** Everything runs locally by default. `qwen-agent --advisor opus`
lets a session ask a Claude model for advice on a hard decision through your own `claude`
login, with at most 4 calls per run. Questions and the files the session attaches leave your
machine; nothing else changes and the session never depends on it.

## Benchmark

One local model (Qwen3.8-Flash-Next on vLLM, one workstation GPU) driving Claude Code on
20 Aider polyglot exercises: Claude Code alone solved 12/20, 14/20 with an independent
retry; `qwen-agent --until-done` solved 10/20 in its first round and **18/20** once the
second round saw the test failures. The gain is the loop, not the model; the two setups
also differ in role rules, shell access and an appended instruction, so read it with the
caveats on the benchmark page. Setup, how the runs were kept honest, concurrency figures:
[reference/benchmark.md](skill/local-auditor/reference/benchmark.md).

## Reference pages

All under `skill/local-auditor/reference/`; the installed skills read the same files.

| page | covers |
|---|---|
| [configuration.md](skill/local-auditor/reference/configuration.md) | requirements, what `install.sh` does, the config file, every `QWEN_*` variable, troubleshooting |
| [qwen-agent.md](skill/local-auditor/reference/qwen-agent.md) | the core command: roles, tool policy, output and background runs, exit codes |
| [coding.md](skill/local-auditor/reference/coding.md) | `qwen-test`, `--test`, `--until-done`: task files, the worktree, exit codes 11-14 |
| [sweep.md](skill/local-auditor/reference/sweep.md) | `qwen-sweep`: builders, flags, output files, the engine, writing a builder |
| [interactive.md](skill/local-auditor/reference/interactive.md) | `qwen-agent --interactive` and `qwen-cc` |
| [deep-research.md](skill/local-auditor/reference/deep-research.md) | `qwen-deep-research`: SearXNG or Brave setup, depth presets, flags, run folder, resume |
| [swarm.md](skill/local-auditor/reference/swarm.md) | `qwen-swarm`: workflows, the manifest, fences, the `wf` API, `--check`, the debug workflow |
| [limits.md](skill/local-auditor/reference/limits.md) | the fence at a glance, and what this lane gets wrong, measured |
| [vllm.md](skill/local-auditor/reference/vllm.md) | serving Qwen on vLLM for Claude Code: the chat-template fix that keeps the prefix cache |
| [benchmark.md](skill/local-auditor/reference/benchmark.md) | full method and numbers |

The skills' `SKILL.md` files are the short version your Claude Code session reads:
[local-agent](skill/local-agent/SKILL.md) (router), [local-auditor](skill/local-auditor/SKILL.md),
[local-sweep](skill/local-sweep/SKILL.md), [local-coder](skill/local-coder/SKILL.md),
[local-deep-research](skill/local-deep-research/SKILL.md), [local-swarm](skill/local-swarm/SKILL.md). Each command's `--help` is its
complete flag reference.

**Troubleshooting.** `qwen-agent --preflight-only` names what is wrong (server unreachable,
several models served, no context window reported, `claude` or Python missing); the
message-by-message table is in
[reference/configuration.md](skill/local-auditor/reference/configuration.md#troubleshooting).

## Development

```bash
python -m pytest            # unit tests plus offline end-to-end CLI tests
bash tests/test_install.sh  # install contract in a throwaway HOME
ruff check . && shellcheck install.sh skill/local-auditor/*.sh tests/*.sh
```

The CLI tests use a fake `/v1/models` server and a fake `claude`, so nothing needs a GPU
or network. Set `TEST_BASH` to run them under a specific bash; CI covers Linux, macOS
(bash 3.2) and Windows Git Bash. A new sweep builder goes in `skill/local-auditor/lib/builders/`
([reference/sweep.md](skill/local-auditor/reference/sweep.md)).

## License

[MIT](LICENSE)
