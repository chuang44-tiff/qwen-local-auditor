# Requirements, install and configuration

What the tools need from the machine, what `install.sh` does, where the config lives,
every `QWEN_*` variable the commands read, and the messages preflight prints when
something is off. `qwen-agent --help`, `qwen-swarm --help` and `qwen-deep-research --help`
are the authoritative texts; this page arranges the same facts for lookup.

## Requirements

| | |
|---|---|
| Claude Code CLI | `claude` on `PATH`, or `QWEN_CLAUDE_BIN` |
| A model server | serves the **Anthropic Messages API** at `/v1/messages`, and ideally lists models at `/v1/models` |
| bash | Linux, macOS (the stock bash 3.2 is fine), or **Git Bash** on Windows |
| Python 3.8+ | stdlib only; used to parse results, build sweeps and run the swarm engine (the test suite needs 3.10+) |
| curl, git | `git` for `qwen-sweep --builder diff`, for the throwaway worktrees `--test` uses, for `--until-done`'s clean-tree check, and for the sandbox copies a `qwen-swarm` workflow runs in |
| tmux | only for `qwen-cc` (interactive sessions); Windows has none: run `qwen-agent --interactive` in a terminal there |
| internet | needed by research (a search backend and pages to fetch): `qwen-deep-research`, `qwen-swarm research`, or a workflow with a `search` or `web` fence; and a browser's first run needs it too, for the `@playwright/mcp` npm package and the Playwright browser install it drives (`npx -y @playwright/mcp@0.0.83 --help` fetches the package, `npx playwright install chromium` the browser; [`qwen-agent.md`](qwen-agent.md) "Scripted UI suites"). No other fence adds a search or fetch tool, but a `browser` role drives a real browser, which opens whatever URL it is told to, and a `sandbox` agent's Bash is your shell, with your network ([`swarm.md`](swarm.md)) |

CI runs the whole suite on Linux, macOS (including `/bin/bash` 3.2) and Windows Git Bash.

### Model servers

Only the Messages API is strictly required. `/v1/models` lets the tools pick the model
and read its context window; without it, set `QWEN_MODEL`, `QWEN_CTX` and
`QWEN_PREFLIGHT=0`.

| what the tools read from `/v1/models` | used for |
|---|---|
| the model ids (`data[].id`, a bare list, or `models[].name`) | choosing the model; embedding/rerank models are ignored when picking |
| `max_model_len`, `context_length`, `max_context_length`, `loaded_context_length` or `max_input_tokens` | the context window, which sets autocompact and sweep budgets |

vLLM reports all of this. If your server does not report a window, preflight says so:
set `QWEN_CTX` to the window the server actually runs with (not the model's training
length), or long runs can overflow it. For serving Qwen on vLLM specifically, and the
chat-template change that keeps the prefix cache warm, see [`vllm.md`](vllm.md).

## Install

```bash
git clone https://github.com/chuang44-tiff/qwen-local-auditor.git
cd qwen-local-auditor
./install.sh
```

`install.sh`:

- symlinks the six skills (`local-agent`, `local-coder`, `local-auditor`, `local-sweep`,
  `local-deep-research`, `local-swarm`) into `~/.claude/skills/` (or
  `$CLAUDE_CONFIG_DIR/skills/`), so `git pull` updates them; the skills install together,
  and an incomplete checkout is refused;
- writes `qwen-agent`, `qwen-cc`, `qwen-deep-research`, `qwen-sweep`, `qwen-swarm` and
  `qwen-test` into `~/.local/bin` (keep that on `PATH`; it warns when it is not). Each is
  a forwarder that execs the script inside the installed `local-auditor` skill, so the
  commands and the skill cannot drift apart;
- copies `config.example` to `~/.config/qwen-agent/config` (or
  `$XDG_CONFIG_HOME/qwen-agent/config`) **once**, and never overwrites it;
- runs `qwen-agent --preflight-only` at the end when the config has been edited; with an
  untouched config it prints the next step instead. Exit 3 from that preflight means the
  server check failed (start the server, or fix `QWEN_BASE_URL` / `QWEN_MODEL` /
  `QWEN_API_KEY`); exit 8 means Claude Code or Python 3.8+ is missing (install them, or set
  `QWEN_CLAUDE_BIN` / `QWEN_PYTHON`).

| option | effect |
|---|---|
| `--no-preflight` | skip the server/python/claude checks |
| `--force` | replace a non-symlink skill directory instead of moving it aside to `NAME.bak-<UTC timestamp>` |
| `--uninstall` | remove the six skills and the six commands; keeps your config and past sweep runs |

Then point the config at your server and check it:

```bash
$EDITOR ~/.config/qwen-agent/config     # QWEN_BASE_URL, and QWEN_MODEL if it serves several
qwen-agent --preflight-only             # exit 0 = server up, model served, python and claude run
```

Upgrade with `git pull`. Re-run `./install.sh` only if it copied the skills (Windows,
below).

