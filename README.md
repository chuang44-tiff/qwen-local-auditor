# local-agent skills for Claude Code

Four Claude Code skills, plus four command-line tools, that run a **Claude Code session
against a model you serve yourself** — headless for an independent second opinion, for
bulk reading that would be expensive to do in your main session, or for a coding task
that is looped until its checks pass; interactive, in tmux, when you want to type into it.

Skills:

- **`local-agent`** — the router: runs a preflight, then hands off to one of the other three.
- **`local-auditor`** — a read-only second opinion on a diff, a file or a claim; may write a
  reproduction test in a throwaway worktree.
- **`local-sweep`** — the same read-only question over many files or items.
- **`local-coder`** — implement a change against a task file and checklist, looping until
  every check passes.

Commands:

- **`qwen-agent`** — one task, one headless session, one result. Read-only by default.
  `--until-done` loops a coding task against a checklist.
- **`qwen-sweep`** — the same lane over many items (changed files, a file glob, claim
  documents, chunks of a large log, transcript history, spec deviations), with batching,
  locking, retries and collation.
- **`qwen-test`** — the only shell command a local agent is ever granted: it runs your
  configured test command in a throwaway git worktree.
- **`qwen-cc`** — opens an **interactive** session (`qwen-agent --interactive`) inside
  tmux, then peeks at its screen, types into it and stops it.

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
| tmux | only for `qwen-cc` (interactive sessions); Windows has none — run `qwen-agent --interactive` in a terminal there |

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

- symlinks the four skills (`local-agent`, `local-coder`, `local-auditor`, `local-sweep`)
  into `~/.claude/skills/` (or `$CLAUDE_CONFIG_DIR/skills/`), so `git pull` updates them;
- writes `qwen-agent`, `qwen-cc`, `qwen-sweep` and `qwen-test` into `~/.local/bin` (keep
  that on `PATH`);
- copies `config.example` to `~/.config/qwen-agent/config` **once**, and never overwrites it.

Then point the config at your server and check it:

```bash
$EDITOR ~/.config/qwen-agent/config     # QWEN_BASE_URL, and QWEN_MODEL if it serves several
qwen-agent --preflight-only             # exit 0 = server up, model served, python and claude run
```

Upgrade with `git pull`. Re-run `./install.sh` only if it copied the skills (below).
Remove with `./install.sh --uninstall`, which removes the four skills and the four
commands and keeps your config.

**Windows (Git Bash).** A real symlink needs Developer Mode or an elevated shell. Without
one, `install.sh` copies the skills instead and says so; re-run it after each `git pull`.
The skills go to `%USERPROFILE%\.claude`, where the native `claude` looks, even if Git
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

### Interactive sessions: qwen-cc

`qwen-agent --interactive` runs preflight and resolves the config like any other run, then
execs an **interactive** `claude` on your server — the same model, tiers, effort and
scrubbed environment, and no tool fence at all, because the person at the keyboard answers
Claude Code's own permission prompts. It takes no prompt, and refuses every flag that
would drive or fence the session instead of leaving it to that person: a prompt, `-f`,
`--stdin`, `--until-done`, `--test`, `--write`, `--all-tools`/`--unrestricted`,
`--toolset`, `-t`/`--tools`, `--web`, `--subagents`, `--json`, `-o`, `-w`, `--resume`,
`-r`/`--role`, `-s`/`--system`, `--read-only`, `--strict-mcp`. `--dry-run` is allowed and
prints the exact command line and child env with the auth token redacted.

`qwen-cc` runs that inside tmux, so a session can be opened, read, typed into and stopped
without anyone attaching a terminal:

```bash
qwen-cc ~/proj                     # a detached session: prints session: and attach:
qwen-cc --list                     # the sessions qwen-cc created, and only those
qwen-cc --peek NAME 100            # what it is doing, and what it is asking
qwen-cc --say NAME "run the tests" # type a line into it, then Enter
qwen-cc --stop NAME                # Ctrl-C and /exit; --stop --force NAME kills it
```

Over SSH there is no desktop to open a window on, so a launch also prints a `remote:` line
(`ssh -t user@host tmux attach -t NAME`) to paste into a terminal on your own machine. The
first session in a folder stops at Claude Code's "trust this folder?" prompt; attach and
answer it yourself.

