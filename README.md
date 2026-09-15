# qwen-local-auditor

A Claude Code skill, plus two command-line tools, that run a **headless Claude Code
session against a model you serve yourself** — for an independent second opinion, or
for bulk reading that would be expensive to do in your main session.

- **`qwen-agent`** — one task, one headless session, one result. Read-only by default.
- **`qwen-sweep`** — the same lane over many items (changed files, a file glob, claim
  documents, chunks of a large log), with batching, locking, retries and collation.
- **`local-auditor`** — the Claude Code skill that tells your main session when and how
  to use the two.

Claude Code routes providers per session, so a subagent cannot be pointed at a
different model. A separate headless `claude -p` process can. The "qwen" in the name is
historical: any model works if your server speaks the right API.

## Requirements

| | |
|---|---|
| Claude Code CLI | `claude` on `PATH` |
| A model server | serves the **Anthropic Messages API** at `/v1/messages`, and ideally lists models at `/v1/models` |
| bash | Linux, macOS (the stock bash 3.2 is fine), or **Git Bash** on Windows |
| Python 3.8+ | stdlib only; used to parse results and build sweeps (the test suite needs 3.10+) |
| curl, git | `git` only for `qwen-sweep --builder diff` |

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
length), or long runs can overflow it.

## Install

```bash
git clone https://github.com/chuang44-tiff/qwen-local-auditor.git
cd qwen-local-auditor
./install.sh
```

`install.sh`:

- symlinks `skill/local-auditor` into `~/.claude/skills/` (or `$CLAUDE_CONFIG_DIR/skills/`),
  so `git pull` updates it;
- writes `qwen-agent` and `qwen-sweep` into `~/.local/bin` (keep that on `PATH`);
- copies `config.example` to `~/.config/qwen-agent/config` **once**, and never overwrites it.

Then point the config at your server and check it:

```bash
$EDITOR ~/.config/qwen-agent/config     # QWEN_BASE_URL, and QWEN_MODEL if it serves several
qwen-agent --preflight-only             # exit 0 = server up, model served, python and claude run
```

Upgrade with `git pull`. Re-run `./install.sh` only if it copied the skill (below).
Remove with `./install.sh --uninstall`, which keeps your config.

**Windows (Git Bash).** A real symlink needs Developer Mode or an elevated shell. Without
one, `install.sh` copies the skill instead and says so; re-run it after each `git pull`.
The skill goes to `%USERPROFILE%\.claude`, where the native `claude` looks, even if Git
Bash's `$HOME` differs. If `python3` is the Microsoft Store stub, set `QWEN_PYTHON=python`
in the config: interpreters are tested by running them, not by finding them on `PATH`.

**macOS.** No GNU `timeout` is needed; a built-in watchdog enforces `--timeout` when
neither `timeout` nor `gtimeout` is installed.

## Use

```bash
# one question, read-only, scoped to a directory
qwen-agent -r auditor -C ./src "Which functions here write to disk? Cite path:line."

# from a file, into a file, in the background
qwen-agent -r auditor -C ./src -f brief.md -o findings.md -w

# many items: always --dry-run a new sweep first
qwen-sweep --builder diff  --repo . --base main --dry-run
qwen-sweep --builder files --repo . --glob 'src/**/*.py'
qwen-sweep --builder claims --repo . --docs docs/issues --items list.json
qwen-sweep --builder logs  --input build.log
```

`qwen-agent --help` and `qwen-sweep --help` are the complete references: roles
(`auditor`, `mechanic`, `plain`, or your own files), tool policy, sweep outputs
(`collated.json`, `needs-human.txt`), environment variables and exit codes. The skill's
[`SKILL.md`](skill/local-auditor/SKILL.md) is the short version your Claude Code session
reads.

**The rule that makes the output worth having: ask for extraction, not judgment.** "What
does this line do, cite it" is reliable; "is this good" is not. Read
[`reference/limits.md`](skill/local-auditor/reference/limits.md) before acting on a
verdict.

## Safety

- A bare `qwen-agent` run can only read: the toolset is `Read,Glob,Grep` and configured
  MCP servers are dropped. Editing needs `--write` (or the `mechanic` role); Bash needs
  `--all-tools` or an explicit `--toolset`, and prints a warning.
- The child process never inherits the parent session's control channel, its provider
  routing (Bedrock, Vertex, Foundry), `ANTHROPIC_API_KEY`, custom headers or model
  overrides, so a prompt cannot silently go to a cloud provider instead of your server.
- `--dry-run` shows the exact command and environment with secrets redacted.
- Sweep runs are written to your cache directory, never into the repository under audit.

## Configuration

Set in `~/.config/qwen-agent/config` (sourced as shell) or the environment. Flags win
over both.

| variable | default | meaning |
|---|---|---|
| `QWEN_BASE_URL` | `http://127.0.0.1:8000` | server base URL, no `/v1` |
| `QWEN_MODEL` | the only served model | required when the server serves several |
| `QWEN_API_KEY` | `dummy` | sent as the auth token, and on the preflight request |
| `QWEN_CTX` | read from `/v1/models` | context window |
| `QWEN_AUTOCOMPACT` | 3/4 of the window | compact-and-continue point; keeps long runs off the server's hard limit |
| `QWEN_EFFORT` | `medium` | passed to `claude --effort`; `default` omits the flag |
| `QWEN_EFFORT_ALLOWED` | *(any)* | e.g. `low medium xhigh` when a chat template rejects other levels |
| `QWEN_TIMEOUT` | `1800` | wall-clock seconds per run |
| `QWEN_PREFLIGHT` | `1` | `0` skips the `/v1/models` check |
| `QWEN_SETTING_SOURCES` | *(claude's)* | e.g. `project,local`, so personal `~/.claude` settings cannot change results |
| `QWEN_CUSTOM_HEADERS` | | extra headers for a gateway |
| `QWEN_PYTHON` | first of `python3`, `python` that runs | Python 3.8+ |
| `QWEN_ROLE_DIR`, `QWEN_BRIEF_DIR` | | your own roles and sweep briefs, outside the clone |
| `QWEN_SWEEP_CACHE` | `$XDG_CACHE_HOME/qwen-sweep` | where sweep runs are kept |

`qwen-agent --help` lists the rest (`QWEN_CLAUDE_BIN`, `QWEN_TIMEOUT_BIN`, `QWEN_OUTDIR`,
`QWEN_AUTO_MODEL`, `QWEN_CONFIG`).

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
| `qwen-sweep` exit 9, `nothing to audit` | the glob matched nothing, the ref is wrong, or every item was withheld — see `needs-human.txt` |
| a sweep batch keeps failing | read that batch's `stderr.txt`; the last lines are also printed in the sweep output |

## Development

```bash
python -m pytest            # unit tests plus offline end-to-end CLI tests
bash tests/test_install.sh  # install contract in a throwaway HOME
ruff check . && shellcheck install.sh skill/local-auditor/*.sh tests/*.sh
```

The CLI tests use a fake `/v1/models` server and a fake `claude`, so nothing needs a GPU
or network. Set `TEST_BASH` to run them under a specific bash. A new sweep builder goes
in `skill/local-auditor/lib/builders/`; see
[`reference/sweep.md`](skill/local-auditor/reference/sweep.md).

## License

[MIT](LICENSE)
