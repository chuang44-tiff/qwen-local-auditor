# `qwen-agent`: one task, one headless session, one result

`qwen-agent --help` is the complete, authoritative text. This page arranges the same
material for lookup: how a run is shaped, what each flag opens, and what the exit codes
mean. The coding modes (`--test`, `--until-done`) are in [`coding.md`](coding.md), the
interactive mode in [`interactive.md`](interactive.md), configuration in
[`configuration.md`](configuration.md).

## Why a separate process

Claude Code routes providers per session, so a subagent cannot be pointed at a different
model. A separate headless `claude -p` process can. `qwen-agent` spawns one, pointed at
your server, with a scrubbed environment and a tool fence, and parses the result.

## Usage forms

```bash
qwen-agent [options] <prompt>...          # positional; words are joined with a space
qwen-agent [options] -f prompt.md         # -f - reads stdin
cat task.md | qwen-agent [options] --stdin
qwen-agent --until-done task.md [-C DIR]  # coding loop; see coding.md
qwen-agent --interactive [-C DIR]         # a session for a person; see interactive.md
qwen-agent --preflight-only               # checks only, no model call
```

Exactly one prompt source. Quotes, newlines, backticks and `$` in a positional prompt are
passed through verbatim; nothing is eval'd, and a prompt starting with `-` is safe.

## Roles

| `-r NAME` | what it does |
|---|---|
| `auditor` | grounded review: cites `path:line`. Pass it for review work; with no `-r` there is no role prompt at all |
| `mechanic` | may edit; implies `--write` |
| `coder` | the role `--until-done` always runs with |
| `plain` | an empty role: no role instructions are added |
| anything else | resolved as `$QWEN_ROLE_DIR/NAME.md` (or `.txt`), then as a literal file path |

`--role-file F` uses a file as the role text, `-s TEXT` appends extra system-prompt text
after the role, `--list-roles` prints the resolvable names. Roles are appended to Claude
Code's own system prompt, never replacing it (replacing it breaks tool use).

## Tool policy: what a run may do

The default is read-only; every widening is a flag. Any run that can modify files or run
a shell (`--write`, the `coder` and `mechanic` roles, `--all-tools`, a `--toolset` naming Edit/Write/Bash, a
read-only `--test` run) prints a warning on stderr, and so does `--web` combined with
`--test`.

| you pass | the run gets |
|---|---|
| nothing, or `--read-only` | toolset `Read,Glob,Grep` and `--strict-mcp-config`: no Bash, no Write, configured MCP servers dropped. A schema-level restriction, not a permission prompt |
| `--write` | toolset `Read,Edit,Write,Glob,Grep`, `--permission-mode acceptEdits` unless you set one. Still no Bash. Role `mechanic` implies it |
| `--test` | one Bash command, `qwen-test`; `claude --restricted` and `--permission-mode dontAsk`. Not with `-w`, `--all-tools`, `--toolset`, `--read-only`, `-t/--tools` or any `--permission-mode`. See [`coding.md`](coding.md) |
| `--test-repo DIR` | the repo whose tests run (default: the `-C` directory) |
| `--all-tools` | no toolset restriction: every built-in, Bash and Write included, plus configured MCP servers; `--strict-mcp` off. The widest setting; costs many more input tokens because every tool schema is sent |
| `--toolset LIST` | passed as `claude --tools`: the real restriction, it removes every built-in you do not name. Overrides `--write`/`--all-tools`; `none` removes every built-in |
| `-t, --tools LIST` | passed as `--allowed-tools`: a grant for tools that are in the toolset. Restricts nothing and is not a sandbox. Default grants include read-only Bash (`ls`, `grep`, `cat`, `head`, `tail`, `wc`), effective only when the toolset has Bash |
| `--strict-mcp` | adds `--strict-mcp-config`, dropping configured MCP servers (`--toolset` governs built-ins only). On by default; `--all-tools` turns it off |
| `--mcp-config FILE` | load only the MCP servers in FILE (implies `--strict-mcp`), even with `--all-tools`; grant their tools with `-t`, e.g. `-t mcp__search__search` |
| `--web` (`QWEN_WEB=1`) | adds `WebFetch` to the toolset and the grants, for every role and for the fixed `--test` grant list. Never `WebSearch`: a server-side tool that local servers reject with a 400 (`body.tools.0.input_schema Field required`); search needs an MCP server. With `--test` it warns that tests can be gamed by fetching upstream answers |
| `--subagents` (`QWEN_SUBAGENTS=1`) | adds the `Task` tool so the model can hand broad reading to a subagent. Same model, same tool limits, one more concurrent request: leave it off on a small GPU |
| `--permission-mode M` | passed through to claude (e.g. `acceptEdits`, `plan`) |
| `-C, --cd DIR` | chdir before running; tool access is rooted at cwd. Scope it tightly: a bounded directory is the single biggest lever on output quality |
| `-D, --add-dir DIR` | an extra readable directory; repeatable |