A launch takes one directory (it must exist; the current directory by default) and names
the session `qwen-<dir>-<HHMMSS>`, tagged `@qwen_cc=1` — which is what `--peek`, `--say`
and `--stop` insist on before touching anything. `--window` also opens a terminal attached
to it (automatic when `DISPLAY` or `WAYLAND_DISPLAY` is set; `--no-window` declines);
`--dry-run` prints the tmux and window commands instead of running them; anything after
`--` goes to `qwen-agent`. Answering the session's permission prompts stays the decision of
the person at the keyboard — `--say` is for what the user asked you to hand over.

### Tests and coding tasks

```bash
# let a read-only run execute your tests (and write reproduction tests in a worktree)
qwen-agent --test -r auditor -C . "Does parse() reject an empty list? Show a failing test if not."

# a coding task, looped until every check passes
qwen-agent --until-done task.md -C . --max-rounds 6
```

`--test` grants the run one Bash command, `qwen-test`, which runs `QWEN_TEST_CMD` (set it
in the config) in a throwaway git worktree; the model only chooses which tests, as test
ids, paths or `-k EXPR`. It always passes `claude --restricted` and `--permission-mode
dontAsk` (write/coder runs still edit, because `--allowed-tools` grants them the edit
tools), and cannot be combined with `-w`, `--all-tools`, `--toolset`,
`--read-only`, `-t`/`--tools` or any `--permission-mode` — an explicit `-t` grant
list would replace the qwen-test-only grants. You can run it
yourself too: `qwen-test tests/test_x.py -k parse`. It exits 0 passed, 1 failed or error,
2 usage or refused selector, 5 timeout.

`--until-done TASK` reads a task file whose checklist items end in `-- check: test ID`,
`-- check: cmd COMMAND` or `-- check: none` (reported UNVERIFIED, never done). It always
runs the `coder` role with `--test`, resumes one session each round with what still fails,
and decides "done" itself by running the checks. It takes no prompt, and refuses every
option that would collide with the loop it owns or widen a round's `--test` fence: `-f`,
`--stdin`, `--resume`, `-w`, `-o`, `--dry-run`, `--json`, any role other than `coder`
(`-r`/`--role`), `--role-file` (it would replace the coder role and turn the rounds
read-only), `--toolset`, `--read-only`,
`--all-tools`, `--unrestricted`, any `--permission-mode`, `-t`/`--tools`, and
`-D`/`--add-dir` (a coder's file access stops at its own tree; `-D /` would open the
whole disk). Options: `--max-rounds N`
(default 8), `--budget-tokens N`, `--budget-seconds N`, `--allow-dirty`,
`--no-deviation-audit`. The deviation audit is an agent call like a round: if it fails
with a usage error or a server error, the run exits 2 or 4. A report is written to the state directory
(`QWEN_AGENT_STATE`, default `$XDG_CACHE_HOME/qwen-agent/runs`, which must be outside the
repo).

| exit | meaning |
|---|---|
| 11 | `--until-done` stopped at the round limit or a budget, or the checks pass but the deviation audit was unusable twice (partial; report written; review the diff manually) |
| 12 | no progress: the same checks failed and the tree was unchanged two rounds in a row |
| 13 | working tree dirty at start (commit, or `--allow-dirty`) |
| 14 | another `--until-done` run holds this repo's lock |
| 130 | interrupted (Ctrl-C); report written |

Running tests runs the repository's code: use `--test` only on code you would run
yourself. See [`reference/limits.md`](skill/local-auditor/reference/limits.md).

`qwen-sweep --builder history --repo . --arg files=src/a.py` and
`--builder deviations --repo . --base main --arg spec=SPEC.md` read the current project's
Claude Code transcripts as evidence; see
[`reference/sweep.md`](skill/local-auditor/reference/sweep.md).

`qwen-agent --help`, `qwen-sweep --help` and `qwen-cc --help` are the complete references: roles
(`auditor`, `mechanic`, `plain`, or your own files), tool policy, sweep outputs
(`collated.json`, `needs-human.txt`), environment variables and exit codes. The skills' `SKILL.md` files
([`local-agent`](skill/local-agent/SKILL.md) is the router, then
[`local-auditor`](skill/local-auditor/SKILL.md), [`local-sweep`](skill/local-sweep/SKILL.md)
and [`local-coder`](skill/local-coder/SKILL.md)) are the short version your Claude Code
session reads.

