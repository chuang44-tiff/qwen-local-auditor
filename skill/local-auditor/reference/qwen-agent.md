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

Your files are never written by a bare run: every widening that can touch YOUR tree
is a flag. Inside a git repo, a bare run under the default depth gets Bash, Edit,
Write and Task subagents (see [`Depth`](#depth-the-default)) in a throwaway sandbox
copy of the project. The harness only reads your tree, but the copy is not a jail:
the shell runs as you, with your network, and can reach any path you can by its
absolute name. `--shallow` (or `QWEN_DEPTH=shallow`) or `--read-only` keeps the plain
`Read,Glob,Grep` fence; outside a git repo there is no sandbox and a bare run stays
read-only, with Task subagents and a review call. Any run that can modify files or run
a shell on your tree (`--write`, the `coder` and `mechanic` roles, `--all-tools`, a `--toolset` naming Edit/Write/Bash, a
read-only `--test` run) prints a warning on stderr, and so does `--web` or `--browser`
combined with `--test`.

| you pass | the run gets |
|---|---|
| `--read-only`, or nothing under `--shallow` | toolset `Read,Glob,Grep` and `--strict-mcp-config`: no Bash, no Write, configured MCP servers dropped. A schema-level restriction, not a permission prompt |
| nothing (default depth) | in a git repo: the `--probe` sandbox below (Bash, Edit, Write in a copy, `dontAsk`, `--restricted`) plus Task and a review call. Elsewhere, or where the sandbox cannot be built: `Read,Glob,Grep` plus Task and a review call |
| `--write` | toolset `Read,Edit,Write,Glob,Grep`, `--permission-mode acceptEdits` unless you set one. Still no Bash. Role `mechanic` implies it |
| `--test` | one Bash command, `qwen-test`; `claude --restricted` and `--permission-mode dontAsk`. Not with `-w`, `--all-tools`, `--toolset`, `--read-only`, `-t/--tools` or any `--permission-mode`. See [`coding.md`](coding.md) |
| `--test-repo DIR` | the repo whose tests run (default: the `-C` directory) |
| `--all-tools` | no toolset restriction: every built-in, Bash and Write included, plus configured MCP servers; `--strict-mcp` off. The widest setting; costs many more input tokens because every tool schema is sent |
| `--toolset LIST` | passed as `claude --tools`: the real restriction, it removes every built-in you do not name. Overrides `--write`/`--all-tools`; `none` removes every built-in |
| `-t, --tools LIST` | passed as `--allowed-tools`: a grant for tools that are in the toolset. Restricts nothing and is not a sandbox. Default grants include read-only Bash (`ls`, `grep`, `cat`, `head`, `tail`, `wc`), effective only when the toolset has Bash |
| `--strict-mcp` | adds `--strict-mcp-config`, dropping configured MCP servers (`--toolset` governs built-ins only). On by default; `--all-tools` turns it off |
| `--mcp-config FILE` | load only the MCP servers in FILE (implies `--strict-mcp`), even with `--all-tools`; grant their tools with `-t`, e.g. `-t mcp__search__search` |
| `--web` (`QWEN_WEB=1`) | adds `WebFetch` to the toolset and the grants, for every role and for the fixed `--test` grant list. Never `WebSearch`: a server-side tool that local servers reject with a 400 (`body.tools.0.input_schema Field required`); search needs an MCP server. With `--test` it warns that tests can be gamed by fetching upstream answers |
| `--subagents` (`QWEN_SUBAGENTS=1`) | adds the `Task` tool so the model can hand broad reading to a subagent. Same model, same tool limits, one more concurrent request: leave it off on a small GPU. Subagents run in the foreground (every run but `--interactive` sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`): one that reported after the answer would start another turn, and the reply to it would replace the answer as the result |
| `--advisor MODEL` | ask a Claude model for advice through your claude login (see Advisor); off by default |
| `--permission-mode M` | passed through to claude (e.g. `acceptEdits`, `plan`) |
| `-C, --cd DIR` | chdir before running; tool access is rooted at cwd. Scope it tightly: a bounded directory is the single biggest lever on output quality |
| `-D, --add-dir DIR` | an extra readable directory; repeatable |

What the fence does and does not guarantee, measured, is in [`limits.md`](limits.md).

## Depth: the default

Four switches make a session go deeper, and they are the DEFAULT: every direct run gets
the ones that FIT it, silently dropping the rest. `--shallow` (or `QWEN_DEPTH=shallow`,
environment or config) is the opt-out — one call, read-only, the quick-question mode.
What a run gets
unasked:

- `--role-variant deep` — only a role that HAS a deep variant: `auditor` and `coder`.
- `--review-round` and `--subagents-push` (or the nudge, when `--subagents-nudge`
  is typed) — every session; not `--interactive`, not
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
counterpart steps aside — including what only the sandbox build itself can know (an
ignored `-C` directory, a `--test-repo` that does not contain `-C`, a probe directory
that cannot be created, a failed clone, a symlink Windows will not make) and a Claude
Code without `--restricted`: one note on stderr, and the run goes on unsandboxed and
read-only, as under `--shallow`. An untracked nested git repository is not copied into
the sandbox. `--deep`'s own `--probe` is this implied one
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
| `--subagents-nudge` | `--subagents` plus a section on when to delegate (independent probes, big or many files, long logs), what to hand a subagent (a self-contained question and the paths) and to verify what it reports. Depth implies `--subagents-push` instead; a typed nudge is kept (the push is then not added). `qwen-sweep` batches and `qwen-swarm` roles with tools nudge |
| `--subagents-push` | `--subagents` plus a section that makes delegation part of the task, not optional: split the work into independent areas, keep one, hand every other area to a subagent one at a time with a self-contained brief (exact paths, questions to answer, path:line evidence and the commands run), verify each subagent's key claims, and close with a `DELEGATION` section. It REPLACES the nudge text when both apply — push wins. Implied on every session by `--deep` and by the default depth; with `--review-round` the review prompt additionally hands the re-verification of the three most important claims to a subagent |
| `--review-round` | implied on every session. After a clean end, the session is resumed once (`--resume <session id>`) with a fixed prompt: try to break what you just did or reported, check each claim, revise, and give the complete answer again in the same format. The prompt also asks for a `REVIEW` section — each earlier claim or change re-checked, the check run, and what changed (kept, corrected, dropped) — with particular attention to what the first pass never covered; with `--subagents-push` it adds handing the re-verification of the three most important claims to a subagent and comparing. The revised answer is the result. A failed review call leaves the first answer and exit code in place, with a `WARNING` on stderr — and so does a review answer shorter than 300 characters when the first answer had 1000 or more (a stub is the auto-compact failure, not a review). Each of the two calls gets the full `--timeout` |
| `--probe` | implied for a read-only run (and it steps aside, with one note on stderr, wherever a typed one would refuse or the sandbox cannot be built). The session runs in a throwaway sandbox of the project: the git work tree holding `-C` (or `--test-repo DIR`), with your uncommitted and untracked files, never ignored ones. It gets Bash, Edit and Write — even a read-only role such as `auditor`, since the sandbox is throwaway and no patch is reported for a read-only role, so a scratch file is a tool call, not a denial —, `--permission-mode dontAsk` and `claude --restricted`; denials are reported as usual. Nothing in your work tree, index or refs is ever written; because the sandbox shares your repository's object files, git may refresh their mtimes (no object's content changes). A write run (`--write`, `-r coder`, `-r mechanic`) reports its edits as a patch and applies nothing: `FILE.patch` next to `-o FILE` (always written, empty when nothing changed), else a new `qwen-agent-XXXXXXXX.patch` — in `QWEN_OUTDIR` when you set it, otherwise in the probe directory, never in the tree being probed (and only when something changed); the path and the shell-quoted, paste-ready `git -C <tree> apply` line are printed on stderr. The sandbox is removed at exit, also on a timeout or a signal |
| `--keep-sandbox` | with a typed `--probe` (`--probe --keep-sandbox`): keep the sandbox and print its path (`sandbox kept: PATH`). The sandbox depth makes on its own is always removed; one that cannot be removed is reported as `sandbox not removed: PATH` |
| `--probe-here` | the `-C` directory is checked to be inside a sandbox kept by `--probe --keep-sandbox` (a plain checkout — yours above all — is refused with exit 2). There: the probe fence, nothing created or removed, no patch; `--test-repo` is refused, and `--test` runs the sandbox's own tests. This is how a probe session is resumed: `qwen-agent --probe-here -C <kept path> --resume ID "..."` (a fresh `--probe` refuses `--resume`, because Claude Code finds a session by its directory) |
| `--deep` | all four TYPED at once: `--probe --role-variant deep --review-round --subagents-push`, with the typed refusals intact (the implied default drops what does not fit instead of refusing; deep's own `--probe` still steps aside where the sandbox cannot be built, as the implied one — a writing run refuses instead); it takes no value, and combining it with another `--role-variant` is a usage error; needs `-r auditor` or `-r coder` (or `--until-done`); with `--probe-here` it resumes in the kept sandbox instead of making a new one; with `--shallow`: exit 2 |

`--probe` and `--probe-here` refuse `--interactive`, `-w`, `--all-tools`, `--toolset`,
`--read-only`, `-t/--tools`, `--permission-mode` and `-D/--add-dir` (exit 2); `--interactive`
refuses every switch above. Sandboxes — and the default patch of a `--probe --write` run
that gave no `-o` — live under `QWEN_PROBE_DIR` (default
`$XDG_CACHE_HOME/qwen-agent/probes`, else `~/.cache/qwen-agent/probes`; a relative value is
relative to the caller's directory), which may not be inside the tree being copied. Every run that USES a sandbox (a typed `--probe`,
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
browser_console_messages, browser_network_requests, browser_close
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
| `mcp__playwright__browser_evaluate` | runs arbitrary JavaScript inside the page; ungranted **and** hidden unless `--browser-eval`, which grants it and `browser_network_request` and stops hiding either |
| `mcp__playwright__browser_network_request` | answers ONE request WITH its response body — the app's scripts, styles and server replies, i.e. its source; ungranted **and** hidden unless `--browser-eval` (the request LIST `browser_network_requests` — URLs, methods, statuses, no bodies — stays granted) |

**Black-box testing:** a tester tests behaviour from the outside, so the two tools that
reach the application's source — `browser_evaluate` and `browser_network_request`'s
response bodies — are hidden by default, and `--browser-eval` is the explicit opt-in into
both. That is the only part enforced: the browser can still navigate to a script URL
such as `/app.js` and read it as a page, and the request list names the script URLs.
The tester's role text forbids verdicts from source; that is an instruction, not a fence.

| switch | effect |
|---|---|
| `--browser` | refused with `--mcp-config` ("--browser brings its own MCP config"), `--until-done` and `--interactive` (exit 2); allowed with `--probe` and `--write`; the `tester` role implies it |
| `--headed` | a visible browser instead of headless. Needs `DISPLAY` or `WAYLAND_DISPLAY` set, else exit 2 ("--headed needs a display (DISPLAY is not set)"). The server entry carries an `env` copied from qwen-agent's environment: `DISPLAY` (and `XAUTHORITY` when set), or — with only `WAYLAND_DISPLAY` set — `WAYLAND_DISPLAY` (and `XDG_RUNTIME_DIR` when set) and no `DISPLAY` key at all. Implies `--browser` |
| `--browser-eval` | grants `mcp__playwright__browser_evaluate` and `mcp__playwright__browser_network_request` and stops hiding them; implies `--browser` |
| `--scenarios FILE` | scripted UI suite: implies `--browser` and `-r tester`, the file is the prompt; refused with another `-r`, `-f`, a prompt argument, `--stdin`, `--until-done`, `--interactive` (exit 2); see "Scripted UI suites" below |

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
it is the evidence — and the session can READ it: the folder joins `--add-dir`
(this own entry never trips `--probe`'s `-D`/`--add-dir` refusal, the run folder
is not the user's tree) and `Read` joins `--allowed-tools` even under
`--toolset none` (Read alone; under `none` it joins `--tools` too). That matters
because Playwright 0.0.83 hands the screenshot image back to the model only when
`browser_take_screenshot` is called WITHOUT a filename (`--image-responses
allow`); given one it returns a link and the image only lands in the run folder.
`QWEN_BROWSER_DIR` moves it (a `qwen-swarm` role with the `browser` fence sets it per unit
to `RUN/browser/<unit>`, so every unit's evidence lands inside that run —
[swarm.md](swarm.md)); `QWEN_PLAYWRIGHT_MCP` replaces the whole
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

### Scripted UI suites (`--scenarios`)

`--scenarios FILE` hands the tester a written suite instead of a sentence: it
implies `--browser` and `-r tester`, the **prompt IS the suite's task text**
(built by `lib/scenarios.py` from the file — the same text the answer is later
scored against), and the answer is scored per scenario. A relative `FILE`
resolves against the caller's directory (like `-o`), never against `-C`.

The file is Markdown:

- `# Suite: <name>` — required, the first heading;
- `base: <url>` — optional, before the first scenario; relative "Open" targets
  in steps resolve against it (it only means something before the first scenario);
- `## Scenario: <title>` — one or more;
- `id: <id>` — optional per scenario; the default is `s1`, `s2`, … in file
  order; ids match `[A-Za-z0-9_-]+` and must be unique;
- `steps:` then a numbered list — at least one step;
- `expect:` then a bullet list — at least one expectation.

Anything else inside a scenario (blank lines, notes) is ignored. Every parse
error names its line ("`line 7: ...`"), and the file is validated **before
claude could start**: a broken suite is exit 2 and nothing runs. Complete
example — a cart page, two scenarios, one named id and one default:

```markdown
# Suite: Cart page
base: http://localhost:3000/cart

## Scenario: add an item
id: add-item
steps:
1. Open the cart page
2. Click Add on "Widget"
expect:
- The cart shows 1 item
- The subtotal is 9.00

## Scenario: remove an item
steps:
1. Click Remove on the only line
expect:
- The cart is empty
```

```bash
qwen-agent --scenarios cart-suite.md          # no prompt argument: the suite is one
```

The tester is told to run the scenarios in order, each from a fresh page load,
and to end its answer with ONE fenced json block
`{"results": [{"id": ..., "status": "PASS"|"FAIL"|"BLOCKED", ...}]}` with one
entry per scenario id (`BLOCKED` means a step could not be performed). The
**last** such block is the report: an id it omits is BLOCKED ("no result
reported"), an id it does not know is ignored, a status outside the three is
BLOCKED with a note saying what it was, and an answer with no parsable block
leaves every scenario BLOCKED ("no result block").

After the run (and after the review round, when depth adds one) qwen-agent
writes `<browser run folder>/results.json` (one entry per scenario, file order)
and `<browser run folder>/summary.md`, prints the table and the results path on
**stderr** — the answer keeps stdout or `-o` — and exits: the run's own code
when the run itself failed, else **0 when every scenario PASSed, 9 when any is
FAIL or BLOCKED**. With `--json` the record carries
`qwen_agent.scenarios = {"file": FILE, "results": PATH, "pass": n, "fail": n,
"blocked": n}`.

```
$ qwen-agent --scenarios cart-suite.md
qwen-agent: browser: screenshots and page snapshots in ~/.cache/qwen-agent/browser/20261006T093012Z-aB3cD
qwen-agent: | id | status | notes |
|---|---|---|
| add-item | PASS |  |
| s2 | FAIL | the row stayed in the cart after Remove |

PASS 1 / FAIL 1 / BLOCKED 0
qwen-agent: results: ~/.cache/qwen-agent/browser/20261006T093012Z-aB3cD/results.json
The answer (the tester's report) is on stdout.   $ echo $?
9
```

`--scenarios` is refused (exit 2, claude never started) with another `-r`,
`-f`, a prompt argument, `--stdin`, `--until-done` and `--interactive` — the
suite supplies the prompt, the role and the browser. The scoring lives in
`skill/local-auditor/lib/scenarios.py`, runnable on its own:
`scenarios.py check FILE` (`ok: N scenarios` / exit 2 + the error),
`scenarios.py prompt FILE`, `scenarios.py results FILE ANSWER_FILE OUT_JSON` —
and the two replay commands, below.

### Record and replay (`--record` / `--replay`)

A scenario suite costs a model run every time it is asked, and its answers vary.
`--record` turns one scored run into a replay that costs neither.

**The scripts are code the model wrote, and they run on your machine.** `--record`
runs every script the session wrote with `node`, as you, unsandboxed and with your
network, before deciding which to keep (the folder is named on stderr first);
`--replay DIR` runs DIR's scripts the same way. The manifest's sha256 only proves the
bytes are the ones that were recorded, not that they are harmless. Replay only folders
you recorded or have read, and read the kept scripts before relying on them.

```bash
qwen-agent --scenarios cart-suite.md --record ./cart-replay
```

After the suite is scored (and after the review round, when depth adds one) the
session is resumed **once** with a fixed prompt that turns each scenario it ran
into a deterministic Playwright script — `<browser run folder>/replay/<id>.mjs`:
a self-contained Node ES module importing `{ chromium } from 'playwright'`,
launching it headless, opening the page fresh, performing the scenario's steps
with stable selectors (roles, labels, visible text; never coordinates), asserting
EVERY expectation, and printing exactly one line — `RESULT <id> PASS` or
`RESULT <id> FAIL: <the expectation that did not hold>` — then exiting 0 (1 only
on a script error). It reads the base URL from `$QWEN_REPLAY_BASE`, whose own
default is the suite's base URL. For that one call the session may write inside
`<browser run folder>/replay/` and nowhere else, and its reply (the list of files
it wrote) is not the run's answer: the answer stays the tester's scored report.

Then every script is replayed and kept **only when it answers what the run
answered** — PASS replaying PASS, FAIL replaying FAIL (a scenario that failed and
reproduces is exactly what a later run must be able to retake). What is rejected,
with its reason in the manifest:

| rejected | because |
|---|---|
| `recorded BLOCKED` | a step could not be performed, so nothing was established about it |
| `not run (no result recorded)` | the run reported nothing for that id |
| `no script was written` | the record round left that scenario out |
| `replay said PASS, the run recorded FAIL: …` | the script does not test what was tested, or the app moved under it |
| `replay said ERROR, the run recorded FAIL: …` | the script errored — crashed, timed out, or caught its own exception and printed it as a FAIL (a note starting `script error`, `Error:`, `locator.click:`… or saying `Timeout …ms exceeded`): an error is `ERROR`, never a kept FAIL |

The kept scripts are copied into `--record`'s directory with a `manifest.json`
(`suite`, `file` — the suite file absolute, `base`, `recorded` — UTC time,
`scripts[]` of `id`/`verdict`/`sha256`/`file`, and `rejected[]` of `id`/`reason`),
and `recorded: N of M scenarios (rejected: ids)` plus the manifest path print on
**stderr** beside the scored summary. Validation needs `node` and the
`playwright` package (below), and asks the scripts the question a later replay
asks them — no base override, so a kept script passes because of what it does and
not because of what it was told.

Later the same verdicts come back with **no model call at all**: no preflight, no
claude, no browser MCP server, seconds — deterministic for the same page state; a
changed app can change the result.

```bash
qwen-agent --replay ./cart-replay
qwen-agent --replay ./cart-replay -b http://127.0.0.1:4000/cart   # another deployment
```

```
$ qwen-agent --replay ./cart-replay
qwen-agent: replay: ~/work/cart-replay
qwen-agent: | id | status | notes |
|---|---|---|
| add-item | PASS |  |
| s2 | FAIL | the row stayed in the cart after Remove |

PASS 1 / FAIL 1 / ERROR 0   $ echo $?
9
```

Each script's bytes are checked against the manifest's `sha256` before anything
runs: a script that changed since recording is `ERROR` ("script changed since
recording") and is never run — the manifest is what says a PASS was ever observed
for this file. `node` then runs each one (120 s each) in a fresh temporary
directory with `NODE_PATH` set to the playwright package's `node_modules`, and
with `QWEN_REPLAY_BASE` set when a base was given. Its `RESULT` line is the
verdict — `PASS`, `FAIL` with the expectation that did not hold, or `ERROR` (it
crashed, printed no `RESULT` line, ran out its clock, or printed its own error as a
`FAIL`) — the table and
`PASS n / FAIL n / ERROR n` print on **stderr**. The scenarios the recording kept no
script for are listed as `NOT RECORDED` with their reason (and counted as `NOT
RECORDED n` when there are any); they are not run and do not change the exit. The exit
is **0 when every kept script PASSED, 9 when any is FAIL or ERROR, and 9 when the
folder kept no script at all** (a recording that kept nothing proves nothing): the code
`--scenarios` gives, because a replay is those verdicts taken again. A manifest entry
whose file is not a plain name inside the folder is `ERROR` and never runs, and a
script's `RESULT` line counts only when it names that script's scenario.

A replay asks nothing of a model, so every model-shaped flag is refused with exit
2 naming the flag — `-r`, `-f`, a prompt argument, `--stdin`, `--scenarios`,
`--until-done`, `--interactive`, `--deep`, `-m`, `--timeout`, `--browser`,
`--json`, `-o`, `-w`. `-b/--base URL` (the URL the scripts open, handed to them as
`QWEN_REPLAY_BASE`, overriding the recorded base) and `-q` are what it accepts.
`node` must be on PATH — the scripts are Node ES modules — and the `playwright`
package is resolved in this order: `QWEN_PLAYWRIGHT_NODE_PATH`, a `node_modules`
**directory** holding it; else a `node_modules/playwright` in the npx cache under
`$(npm config get cache)/_npx/*/node_modules`. Neither: exit 2 with the fix in the
message (`npm i -g playwright && npx playwright install chromium`), never scripts
silently skipped. On Windows the runner links `node_modules` beside each script with a
symbolic link, else a directory junction; when neither can be made the replay stops
with the reason (enable Developer Mode) instead of reporting every scenario as ERROR.

The runner is `lib/scenarios.py`, runnable on its own:

```bash
python3 skill/local-auditor/lib/scenarios.py replay-check SUITE.md RESULTS.json REPLAY_DIR OUT_DIR
python3 skill/local-auditor/lib/scenarios.py replay OUT_DIR --base http://127.0.0.1:3000 --out replay-results.json
```

**Re-record when the app moved.** A kept script is frozen bytes — that is what
makes it deterministic — so after a UI change a scenario whose markup was renamed
comes back `ERROR`, with no `RESULT` line because its selector no longer resolves.
That is the signal to record again (`--scenarios FILE --record DIR`), not to edit
the script by hand: an edited script fails its sha256 and `--replay` will not run
it. The replay names which scenario to re-check; only a model can say what the new
right answer is.

## Advisor (`--advisor MODEL`, experimental)

Inspired by Claude's [advisor tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool):
a cheaper model does the work and asks a stronger one at decision points. Here the local
Qwen session is the executor. It is experimental and not proven effective: Qwen is not
trained to use an advisor, so it decides on its own when to ask and rarely does. In a blind
audit A/B it found 2 more of 51 known bugs, which is below our bar of 3. Its calls helped
classify findings, not find them. In coder runs (`--until-done`) it asked twice in 22 runs,
and both answers changed or should have changed the outcome. Read the advisor log: the
advice is not binding, and the session can ignore it.

The session gets one more tool, `ask`: it sends one self-contained question, the evidence,
and up to 5 files (40 KB each, 120 KB in all, inside the working directory) to a Claude
model through **your own `claude` login**, and gets advice back as a tool result. Each call
is one `claude -p` run with no tools, no settings, no hooks and one turn, so one call is one
request to that model, at Anthropic's default effort. The Qwen session keeps doing the work;
the advisor never edits or runs anything.

- Off unless you type `--advisor MODEL`. No environment variable or config line turns it on, so sweeps and swarms never send code out on their own.
- `--until-done` turns the advisor on for its coder rounds only when you type `--advisor MODEL` on the `--until-done` command itself: every round of the loop then gets the advisor
  through ONE budget shared by the whole run (the loop makes one state directory and hands it to each round with the internal `--advisor-state DIR`), and its deviation audit
  never gets it.
- `QWEN_ADVISOR_MAX_CALLS` (default 4) is shared by the first answer, the review round and
  subagents — and, under `--until-done`, by every round and review round of the loop.
  `QWEN_ADVISOR_TIMEOUT` (default 600 s) applies per call.
- A missing login, no network, a usage limit, a timeout or a spent budget come back as
  `ADVISOR UNAVAILABLE: ...` and the session carries on; the exit code is unchanged.
- Every question and answer is appended to `advisor-<stamp>-<pid>.md` in `QWEN_OUTDIR` when you set it, else in `~/.cache/qwen-agent/advisor/` (never the tree under audit).
  `--json` adds `qwen_agent.advisor` = `{model, budget, calls, answered, seconds, cost_usd,
  unavailable, log}`.
- The system note tells the session to ask only about judgement calls: contradictory evidence,
  a finding it is unsure whether to keep, a design trade-off. Not to find bugs, and not about
  mechanical work (aligning docs, renaming, formatting).
- Files that look like secrets are never attached, even inside the working directory:
  `.env*`, `*.env`, `id_*`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, names with
  "credential" or "secret", key and certificate files (`.pem`, `.key`, `.p12`, `.pfx`, `.jks`,
  `.keystore`, `.kdbx`, `.asc`, `.gpg`), and anything under `.git`, `.ssh`, `.aws`, `.gnupg`,
  `.kube` or `.docker`. The session gets "looks like a secret, not attached".
- **Privacy:** questions and attached files leave this machine. The startup line says so
  even with `-q`. Only the `claude` login is used: no `ANTHROPIC_*` variable reaches the
  advisor, so Bedrock, Vertex and API-key-only setups get "unavailable".
- Not with `--interactive` (a person is there to ask).

## Desktop applications (`--desktop APP`)

`--desktop APP` lets a run drive one native application with real mouse and keyboard input:
open files through its dialogs, read its windows, save its output. It grew out of a field
trial on Windows (issue #2): Qwen opened a design in an optical-design program, ran two
analyses, transcribed them correctly and saved their text, in about 9 minutes against 4 to 6
for a Claude Haiku agent, at no per-token cost. One trial, one model.

The run gets Bash, granted for ONE command, `qla-desktop` (`lib/desktop.py`); qwen-agent's
default grants of read-only shell commands are dropped. qwen-agent writes the command as a
small wrapper into the run folder's `bin/`, made read-only (555) and put first on the
session's `PATH`; the wrapper fixes the application and the folder (`--lock`), so no call
can point it at another program. Only the folder's `shots/` subfolder -- where screenshots
and `desktop-state.json` land -- joins the directories the session is given: a `--write`
run can edit what it is given, and the command its Bash grant runs must not be among it.
Every call prints one JSON line with `ok`.

What the fence held in a live test (Claude Code, `-p`): writing a file by redirection,
`qla-desktop ... && touch FILE`, an env-variable prefix and `$(...)` were all denied, and
`--app` was refused by the driver. Claude Code itself still auto-allows its built-in
read-only commands (`id`, `head`, `ls` inside the working directories), so `qla-desktop
windows; id` ran: the fence is one command plus Claude Code's read-only set.

| Command | Does |
|---|---|
| `windows` | the application's windows: id, title, rect on screen |
| `shot NAME [WIN] [grid]` | screenshot of the window in front (a dialog when one is open), long side at most 1280 px |
| `crop NAME X Y W H [ZOOM] [grid]` | part of the window, enlarged up to 4 times: how small icons get hit |
| `click`, `dclick`, `rclick X Y` | clicks, in window pixels (0,0 is the window's corner) |
| `hover X Y`, `drag X1 Y1 X2 Y2`, `scroll X Y N` | the rest of the mouse |
| `type TEXT`, `key ctrl s` | text and key chords |
| `resize W H [X Y]`, `restore` | set the window size; `restore` puts back the size before the first resize |
| `wait SECONDS` | let a dialog or a file open |

`grid` draws labelled window coordinates over the image. The system note tells the session to
resize a large window to 1280x800 rather than maximize it (smaller screenshots, faster turns,
1:1 coordinates), to shoot before and after every action, to crop with a grid before clicking
a small target, to say "unreadable" rather than guess, and to restore the size at the end.
`windows` lists a window only when it is on screen: a minimised window cannot be driven, so
restore it first. On X11 with a window manager, and on macOS, popup and menu-bar menus are
not listed as windows: use keyboard shortcuts for menus there.

What the driver checks:
- The application must be running: `check` runs before claude starts, and a run with no
  window to drive exits 2.
- Before any input it brings the application to the front; when it cannot, nothing is sent.
- A click outside the window, or whose cursor read-back does not match, is not sent; on
  X11 and Windows a point covered by another application's window is refused too (macOS
  has no API for who owns a point, so there no such check is made).
- Chords that close, quit or destroy are refused: alt+F4, cmd+Q, cmd+W, ctrl+P and cmd+P
  (printing), on Linux ctrl+Q (quits GTK/Qt applications), on Windows and Linux
  shift+Delete (permanent delete in file managers), on macOS cmd+Backspace (move to Trash
  in Finder). ctrl+W is deliberately *not* refused: closing the application's own tabs is
  legitimate work. A click on a toolbar Print *button* cannot be told apart from any other
  click: in the trial the model hit one while looking for Save. Use `--desktop` only where
  a stray click is cheap.

Platforms: Linux on X11, tested live (an application under Xvfb included, also from a
Wayland user's shell); Windows (ctypes, physical pixels on mixed-DPI screens), not yet run in
this form; macOS (System Events and Quartz; allow the terminal under Privacy & Security,
Accessibility and Screen Recording), not yet run on a real Mac. XWayland, the X server of a
Wayland session, is refused: it lets no program read or click another's windows. Screenshots
need Pillow (`pip install pillow`; 9.2 or later on macOS).

- **Unsandboxed.** The input is real and goes to whatever the application does with it. Run it
  on a machine nobody is using: input sent while a person types goes to the wrong place.
- The fence fixes which program runs, not what it does inside the application.
- Screenshots and `desktop-state.json` stay in the run folder's `shots/`, under
  `QWEN_DESKTOP_DIR` (default `~/.cache/qwen-agent/desktop/`); the startup line prints it,
  even with `-q`. The wrapper sits in the `bin/` beside it, outside every folder the
  session is given.
- The toolset is the run's own (read-only, or `--write`) plus that Bash. `--toolset`,
  `--all-tools`, `--probe` (and the probe of the default depth, which brings a full shell),
  `--browser`/`-r tester`, `--test`, `--until-done` and `--interactive` are refused, and so
  are `--permission-mode`, `--mcp-config` and a `-t` list with an entry granting Bash.

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
| 7 | a tool call was blocked by the permission system (see `--warn-denials`). On a probe run, file-tool calls blocked only because they reached outside the sandbox are a note on stderr, not exit 7 |
| 8 | harness failure (claude or python missing, or unparseable output) |
| 9 | `--scenarios`: at least one scripted scenario ended FAIL or BLOCKED; `--replay`: at least one recorded script ended FAIL or ERROR, or the folder kept no script |
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