What the fence does and does not guarantee, measured, is in [`limits.md`](limits.md).

## Depth switches (opt-in)

Four switches make a session go deeper. Each is off by default and is being measured
(seeded-defect recall and precision for audits, hidden-test pass rate for coding) before
any of them becomes a default. They compose with the other flags.

| switch | effect |
|---|---|
| `--role-variant deep` | the deep variant of `-r auditor` or `-r coder`. auditor-deep maps what the code must guarantee, tries to trigger each failure mode (with `--probe`: by running probes or tests), records evidence for every verdict, defaults to FAIL when evidence is missing, and lists unverified suspicions separately. coder-deep is the coder text plus an edge-case pass after the checks, ending with an `EDGE CASES` section. Other roles, `--role-file` and other variant names: exit 2. `-r` and `--list-roles` are unchanged |
| `--subagents-nudge` | `--subagents` plus a section on when to delegate (independent probes, big or many files, long logs), what to hand a subagent (a self-contained question and the paths) and to verify what it reports |
| `--review-round` | after a clean end, the session is resumed once (`--resume <session id>`) with a fixed prompt: try to break what you just did or reported, check each claim, revise, and give the complete answer again in the same format. The revised answer is the result. A failed review call leaves the first answer and exit code in place, with a `WARNING` on stderr. Each of the two calls gets the full `--timeout` |
| `--probe` | the session runs in a throwaway sandbox of the project: the git work tree holding `-C` (or `--test-repo DIR`), with your uncommitted and untracked files, never ignored ones. It gets Bash, Edit and Write, `--permission-mode dontAsk` and `claude --restricted`; denials are reported as usual. Nothing in your work tree, index or refs is ever written; because the sandbox shares your repository's object files, git may refresh their mtimes (no object's content changes). A write run (`--write`, `-r coder`, `-r mechanic`) reports its edits as a patch and applies nothing: `FILE.patch` next to `-o FILE` (always written, empty when nothing changed), else a new `qwen-agent-XXXXXXXX.patch` — in `QWEN_OUTDIR` when you set it, otherwise in the probe directory, never in the tree being probed (and only when something changed); the path and the shell-quoted, paste-ready `git -C <tree> apply` line are printed on stderr. The sandbox is removed at exit, also on a timeout or a signal |
| `--keep-sandbox` | with `--probe`: keep the sandbox and print its path (`sandbox kept: PATH`) |
| `--probe-here` | the `-C` directory is checked to be inside a sandbox kept by `--probe --keep-sandbox` (a plain checkout — yours above all — is refused with exit 2). There: the probe fence, nothing created or removed, no patch; `--test-repo` is refused, and `--test` runs the sandbox's own tests. This is how a probe session is resumed: `qwen-agent --probe-here -C <kept path> --resume ID "..."` (a fresh `--probe` refuses `--resume`, because Claude Code finds a session by its directory) |
| `--deep` | all four: `--probe --role-variant deep --review-round --subagents-nudge`; it takes no value, and combining it with another `--role-variant` is a usage error; needs `-r auditor` or `-r coder` (or `--until-done`); with `--probe-here` it resumes in the kept sandbox instead of making a new one |

`--probe` and `--probe-here` refuse `--interactive`, `-w`, `--all-tools`, `--toolset`,
`--read-only`, `-t/--tools`, `--permission-mode` and `-D/--add-dir` (exit 2); `--interactive`
refuses every switch above. Sandboxes — and the default patch of a `--probe --write` run
that gave no `-o` — live under `QWEN_PROBE_DIR` (default
`$XDG_CACHE_HOME/qwen-agent/probes`, else `~/.cache/qwen-agent/probes`), which may not be
inside the tree being copied. These runs clear `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`,
`GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` from the
environment, so an inherited git setting cannot point the session's git calls at your
tree. A sandbox protects your tree from accidents, not from a hostile model: a shell can
still `cd` out of it.

