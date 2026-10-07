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
| `tester` | drives a real browser through the Playwright tools; implies `--browser` (see "Browser testing" below) |
| `plain` | an empty role: no role instructions are added |
| anything else | resolved as `$QWEN_ROLE_DIR/NAME.md` (or `.txt`), then as a literal file path |

`--role-file F` uses a file as the role text, `-s TEXT` appends extra system-prompt text
after the role, `--list-roles` prints the resolvable names. Roles are appended to Claude
Code's own system prompt, never replacing it (replacing it breaks tool use).

## Tool policy: what a run may do

The default is read-only: every widening that can touch YOUR tree is a flag. A bare
run under the default depth does get Bash, Edit and Write (see
[`Depth`](#depth-the-default)), but only inside a throwaway sandbox copy of the
project — your tree is only ever read there. Any run that can modify files or run
a shell on your tree (`--write`, the `coder` and `mechanic` roles, `--all-tools`, a `--toolset` naming Edit/Write/Bash, a
read-only `--test` run) prints a warning on stderr, and so does `--web` or `--browser`
combined with `--test`.

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

## Depth: the default

Four switches make a session go deeper, and they are the DEFAULT: every direct run gets
the ones that FIT it, silently dropping the rest. `--shallow` (or `QWEN_DEPTH=shallow`,
environment or config) is the opt-out — the quick-question mode. What a run gets
unasked:

- `--role-variant deep` — only a role that HAS a deep variant: `auditor` and `coder`.
- `--review-round` and `--subagents-push` — every session; not `--interactive`, not
  `--resume`, not a mode that never starts one (`--preflight-only`), and not when
  `--toolset none` was typed. `--dry-run` starts no session and makes no second call,
  but it shows both: the delegation push is part of the printed argv, and the review
  round is noted as `# review round: one --resume call after the first`.
- `--probe` — only a READ-ONLY run (no `--write`, not `coder`/`mechanic`, no
  `--until-done`, not `--interactive`/`--resume`/`--probe-here`/`--browser`, no
  `--test` — its reproduction test must survive the run, and a sandbox deletes
  itself at exit —, no typed `-t/--tools`, `--toolset`, `--all-tools`,
  `--read-only`, `--permission-mode` or `-D/--add-dir`, and a sandbox that can
  actually be built here).

An implied part never causes a refusal: where a typed switch refuses, its implied
counterpart steps aside — including refusals only the sandbox build itself can know
(an ignored `-C` directory, a `--test-repo` that does not contain `-C`): one note on
stderr, and the run goes on unsandboxed. `--deep`'s own `--probe` is this implied one
(only a typed `--probe` is typed) and steps aside the same way — except where the run
WRITES: there the sandbox is the promise that the edits come back as a patch and your
tree stays untouched, so a build that cannot be made ends the run. Typed switches keep
every refusal.
`--shallow` with a typed part runs exactly that part; `--shallow` and `--deep`
together exit 2. A depth run — implied or typed — defaults `--timeout` to **3600 s**
(1800 with `--shallow`); a typed `--timeout` or `QWEN_TIMEOUT` always wins.
`--until-done` gets V, R and the delegation push on its coder rounds, `--probe` only
if typed, and the
3600 s default handed to every round through `QWEN_TIMEOUT` in their environment
unless one was typed; see [`coding.md`](coding.md).

| switch | effect |
|---|---|
| `--shallow` (`QWEN_DEPTH=shallow`) | no implied depth: only the switches you type. With `--deep`: exit 2 |
| `--role-variant deep` | the deep variant of `-r auditor` or `-r coder`, implied for exactly those two roles. auditor-deep maps what the code must guarantee, lists every public entry point and makes each carry a failure mode (or a one-line reason it cannot fail visibly), tries to trigger each failure mode (with `--probe`: by running probes or tests), records evidence for every verdict, defaults to FAIL when evidence is missing, finishes by attempting every failure mode it had not, and ends with a `COVERAGE` section (entry point by entry point, FAIL / PASS / UNVERIFIED per failure mode) plus unverified suspicions listed separately. coder-deep is the coder text plus an edge-case pass after the checks, ending with an `EDGE CASES` section and a finish check that names the probe and result per edge case or parks it under `NOT CHECKED`. Other roles, `--role-file` and other variant names: exit 2. `-r` and `--list-roles` are unchanged |
| `--subagents-nudge` | `--subagents` plus a section on when to delegate (independent probes, big or many files, long logs), what to hand a subagent (a self-contained question and the paths) and to verify what it reports. Typed; depth now implies `--subagents-push` instead, but `qwen-sweep` batches still nudge |
| `--subagents-push` | `--subagents` plus a section that makes delegation part of the task, not optional: split the work into independent areas, keep one, hand every other area to a subagent one at a time with a self-contained brief (exact paths, questions to answer, path:line evidence and the commands run), verify each subagent's key claims, and close with a `DELEGATION` section. It REPLACES the nudge text when both apply — push wins. Implied on every session by `--deep` and by the default depth; with `--review-round` the review prompt additionally hands the re-verification of the three most important claims to a subagent |
| `--review-round` | implied on every session. After a clean end, the session is resumed once (`--resume <session id>`) with a fixed prompt: try to break what you just did or reported, check each claim, revise, and give the complete answer again in the same format. The prompt also asks for a `REVIEW` section — each earlier claim or change re-checked, the check run, and what changed (kept, corrected, dropped) — with particular attention to what the first pass never covered; with `--subagents-push` it adds handing the re-verification of the three most important claims to a subagent and comparing. The revised answer is the result. A failed review call leaves the first answer and exit code in place, with a `WARNING` on stderr — and so does a review answer shorter than 300 characters when the first answer had 1000 or more (a stub is the auto-compact failure, not a review). Each of the two calls gets the full `--timeout` |
| `--probe` | implied for a read-only run (and it steps aside, unheard, wherever a typed one would refuse or the sandbox cannot be built). The session runs in a throwaway sandbox of the project: the git work tree holding `-C` (or `--test-repo DIR`), with your uncommitted and untracked files, never ignored ones. It gets Bash, Edit and Write — even a read-only role such as `auditor`, since the sandbox is throwaway and no patch is reported for a read-only role, so a scratch file is a tool call, not a denial —, `--permission-mode dontAsk` and `claude --restricted`; denials are reported as usual. Nothing in your work tree, index or refs is ever written; because the sandbox shares your repository's object files, git may refresh their mtimes (no object's content changes). A write run (`--write`, `-r coder`, `-r mechanic`) reports its edits as a patch and applies nothing: `FILE.patch` next to `-o FILE` (always written, empty when nothing changed), else a new `qwen-agent-XXXXXXXX.patch` — in `QWEN_OUTDIR` when you set it, otherwise in the probe directory, never in the tree being probed (and only when something changed); the path and the shell-quoted, paste-ready `git -C <tree> apply` line are printed on stderr. The sandbox is removed at exit, also on a timeout or a signal |
| `--keep-sandbox` | with `--probe`: keep the sandbox and print its path (`sandbox kept: PATH`) |
| `--probe-here` | the `-C` directory is checked to be inside a sandbox kept by `--probe --keep-sandbox` (a plain checkout — yours above all — is refused with exit 2). There: the probe fence, nothing created or removed, no patch; `--test-repo` is refused, and `--test` runs the sandbox's own tests. This is how a probe session is resumed: `qwen-agent --probe-here -C <kept path> --resume ID "..."` (a fresh `--probe` refuses `--resume`, because Claude Code finds a session by its directory) |
| `--deep` | all four TYPED at once: `--probe --role-variant deep --review-round --subagents-push`, with the typed refusals intact (the implied default drops what does not fit instead of refusing; deep's own `--probe` still steps aside where the sandbox cannot be built, as the implied one — a writing run refuses instead); it takes no value, and combining it with another `--role-variant` is a usage error; needs `-r auditor` or `-r coder` (or `--until-done`); with `--probe-here` it resumes in the kept sandbox instead of making a new one; with `--shallow`: exit 2 |

`--probe` and `--probe-here` refuse `--interactive`, `-w`, `--all-tools`, `--toolset`,
`--read-only`, `-t/--tools`, `--permission-mode` and `-D/--add-dir` (exit 2); `--interactive`
refuses every switch above. Sandboxes — and the default patch of a `--probe --write` run
that gave no `-o` — live under `QWEN_PROBE_DIR` (default
`$XDG_CACHE_HOME/qwen-agent/probes`, else `~/.cache/qwen-agent/probes`), which may not be
inside the tree being copied. Every run that USES a sandbox (a typed `--probe`,
`--probe-here` or `--deep`, or an implied `--probe` that was not stepped aside)
clears `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`,
`GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` from the
environment, so an inherited git setting cannot point the session's git calls at your
tree; a run with no sandbox keeps them. A sandbox protects your tree from accidents, not from a hostile model: a shell can
still `cd` out of it.

With `--json`, a depth run carries a `qwen_agent` key next to Claude Code's own fields
saying which depth ran and which switches were used (a `--shallow` run that typed no
switch emits Claude Code's record unchanged):

```json
"qwen_agent": {
  "depth": "default",
  "switches": {"probe": true, "role_variant": "deep", "review_round": true, "subagents_nudge": false, "subagents_push": true},
  "review_round": {"status": "ok", "first_session": "<id>", "warning": null},
  "patch": "/path/out.json.patch",
  "sandbox": null
}
```

`depth` is `"default"` (the implied set), `"deep"` (`--deep` typed) or `"shallow"`;
`switches` names the switches actually used — `subagents_push` appears only on runs
that pushed (`--subagents-push`, typed or implied; a nudge run says
`"subagents_nudge": true` without it). `review_round` (`status` `ok`, `failed` or
`skipped`) is present with `--review-round`, `patch` and `sandbox` (the kept path, else
`null`) with `--probe`/`--probe-here`. A run whose final answer is shorter than 300
characters after more than 20 requests (the auto-compact failure signature) adds
`"short_answer_warning": true` and prints `WARNING: the answer is suspiciously short
for a long session (possible auto-compact failure); check it` on stderr; the answer
still stands. With `--until-done` the switches behave as described in
[`coding.md`](coding.md).

## Browser testing (opt-in)

`--browser` gives the session a real browser. qwen-agent writes an MCP config for
**one server**, `playwright` — the command `npx -y --prefer-offline
@playwright/mcp@0.0.83` with `--headless --isolated --output-dir <run folder>
--image-responses allow --viewport-size 1280,900` — into a fresh browser run folder
and passes it with `--mcp-config`, which implies `--strict-mcp-config`: no other MCP
server loads. On Git Bash (native Windows Claude Code) the built-in command is written
as `cmd /c npx -y --prefer-offline @playwright/mcp@0.0.83`: Claude Code cannot spawn
`npx` there (it is `npx.cmd`, and without the `cmd /c` wrapper the server connection
just closes); `QWEN_PLAYWRIGHT_MCP` overrides that form too (below). The browser tools
join the run's existing `--allowed-tools` list (one
list, on top of whatever toolset the run has — `--tools` governs built-ins only, MCP
tools survive it, so the read-only default stays):

```
browser_navigate, browser_navigate_back, browser_snapshot, browser_find, browser_click,
browser_type, browser_fill_form, browser_press_key, browser_select_option, browser_hover,
browser_drag, browser_drop, browser_file_upload, browser_handle_dialog, browser_tabs,
browser_resize, browser_emulate_media, browser_wait_for, browser_take_screenshot,
browser_console_messages, browser_network_requests, browser_network_request, browser_close
```

Each is granted as `mcp__playwright__<name>`. The server **offers more tools than that**,
and a tool that is merely not granted still reaches the model's tool list — where the model
tries it and the run dies on the refusal. So the tools qwen-agent does not grant go to
Claude Code as one `--disallowedTools` value (comma-joined, one flag) too, which keeps them
out of the model's sight entirely:

| hidden tool | why |
|---|---|
| `mcp__playwright__browser_run_code_unsafe` | runs arbitrary code in the browser's own process; never granted, always hidden |
| `mcp__playwright__browser_install` | downloads a browser mid-run; never granted, always hidden |
| `mcp__playwright__browser_evaluate` | runs arbitrary JavaScript inside the page; ungranted **and** hidden unless `--browser-eval`, which both grants it and stops hiding it |

| switch | effect |
|---|---|
| `--browser` | refused with `--mcp-config` ("--browser brings its own MCP config"), `--until-done` and `--interactive` (exit 2); allowed with `--probe` and `--write`; the `tester` role implies it |
| `--headed` | a visible browser instead of headless. Needs `DISPLAY` or `WAYLAND_DISPLAY` set, else exit 2 ("--headed needs a display (DISPLAY is not set)"). The server entry carries an `env` copied from qwen-agent's environment: `DISPLAY` (and `XAUTHORITY` when set), or — with only `WAYLAND_DISPLAY` set — `WAYLAND_DISPLAY` (and `XDG_RUNTIME_DIR` when set) and no `DISPLAY` key at all. Implies `--browser` |
| `--browser-eval` | grants `mcp__playwright__browser_evaluate` and stops hiding it; implies `--browser` |

`--browser` can open **any URL, the internet included**, independently of `--web`:
`--web` gates only the `WebFetch` built-in, and the browser needs no web opt-in to
navigate off localhost. With `--test` it prints the same style of warning as `--web`
with `--test` (`WARNING: --browser with --test: tests and checks can be gamed by
browsing upstream answers`): the browser reaches the same upstream pages a run's
tests and checks should derive their answers from.

The run folder is
`${QWEN_BROWSER_DIR:-$XDG_CACHE_HOME/qwen-agent/browser}/<UTC stamp>-XXXXXX` (else
`~/.cache/...`; `mktemp -d`) — the default is never the caller's or the `-C`
directory; a RELATIVE `QWEN_BROWSER_DIR` resolves against the caller's directory
(like `QWEN_OUTDIR`). It is made only once every refusal, validation and preflight
has passed and claude is about to start: a run that fails before that, and every
`--dry-run`, creates nothing (`--dry-run` shows `<browser run folder>/mcp.json` as
the would-be config path). It is the server's `--output-dir`: screenshots, page
snapshots and downloads land there. Its path is printed on stderr (`browser:
screenshots and page snapshots in PATH`) and the folder is **kept** after the run:
it is the evidence. Under `--probe` or `--test` the session may be unable to `Read`
screenshot files from the run folder; the screenshot image itself still reaches the
model inline (`--image-responses allow`).
`QWEN_BROWSER_DIR` moves it; `QWEN_PLAYWRIGHT_MCP` replaces the whole
`npx -y --prefer-offline @playwright/mcp@0.0.83` part of the server command (and the
Git Bash `cmd /c` form of it), split on whitespace with pathname expansion off: the
first word is the command, the rest its leading args, and a `*` in the value stays
literal (`node /x/*.js` reaches the config as one word).

The role `tester` implies `--browser` and carries the browser-testing method (snapshot,
exercise, chain actions into sequences and check the state they leave, compare, screenshot;
defects with steps/expected/actual/evidence). It has no role variants. With `--json`,
`qwen_agent` carries `"browser": {"dir": PATH, "headed": bool, "eval": bool}`.

The first run needs the npm package and a Playwright browser installed:

```bash
npx -y @playwright/mcp@0.0.83 --help      # fetches the package; --help proves it runs
npx playwright install chromium           # fetches the browser itself
```

```bash
qwen-agent -r tester "test the sign-up form at http://localhost:3000"
qwen-agent -r tester --headed "watch the checkout flow at http://localhost:3000"
```

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
| `--timeout SECS` / `--no-timeout` | `QWEN_TIMEOUT` | wall clock; default 3600 whenever depth is on (the default), 1800 with `--shallow`; a set `QWEN_TIMEOUT` wins over both; `0` is refused |

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