**Windows (Git Bash).** A real symlink needs Developer Mode or an elevated shell. Without
one, `install.sh` copies the skills instead and says so (the copy carries an
`.installed-copy` marker so a later run or `--uninstall` knows it is the installer's);
re-run it after each `git pull`. The skills go to `%USERPROFILE%\.claude`, where the native
`claude` looks, even if Git Bash's `$HOME` differs. If `python3` is the Microsoft Store
stub, set `QWEN_PYTHON=python` in the config: interpreters are tested by running them, not
by finding them on `PATH`. Write `QWEN_TEST_CMD` with forward slashes
(`C:/Python312/python.exe -m pytest`): it is split like a POSIX shell line, which eats
backslashes.

**macOS.** No GNU `timeout` is needed; a built-in watchdog enforces `--timeout` when
neither `timeout` nor `gtimeout` is installed.

## The config file

`$QWEN_CONFIG`, default `$XDG_CONFIG_HOME/qwen-agent/config` (else
`~/.config/qwen-agent/config`). Every command sources it as shell before applying its
defaults, so it is the place for the per-machine endpoint. Every value in `config.example`
is written as `${VAR:-...}`, which is what lets an environment variable or a command-line
flag still win:

```bash
QWEN_BASE_URL="${QWEN_BASE_URL:-http://192.0.2.10:8000}"   # no /v1 suffix
QWEN_MODEL="${QWEN_MODEL:-my-served-model}"               # only when several are served
QWEN_PYTHON="${QWEN_PYTHON:-python}"
```

Precedence, highest first: flags, then the environment, then the config file, then the
built-in defaults.

## Environment variables

### Endpoint, model and run limits (`qwen-agent`, inherited by every other command)

| variable | default | meaning |
|---|---|---|
| `QWEN_BASE_URL` | `http://127.0.0.1:8000` | server base URL, no `/v1` (`-b`) |
| `QWEN_MODEL` | the only served model | required when the server serves several (`-m`) |
| `QWEN_API_KEY` | `dummy` | sent as the auth token, and on the preflight request |
| `QWEN_CTX` | read from `/v1/models` | context window (`--ctx`) |
| `QWEN_AUTOCOMPACT` | 3/4 of the window | compact-and-continue point, passed as `claude --autocompact`: `auto`, or 100000-1000000 and below the window; keeps long runs off the server's hard limit (a 400 that kills the run). `--no-autocompact` omits the flag |
| `QWEN_EFFORT` | `medium` | passed to `claude --effort` (`-e`); `default` omits the flag. The level is also set as `CLAUDE_CODE_EFFORT_LEVEL` in the child, so Claude Code's internal calls use it too |
| `QWEN_EFFORT_ALLOWED` | *(any)* | e.g. `low medium xhigh` when a chat template rejects other levels; a level outside the list is refused before any model call (exit 2) |
| `QWEN_TIMEOUT` | `1800` | wall-clock seconds per run (`--timeout`); `0` is refused, `--no-timeout` really removes the limit |
| `QWEN_TIMEOUT_BIN` | GNU `timeout`, else `gtimeout`, else the watchdog | pins a timeout binary; `none` forces the built-in watchdog |
| `QWEN_PREFLIGHT` | `1` | `0` skips the `/v1/models` check (`QWEN_MODEL` is then required) |
| `QWEN_AUTO_MODEL` | `0` | `1` = `--auto-model`: if the configured model is not served but exactly one other is, use that |
| `QWEN_WEB` | `0` | `1` = `--web`: adds `WebFetch` (never `WebSearch`) |
| `QWEN_SUBAGENTS` | `0` | `1` = `--subagents`: adds the `Task` tool |
| `QWEN_SETTING_SOURCES` | *(claude's)* | passed to `claude --setting-sources`, e.g. `project,local`, so personal `~/.claude` settings cannot change results. Not passed under `--test` (`--restricted` already ignores settings files) |
| `QWEN_CUSTOM_HEADERS` | | passed to claude as `ANTHROPIC_CUSTOM_HEADERS` (gateways) |
| `QWEN_PYTHON` | first of `python3`, `python` that runs | Python 3.8+ for result parsing, probed by execution |
| `QWEN_CLAUDE_BIN` | `claude` | the Claude Code executable |
| `QWEN_ROLE_DIR` | | your own roles as `NAME.md` or `NAME.txt`, outside the clone |
| `QWEN_OUTDIR` | the current directory | where `-w` puts generated output files |
| `QWEN_CONFIG` | `$XDG_CONFIG_HOME/qwen-agent/config` | the config file itself |

The child process never inherits the parent Claude Code session's control channel, its
provider routing (Bedrock, Vertex, Foundry), `ANTHROPIC_API_KEY`, custom headers or model
overrides: every model alias is pointed at the served model.

### Tests and coding loops (`--test`, `qwen-test`, `--until-done`)

| variable | default | meaning |
|---|---|---|
| `QWEN_TEST_CMD` | *(unset; required by `--test`)* | the test command `qwen-test` runs; the model only picks which tests |
| `QWEN_TEST_TIMEOUT` | `600` | seconds per `qwen-test` run; must be positive |
| `QWEN_TEST_MAX_BYTES` | `20000` | cap on the test output returned to the model |
| `QWEN_AGENT_STATE` | `$XDG_CACHE_HOME/qwen-agent/runs` | `--until-done` run state and reports; must be outside the repo |

See [`coding.md`](coding.md) for how these are used.

### Sweeps (`qwen-sweep`)

| variable | default | meaning |
|---|---|---|
| `QWEN_SWEEP_CACHE` | `$XDG_CACHE_HOME/qwen-sweep`, else `~/.cache/qwen-sweep` | where sweep runs are kept, grouped per repo; never inside the repo under audit |
| `QWEN_BRIEF_DIR` | | your own sweep briefs as `NAME.md`, selected with `--brief NAME` |
| `QWEN_TRANSCRIPT_DIR` | the project's folder under the Claude config dir | where the `history` and `deviations` builders read Claude Code transcripts |

See [`sweep.md`](sweep.md).

### Deep research (`qwen-deep-research`)

`QWEN_SEARCH_URL` (SearXNG), `QWEN_SEARCH_KEY` (Brave), `QWEN_SEARCH_BACKEND`,
`QWEN_SEARCH_BRAVE_URL`, and the `QWEN_DR_*` defaults for its flags (`MAX_AGENTS`,
`MAX_ITEMS`, `SEATS`, `WEB_SEATS`, `TIMEOUT`, `RETRIES`, `HOURS`, `MAX_UNIT_SECONDS`,
`BACKOFF`) all go in the same config file. They are documented with the flags they back in
[`deep-research.md`](deep-research.md). The `QWEN_DR_` defaults apply to `qwen-swarm`'s
`research` workflow too, but a `QWEN_SWARM_` name of the same suffix wins there.

### Swarms (`qwen-swarm`)

| variable | default | meaning |
|---|---|---|
| `QWEN_SWARM_MAX_AGENTS`, `_MAX_ITEMS`, `_SEATS`, `_WEB_SEATS` | 8, 10, 4, `--seats` | defaults for the flags of the same names (the flags are in [`swarm.md`](swarm.md)) |
| `QWEN_SWARM_TIMEOUT`, `_RETRIES`, `_HOURS` | the depth preset's | defaults for `--timeout` (the per-item budget), `--retries` and `--hours` |
| `QWEN_SWARM_MAX_UNIT_SECONDS` | 14400 | the cap on one unit's timeout, retry doublings included; below 1 or non-numeric is a usage error (exit 2) |
| `QWEN_SWARM_BACKOFF` | 30 | seconds before re-spawning an agent after a server error (exit 3 or 4) |
| `QWEN_SWARM_SKIP_SEARCH_CHECK` | `0` | `1` skips the search half of the preflight, so a run of a search-using workflow and `--preflight WORKFLOW` check the model only and never probe the backend (`qwen-deep-research --check` is that preflight and is skipped too; a `qwen-swarm --check WORKFLOW` runs no preflight at all, set or not — it only dry-runs against fake agents. Test hook; `QWEN_DR_SKIP_SEARCH_CHECK` is the same on a research run) |
| `QWEN_SWARM_AGENT_OVERRIDE` | | a stand-in for `bash qwen-agent.sh` as the agent command, split on spaces (test hook) |
| `QWEN_SWARM_BASH` | set by `qwen-swarm.sh` to the bash that started it | the shell `wf.steps.run_cmd` uses in a sandbox: pins Git Bash on Windows instead of whatever a native interpreter finds on `PATH` |

The `QWEN_SEARCH_*` variables above are what a workflow with a `search` or `web` fence
searches through.

## Troubleshooting

| message | fix |
|---|---|
| `cannot reach …/v1/models` | the server is down or `QWEN_BASE_URL` is wrong |
| `…/v1/models answered 404` | remove a trailing `/v1` from `QWEN_BASE_URL`; or, if the server has no listing, set `QWEN_MODEL`, `QWEN_CTX` and `QWEN_PREFLIGHT=0` |
| `answered 401` / `403` | set `QWEN_API_KEY` |
| `several models are served` | set `QWEN_MODEL` (or pass `-m`) |
| `the server does not report a context window` | set `QWEN_CTX` |
| exit 2 `effort '…' is not accepted` | your `QWEN_EFFORT_ALLOWED` list refused it; pick a listed level |
| exit 8 `claude binary not found` / `no working Python 3.8+` | install Claude Code / Python, or set `QWEN_CLAUDE_BIN` / `QWEN_PYTHON` |
| API error 400 mid-run | often an effort level the chat template rejects: set `QWEN_EFFORT_ALLOWED`, or `QWEN_EFFORT=default` |
| `qwen-sweep` exit 9, `nothing to audit` | the glob matched nothing, the ref is wrong, or every item was withheld; see `needs-human.txt` |
| a sweep batch keeps failing | read that batch's `stderr.txt`; the last lines are also printed in the sweep output |
| `qwen-test: no test command configured` | set `QWEN_TEST_CMD` in the config |
| `qwen-deep-research --check` fails with `search:` | no search backend is configured; see [`deep-research.md`](deep-research.md) |
| near-zero prefix-cache hits on vLLM | see [`vllm.md`](vllm.md) |