**The rule that makes the output worth having: ask for extraction, not judgment.** "What
does this line do, cite it" is reliable; "is this good" is not. Read
[`reference/limits.md`](skill/local-auditor/reference/limits.md) before acting on a
verdict.

## Safety

- A bare `qwen-agent` run can only read: the toolset is `Read,Glob,Grep` and configured
  MCP servers are dropped. Editing needs `--write` (or the `mechanic` or `coder` role);
  Bash needs `--test` (which grants only `qwen-test`), `--all-tools` or an explicit
  `--toolset`, and each prints a warning. Under `--test`, Claude Code itself also
  auto-approves read-only shell commands that stay inside the working directory (seen
  with 2.1.288: `cat README.md` and `grep -r` ran; `cat /etc/hostname`, `ls /`, writes,
  `python3` and `curl` were denied). That is the same reach as the `Read`/`Grep` tools:
  no writes, no execution, no network, nothing outside the working directory.
- Subagents are opt-in too: `--subagents` (`QWEN_SUBAGENTS=1`) adds the `Task` tool so
  the model can hand broad reading to a subagent and keep its own context small. A
  subagent runs on the same model with the same tool limits, and is one more concurrent
  request against your server, so leave it off on a small GPU. Without `--test` the model
  may also pick your user, plugin or repo-defined agents, each with its own prompt.
- No run reaches the web unless you ask: web access is opt-in with `--web`
  (`QWEN_WEB=1`), and it adds only `WebFetch` — never `WebSearch`, which is a
  server-side tool vLLM rejects anyway (400 `body.tools.0.input_schema Field
  required`; search needs an MCP server). Keep web off for test-driven work: in the
  benchmark, given web access the model went looking for the upstream tests and
  reference solutions.
- `--test` limits the shell, not what code runs: the tests `qwen-test` runs, including any
  the model wrote or edited, execute as you. Every `--test` run passes
  `claude --restricted`, so your own Claude settings cannot widen the fence.
- The child process never inherits the parent session's control channel, its provider
  routing (Bedrock, Vertex, Foundry), `ANTHROPIC_API_KEY`, custom headers or model
  overrides, so a prompt cannot silently go to a cloud provider instead of your server.
- `--dry-run` shows the exact command and environment with secrets redacted.
- Sweep runs are written to your cache directory, never into the repository under audit.
- `qwen-cc` reads, types into and kills only the tmux sessions it created (tagged
  `@qwen_cc=1`) and refuses every other session. What it cannot limit is what a typed line
  does: `--say` is how an interactive session's permission prompts get answered, so that
  stays a human decision (the skills tell your Claude Code session to answer one only when
  the user explicitly asks).

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
| `QWEN_TEST_CMD` | *(unset; required by `--test`)* | the test command `qwen-test` runs |
| `QWEN_TEST_TIMEOUT` | `600` | seconds per `qwen-test` run |
| `QWEN_TEST_MAX_BYTES` | `20000` | cap on the test output returned to the model |
| `QWEN_AGENT_STATE` | `$XDG_CACHE_HOME/qwen-agent/runs` | `--until-done` run state and reports; outside the repo |
| `QWEN_TRANSCRIPT_DIR` | the project's folder under the Claude config dir | where `history` / `deviations` read transcripts |
| `QWEN_SWEEP_CACHE` | `$XDG_CACHE_HOME/qwen-sweep` | where sweep runs are kept |

`qwen-agent --help` lists the rest (`QWEN_CLAUDE_BIN`, `QWEN_TIMEOUT_BIN`, `QWEN_OUTDIR`,
`QWEN_AUTO_MODEL`, `QWEN_CONFIG`).

## Serving Qwen on vLLM for Claude Code: keep the prefix cache

**Symptom.** Claude Code against vLLM's `/v1/messages` gets near-zero prefix-cache
hits. At startup vLLM logs a warning that the chat template "requires system-first
ordering" and that the conversation "misses the prefix cache".