With `--json`, a run that used any switch carries a `qwen_agent` key next to Claude Code's
own fields (a run without one emits Claude Code's record unchanged):

```json
"qwen_agent": {
  "switches": {"probe": true, "role_variant": "deep", "review_round": true, "subagents_nudge": true},
  "review_round": {"status": "ok", "first_session": "<id>", "warning": null},
  "patch": "/path/out.json.patch",
  "sandbox": null
}
```

`review_round` (`status` `ok`, `failed` or `skipped`) is present with `--review-round`,
`patch` and `sandbox` (the kept path, else `null`) with `--probe`/`--probe-here`. With
`--until-done` the switches behave as described in [`coding.md`](coding.md).

## Model, context and effort

| flag | env | meaning |
|---|---|---|
| `-m, --model NAME` | `QWEN_MODEL` | served model; default the only served model |
| `-b, --base URL` | `QWEN_BASE_URL` | base URL, no `/v1` |
| `-e, --effort LEVEL` | `QWEN_EFFORT` | passed to `claude --effort`; default `medium`. `QWEN_EFFORT_ALLOWED` refuses other levels up front |
| `--ctx N` | `QWEN_CTX` | context window; default the model's `max_model_len` from `/v1/models`, else claude's own default |
| `--autocompact N` / `--no-autocompact` | `QWEN_AUTOCOMPACT` | compact-and-continue point; default 3/4 of a known window |
| `--auto-model` | `QWEN_AUTO_MODEL=1` | if the configured model is not served but exactly one other is, use it |
| `--no-preflight` | `QWEN_PREFLIGHT=0` | skip the `/v1/models` check |
| `--preflight-only` | | run the checks and exit: 0 usable, 2, 3 or 8 not. Also runs with `QWEN_PREFLIGHT=0` |
| `--timeout SECS` / `--no-timeout` | `QWEN_TIMEOUT` | wall clock, default 1800; `0` is refused |

Preflight exit 0 means the server is up, a model is actually served (the configured one,
or the only one), and a working python and `claude` were found by running them. Exit 3
names what failed and lists the models that are served.

## Output and background runs

| flag | meaning |
|---|---|
| `-o, --out FILE` | write the result to FILE instead of stdout. Relative to the current directory, not to `-C` |
| `-w, --detach` | run in the background and print the output path. Implies `-o` (a unique name under `QWEN_OUTDIR` if none is given); writes `FILE`, `FILE.err` and `FILE.status`. `FILE` does not exist until the run finishes: poll for a non-empty `FILE.status`, not for `FILE` |
| `--force` | with `-w -o FILE`, overwrite an existing `FILE`/`.status` instead of refusing |
| `--json` | emit Claude Code's full JSON result record, not just the text (`--json "count TODOs" \| jq -r .usage.input_tokens`) |
| `--resume ID` | continue Claude Code session ID; every run prints its session id on the status line |
| `--warn-denials` | treat tool-permission denials as a warning, not a failure (exit 7) |
| `-q, --quiet` | suppress the stderr status line |
| `--dry-run` | print the exact command and the child environment, secrets redacted, then exit |
| `-V, --version` | print the version |

Judge a result by its content, not the exit code: a tiny body with none of the expected
blocks has been seen at both rc=0 and rc=8 (see [`limits.md`](limits.md)).

## Exit codes

| exit | meaning |
|---|---|
| 0 | success |
| 2 | usage error (bad flag, missing or doubled prompt, refused combination) |
| 3 | preflight failed (server unreachable, or model not served; the message says which) |
| 4 | API error from the endpoint (status reported on stderr) |
| 5 | timed out after `--timeout` seconds |
| 6 | ran clean but returned no usable text |
| 7 | a tool call was blocked by the permission system (see `--warn-denials`) |
| 8 | harness failure (claude or python missing, or unparseable output) |
| 11-14 | `--until-done` outcomes; see [`coding.md`](coding.md) |

## Examples

```bash
qwen-agent "which files in this dir are shell scripts?"
qwen-agent -r auditor -C ./src "Which functions here write to disk? Cite path:line."
qwen-agent -r auditor -C ./src -f brief.md -o findings.md -w     # detached, from a file
qwen-agent --json "count TODOs" | jq -r .usage.input_tokens
qwen-agent -r mechanic "add a trailing newline to every .sh that lacks one"
qwen-agent --write --toolset 'Read,Edit,Glob,Grep' "retitle every heading"
```

The rule that makes the output worth having: ask for extraction, not judgment. "What does
this line do, cite it" is reliable; "is this good" is not. Hand it path headers: citations
were 35/35 correct when the context carried real paths, and ~40% were bare filenames when
it explored freely.