**Cause.** Claude Code sends per-turn reminders as inline system messages. When the
model's chat template rejects non-leading system messages (Qwen templates raise
"System message must be at the beginning."), vLLM merges them into the leading
system prompt, so the prompt prefix changes every turn.

**Fix.** Copy your model's `chat_template.jinja`, change the raise into rendering
the message in place as a user turn, and start vLLM with `--chat-template` pointing
at the copy:

```diff
     {%- if message.role == "system" %}
         {%- if not loop.first %}
-            {{- raise_exception('System message must be at the beginning.') }}
+            {{- '<|im_start|>user\n' + content + '<|im_end|>\n' }}
         {%- endif %}
```

**Verify.** The startup warning disappears; prompts without inline system messages
render unchanged.

**Measured.** One Claude Code review task went from 0% to 53-56% of prompt tokens
served from cache; a long `qwen-agent --until-done` session reached ~94%. On hybrid
(linear-attention) models the cache reuses whole blocks only, so short prompts can
still show 0%.

**Upstream.** [vllm-project/vllm#53393](https://github.com/vllm-project/vllm/issues/53393)
and [vllm-project/vllm#58772](https://github.com/vllm-project/vllm/pull/58772) (a
configurable fix; retire this workaround once it lands and you have measured it).

## Benchmark: a local model, with and without the loop

One local model (Qwen3.8-Flash-Next, NVFP4, served by vLLM on one workstation GPU with
`--max-num-seqs 4`) driving Claude Code 2.1.286 at `--effort xhigh`, on a fixed sample of
20 Aider polyglot exercises (seeded, stratified: Python 4, JavaScript 4, Go 3, Rust 3,
Java 3, C++ 3) and 10 Terminal-Bench 2.0 tasks (3 easy, 6 medium, 1 hard), run with
[Harbor](https://github.com/laude-institute/harbor).

| setup | first attempt | with a second chance |
|---|---|---|
| Claude Code alone (Harbor `claude-code` agent), Aider polyglot | 12/20 | 14/20 (independent retry) |
| Claude Code alone, Terminal-Bench 2.0 | 9/10 | 10/10 (independent retry) |
| `qwen-agent --until-done`, Aider polyglot | 10/20 | **18/20** (second round sees the test failures) |

In this setup the same model and the same exercises went from 14/20 to 18/20 with a
supervisor that runs the hidden tests after a round and hands the failures back (Aider's own
protocol: one attempt, then one more with the test output). The gain is the second round:
the loop's first round scored 10/20, below Claude Code alone, and the two setups also differ
in role rules, shell access and the appended instruction. Zero harness failures in the final
runs: no API errors, timeouts, context overflows or crashes.

How the runs were kept honest:

- **No answer keys.** Given network access, the model went looking for the exercises'
  upstream tests and reference solutions (`curl`, `WebFetch`, `git clone`). The final runs
  disable `WebFetch`/`WebSearch` and block the hosts that serve them; every transcript is
  scanned for attempts (all blocked). Terminal-Bench graders download a tool from a GitHub
  release, so `github.com` stays reachable there; raw files, archives and the API do not.
- **All tests count.** Harbor's Aider adapter runs only the first test case for C++, Rust and
  JavaScript (Exercism marks the rest as skipped). The runs here enable every case; the
  reference solutions pass all 20 exercises under the patched graders.
- **Not leaderboard numbers.** A 20-exercise sample, tests hidden from the model, one
  machine. Harbor attempts are independent; the `--until-done` coder's only shell grant
  was `qwen-test`, set up to compile its code (plus the read-only commands Claude Code
  allows inside the working directory), while Harbor's agent had a full shell. The Harbor runs
  append a short, generic instruction (hidden tests will run, network to code hosts is
  blocked, check your output against the spec, do not add unrequested leniency).

Concurrency (Aider sample, one attempt, server capped at 4 running requests): 2, 4 and 6
concurrent sessions kept time-to-first-token under 1 s (0.45, 0.45, 0.75 s) with aggregate
decode rising 135 -> 218 -> 283 tok/s; at 8 sessions throughput stopped rising (287 tok/s)
and time-to-first-token reached 3.9 s. Agent sessions spend much of their time in tools, so
4 server slots serve about 6 sessions. Prefix-cache hit rate was 64-84% throughout, with no
preemptions.

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
