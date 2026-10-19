# Swarm workflows: `qwen-swarm`

`qwen-swarm` runs a **workflow** on a swarm of Claude Code sessions on your local model.
A workflow states a purpose; the engine runs it the way `qwen-deep-research` always ran:
work dealt round-robin over at most `--max-agents` agents of at most `--max-items` items
each, every unit of a call running in one phase with at most `--seats` running at once,
one repair round for an unusable answer, retries for timeouts, every
answer cached in the run folder so `--resume` continues where a run stopped, and a hard
`--hours` deadline. The short version your session loads is `skill/local-swarm/SKILL.md`.

Set `--seats` (or `QWEN_SWARM_SEATS`) to your server's concurrent sequence limit (for
vLLM, `--max-num-seqs`); more seats than that only queue on the server.

Built-ins: `research` (this is `qwen-deep-research`; see [deep-research.md](deep-research.md)),
`debug`, `ui-test` (a scripted UI suite) and `optimize` (a faster test suite), each documented
under its own heading below. `qwen-swarm --list` prints them.

## A workflow folder

```
my-workflow/
  workflow.json     the manifest: what the user approves
  workflow.py       run(wf): the calls to make
  roles/*.md        one role text per role
  check.py          optional: fake answers for --check
```

### workflow.json

```json
{
  "name": "my-workflow",
  "description": "One line: what it produces",
  "goal": "what the goal argument is, e.g. the symptom to debug",
  "target": "none",
  "roles": {
    "worker": {"file": "roles/worker.md", "fence": "none"},
    "reader": {"file": "roles/reader.md", "fence": "web", "budget_weight": 2, "effort": "medium"}
  },
  "knobs": {"items": "int", "voters": "int", "label": "str"},
  "presets": {
    "quick":    {"items": 3, "voters": 1, "label": "", "budget": 240, "retries": 1, "rounds": 1},
    "standard": {"items": 5, "voters": 3, "label": "", "budget": 240, "retries": 1, "rounds": 2},
    "overnight": {"items": 12, "voters": 5, "label": "", "budget": 900, "retries": 3,
                  "rounds": "until", "hours": 8}
  },
  "default_depth": "quick"
}
```

| key | rule |
|---|---|
| `name` | `[a-z][a-z0-9-]*` |
| `description`, `goal` | non-empty strings; `goal` is quoted in the "no goal given" error |
| `target` | `none`, or `required`: the run needs `--target DIR`, an existing directory |
| `roles.R.file` | a file inside the folder (no `..`, no absolute path) |
| `roles.R.fence` | `none`, `browser`, `browser-probe`, `search`, `web`, `read` or `sandbox` (below); `read` and `sandbox` need `"target": "required"` |
| `roles.R.budget_weight` | number >= 1 (default 1): the per-item budget multiplier |
| `roles.R.effort` | optional default effort level; the level must be one the server accepts, and one that would turn thinking off (`off`, `none`, `0`, `disabled`, any case) is refused — no role may run without thinking. On a run, `--effort` and `--role-effort` override these role defaults. A role with no effort anywhere runs at qwen-agent's own default (`QWEN_EFFORT`): the engine refuses a `QWEN_EFFORT` in the environment that would turn thinking off, at start and on resume; a level set only in qwen-agent's config file stays qwen-agent's own to accept or refuse, and `QWEN_EFFORT_ALLOWED` restricts it there |
| `roles.R.deep` | optional, and **the default is depth**: no `deep` field (or `true`) gives the role both switches — on every fence but `none`, which never gets the review round: it has no tools, so `true` or absent gives it no switches at all and a list may name `"subagents"`/`"subagents_push"` only (naming `"review_round"` there is a manifest error) — `"deep": false` is the shallow opt-out, and on a fence with tools a list of `"review_round"` (qwen-agent `--review-round`), `"subagents"` (`--subagents-nudge`) and `"subagents_push"` (`--subagents-push`, the delegation mandate that replaces the nudge when a list names both) picks the list's own; nudging stays the default a `true` or absent field gives. `"probe"` is refused: a `sandbox` role already has a shell. `--deep ROLE` forces both on a tool-bearing role (on a fence-`none` one, only its subagent switch, with one note on stderr); `--shallow ROLE` forces none. The switches are part of each unit's cache key (a shallow unit's key is the one the released command computed), and a repair round never carries them |
| `knobs` | name -> `int` (>= 0), `float`, `str` or `bool`; a name may not shadow a config key (`depth`, `goal`, `budget`, ...); `--set` also takes the engine knobs `budget`, `retries`, `rounds` and `hours` without their being declared |
| `presets.D` | may name only declared knobs and the engine knobs, and must set every declared knob plus `budget` (int >= 1, seconds per item), `retries` (int >= 0) and `rounds` (int >= 1 or `"until"`); `hours` is optional (> 0; required with `"until"`) |
| `default_depth` | names a preset |

Unknown keys are rejected; every error names its field and exits 2.

### Fences

A role's fence is the only way a workflow chooses tools. Every agent also gets
`--permission-mode dontAsk` and `--warn-denials`.

| fence | tools | working directory | runs at |
|---|---|---|---|
| `none` | none | its own empty folder in the run | `--seats` |
| `browser` | a real browser: the Playwright tools qwen-agent `--browser` grants | its own empty folder | `--seats` |
| `browser-probe` | `browser` plus qwen-agent `--browser-eval`: `browser_evaluate` and `browser_network_request` | its own empty folder | `--seats` |
| `search` | the `search` MCP tool | its own empty folder | `--web-seats` |
| `web` | `search` + `page` + `WebFetch` | its own empty folder | `--web-seats` |
| `read` | Read, Glob, Grep | `--target` | `--seats` |
| `sandbox` | Read, Edit, Write, Bash, Glob, Grep | a fresh copy of `--target` per agent | `--seats` |

A `web` role gets the search MCP server's second tool as well: `page` fetches a known
url once and answers with its title plus up to 5 passages of about 600 characters around
each case-insensitive match of `find` (with no `find`, or no match, the first 3000
characters of the text) — plain text, **no model call**, where `WebFetch` runs one model
call per page. The run shares one copy of every page: each web unit is given
`QWEN_PAGE_CACHE=RUN/pages` in its environment — never in `mcp.json`, which is part of
every web unit's cache key and stays free of run-folder paths — the server stores the
fetched text there as `<sha256(url)>.txt`, and every call appends one tab-separated line
`time, sha256, hit|miss|error, status, bytes` to `RUN/pages/log.tsv`; a url fetched once
is read from the file for the rest of the run. It fetches http(s) text pages only,
refuses hosts that resolve to loopback, link-local, private (the carrier-shared /10
included), unspecified, reserved or multicast addresses — IPv4 embedded inside an IPv6
address checked too — (a local LLM server on 127.0.0.1 is not reachable through it),
follows at most 3 redirects while re-checking scheme and address on every hop, and caps
the body at the search tool's byte limit. Every hop resolves its host once and connects
only to the addresses that check saw, trying each in turn, so a name cannot answer
publicly to the check and loopback to the connect; proxy environment variables do not
reroute it; and 30 seconds cover one whole fetch — connect, headers and body together.
The DNS lookup itself is not inside that 30-second deadline, so a slow resolver can add
its own timeout on top. A refusal or HTTP error comes back as a short error text naming
`WebFetch` as the fallback — `WebFetch` stays the way to
read a page whole, and the way to fetch what `page` refuses. Errors are not cached. With
`QWEN_PAGE_CACHE` unset the tool writes nothing at all. `QWEN_PAGE_ALLOW_HOSTS` (a
comma-separated host list) turns those address refusals off for the hosts it names — it
exists for tests only (the page tests serve a local server on loopback), the server says
so on stderr at startup when it is set, and it must never be set for a real run: it opens
exactly the door the refusals shut.

A `browser` role is a `none` role plus qwen-agent `--browser`: qwen-agent writes the one
Playwright MCP server config itself and grants the `mcp__playwright__` tools itself, so
the unit carries no `--mcp-config` of the engine's (qwen-agent refuses the pair) and no
`--target` is asked for. Its toolset stays `none`; qwen-agent adds `Read` for its own
browser folder, so the session can look at the screenshots it took. Each unit is given
`QWEN_BROWSER_DIR=RUN/browser/<unit>`, under which qwen-agent makes a fresh timestamped
folder for every call it runs — a repair round or a retry gets one of its own — and writes
the screenshots and page snapshots there: `wf.browser_dir(unit)` names that root, so a
report can point at the evidence of each unit. It is NOT a web fence — a UI suite is driven
against local URLs — so it runs at `--seats` and asks for no search preflight, but the
browser is a network tool: a browser agent can navigate wherever it is told to. `--browser`
is refused with `--interactive` and `--until-done`, which a swarm unit never passes. A
browser unit's cache key carries the `--browser` flag: no other fence's key moved.

A `browser-probe` role is a `browser` role whose unit also passes `--browser-eval`
(`Unit.browser_eval`, part of its cache key, so a `browser` unit's key did not move), with the
same `QWEN_BROWSER_DIR`. It can run JavaScript in the page and read one request's response
body, which is to say the app's own scripts and server replies: give it to a role whose job
is checking, such as ui-test's local confirmer, not to a black-box tester.

A sandbox is an independent copy of the target, never a link into it. When the target is
the top of a git repository it is a `git clone --shared` of it, checked out detached at
HEAD, with the link to the target removed again (`git remote remove origin`, so a push
cannot reach the target) and every git hook switched off (an empty template,
`core.hooksPath` pointed at a path that does not exist, so no hook can fire). Sandboxes
are NOT git worktrees: a worktree shares the target's `.git`, and edits an agent makes
there (`git stash`, `git branch`) land in the target. A sandbox isolates an agent against
ACCIDENTS, not against a hostile agent — it has a shell, and a shell can `cd` elsewhere.
Bash in a sandbox is not a security boundary: it runs as you, with your network; use
sandbox workflows only on code you would run yourself. A `--shared` clone borrows the
target's objects, so do not `git gc`/`prune` the target while a run is going.
Uncommitted target changes are NOT in a sandbox (the run's summary says so when the target
is dirty); a target that is not the top of a git repository is copied whole without
`.git`, `node_modules`, `.venv`, `__pycache__` and the test/lint caches (`.pytest_cache`,
`.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`) and committed into a fresh repository, so
diffs work the same in both cases. Build artifacts an agent's own commands leave in a
sandbox (`__pycache__`, `.pytest_cache`, `node_modules`, `.venv`, ...) are excluded from
its patch. `--target` itself is never an agent's working directory unless the fence is
`read`. Sandboxes live in `RUN/sandboxes/` and are removed when the agent ends, unless
`--keep-sandboxes` — which also keeps the sandboxes on an interrupt, for inspection.

### workflow.py: the `wf` object

| call | does |
|---|---|
| `wf.goal` | the goal text |
| `wf.knob(name)` | a knob (preset, then `--set`); also `budget`, `retries`, `rounds`, `hours` |
| `wf.target` | the target directory (a `pathlib.Path`) or `None` |
| `wf.agent(name, role, prompt, parse)` | one agent, unit `name-1`; returns `parse(text)`, or `None` when it failed (then `wf.last_unit` has `ok`, `why`, `deadline`). Options: `cache=False`, `always=True` (runs past the deadline), `item="goal"` (the deadline log label), `stage=DIR` (below) |
| `wf.fan_out(name, role, items, prompt, parse)` | deals `items` in groups of <= `--max-agents` units holding <= `--max-items` items each and runs every unit of the call in ONE phase (`--seats` alone limits concurrency; the group number only names units); `prompt(batch)` -> str, `parse(text, batch)` -> list of rows. Returns `Result`: `.rows`, `.dropped_items`, `.not_run_items`, `.units`, `.ok`. Options: `item_id=`, `max_items=` (e.g. 1 = one item per agent), `stage=DIR` (a fresh copy of DIR in each unit's folder as `fixtures/`, made once per unit and kept for its retries and repair round; DIR's content hash joins the unit's cache key), `repair=TEXT` (this call's repair-round prompt; `{why}` is filled in), `unit_ids=True` (units named `name-<item id>` instead of by position, so skipping an item does not move another unit's cache key; needs `item_id=` and `max_items=1`) |
| `wf.claude_check(name, items, prompt, parse, model=M, max_calls=N)` | one `claude -p` per item through the user's claude login (below); returns one `{item, state, data, why}` per item, in order. Options: `budget_usd=2.0`, `browser=False`, `stage=DIR`, `read_dirs=()`, `item_id=`, `timeout=600`, `max_turns=40`. Refused (every item `unavailable`, no process) unless the config's `QWEN_CONFIRM` names a model or `--set confirm=claude` is typed |
| `wf.event(kind, **fields)` | one line in `RUN/events.jsonl` (below); an engine kind is a `ValueError` |
| `wf.vote(name, role, claims, voters, prompt, parse)` | claim-major vote slots — `(claim, k)` with `k` = 0..`voters`-1, dealt so no agent holds two slots of one claim (`voters` above `--max-agents` is an error). `prompt(batch)` and `parse(text, batch)` get the batch as that list of `(claim, k)` tuples; `parse` returns rows with `claim` (a claim id) and `verdict` (`supported`, `refuted`, `unclear`) — a row naming a claim this vote never asked about, or whose `verdict` is not a string, is ignored. `claim_id=fn` says how a claim is identified (default `c["id"]`); it also labels `run.log`'s deadline lines. Returns `{claim_id: verdict}` (majority of the votes requested) with `.cast`, `.requested`, `.result` |
| `wf.rounds()` | `for r in wf.rounds():` yields 1, 2, ... until the `rounds` knob, the deadline, or `wf.converged` |
| `wf.converged(reason)` | end the rounds loop after this round |
| `wf.round` | the current round (1 outside the loop) |
| `wf.steps.NAME` | `normalize_url`, `merge_urls`, `merge_claims`, `vote_slots`, `tally`, `extract_json`, `clip`, `slug`, and `run_cmd` (`merge_urls` and `merge_claims` expect PARSED rows, as the research parsers produce, not raw agent output) |
| `wf.steps.run_cmd(cmd, patch=None, timeout=600)` | `bash -c cmd` in a fresh sandbox of the target, `patch` applied first; returns `{applied, rc, timed_out, output_tail}`. A command longer than 4000 characters is run from a script file next to the sandbox instead of `-c`, because Windows truncates long command lines |
| `wf.save(key, data)` / `wf.load(key)` / `wf.exists(key)` / `wf.forget(*keys)` | JSON artifacts `<key>.json` in the run folder (`round-<r>/` from round 2), written to a temporary file and renamed into place, so a reader never sees half of one |
| `wf.write(relpath, text)` | a text file in the run folder, e.g. `patches/1.diff` |
| `wf.browser_dir(unit)` | the `RUN/browser/<unit>` folder a browser-fenced unit's session was given as its `QWEN_BROWSER_DIR`: qwen-agent's timestamped folders of screenshots and page snapshots are made inside it, so a report names this path as the unit's evidence |
| `wf.report(markdown)` | writes `report.md` (and `report-round-<r>.md` in a multi-round run) |
| `wf.totals()` | the run's cumulative `agents_run`, `tokens`, `seconds`, `invocations`, `claude_calls`, `claude_cost_usd` (for a Run table) |
| `wf.fail(message)` | stop: nothing usable, exit 5 |
| `wf.goal_unmet(reason)` | the run ends with exit 4 once its report is written |
| `wf.log(msg)` | a line in `run.log` |
| `wf.dropped`, `wf.not_run` | units dropped so far; items the deadline kept from starting |

Sandbox roles: `parse` takes a third argument, the agent's patch (`git diff` of its
sandbox, new files included), which is also saved to `RUN/agents/<unit>/patch.diff`.
A patch is a byte-exact string (its non-UTF-8 bytes ride along as lone surrogates,
`surrogateescape`): write it back with `wf.write`, which preserves those bytes, never
with a plain text-mode write.

A round in which any unit was dropped is not resumable from where it stopped, so
`research` ends its rounds loop then (`wf.converged`, and `stop_reason` reads
`converged: units dropped in round <r>`): no later round is built on ids that `--resume`
will renumber when it retries the dropped units.

Unit names: `name-<k>` for a call's first deal group, `name-w<n>-<k>` for its later groups
(the grouping only names units; all of them run in one phase), prefixed `r<r>-` from
round 2. A `parse` that raises `ValueError` makes the answer unusable: one repair round,
then the unit's retries.

Determinism: resume re-runs `run(wf)` from the start and every finished unit returns its
cached answer, so the script must make the same calls in the same order given the same
answers. Never read the clock, randomness, the environment or files outside `wf.load`.
An exception in `workflow.py` is exit 8 with the traceback in `RUN/error.log`; fix the
script and `--resume`: the cache key holds the role text, prompt, fence flags, effort and
depth switches, not the script.

A module-level `validate(cfg)` returning an error string refuses the run before it
starts (exit 2), e.g. when a knob needs more agents than `--max-agents`. A module-level
`notice(cfg)` may return one paragraph that the runner prints to stderr with the run-start
summary, on a fresh run and on every `--resume`: the place to say what the run will send off
this machine before it sends anything. `None` prints nothing.

### Asking Claude: `wf.claude_check`

```python
results = wf.claude_check(name, items, prompt, parse, *, model, max_calls, budget_usd=2.0,
                          browser=False, stage=None, read_dirs=(), item_id=None,
                          timeout=600, max_turns=40)
```

A workflow may ask for a Claude check of a list of items, but only the user's settings allow it: the call proceeds only when the config file's `QWEN_CONFIRM` names a model (or `--set confirm=claude` is typed, for a workflow with that knob); otherwise every item comes back `unavailable` with why "Claude is off for this run (set QWEN_CONFIRM in the config, or --set confirm=claude)" and no `claude` process starts. `--show-claude` and the run-start summary print `claude checks: off` or `claude checks: <model>` for every workflow. A workflow may have Claude check a list of items through the user's own `claude` login;
ui-test's confirm pass is built on it (the only swarm use of it today; it is the confirm setting, not plan or advisor). Each item is one `claude -p` run, one after another:
`prompt(item)` is the question and `parse(text, item)` reads the answer (raise `ValueError`
for an unusable one). The binary is `QWEN_CLAUDE_BIN` or `claude`, looked up on `PATH` for
every call (qwen-swarm exports `QWEN_CLAUDE_BIN` and `QWEN_PLAYWRIGHT_MCP` from the config
file). The environment is the caller's without any `ANTHROPIC_*`, `CLAUDE_CODE_*` or Bedrock
variable, so the call uses the login and never the local server. The flags are `--model M
--output-format json --max-turns T --max-budget-usd B --strict-mcp-config --setting-sources ""
--no-session-persistence --permission-mode dontAsk --restricted --tools Read`, plus one
`--add-dir` per `read_dirs` entry. `browser=True` adds the Playwright MCP server that
`lib/browser_mcp.py` builds (the one qwen-agent `--browser` writes) with every browser tool
except `browser_run_code_unsafe`, `browser_install` and `browser_network_request`:
`browser_evaluate` and the request list are allowed, because a scripted probe is the point of
a check. The call runs in `RUN/agents/<name>-<item id>/` with `stage` copied in as `fixtures/`;
Read reaches that folder and `read_dirs`, nothing else. On a timeout or an interrupt its
whole process group is killed, so no npx, Playwright or Chromium outlives it. Exit 126 or 127
(the binary was being replaced) is retried after 10 s and then 30 s
(`QWEN_EXEC_RETRY_BACKOFF`, default `10 30`; set but blank means no retry at all).

| state | meaning |
|---|---|
| `ok` | `data` is what `parse` returned |
| `failed` | this item only: max turns, timeout, `is_error`, the budget, or an answer `parse` refused; the next item is tried |
| `unavailable` | the breaker tripped: no binary, 126/127 after the retries, a login error, a connection error (offline, a dead proxy), or an unreadable reply to the first call; no later item is called |
| `over_cap` | the item needed a call beyond `max_calls` (cache hits do not count) |
| `deadline` | `--hours` had passed; no call |

Only `ok` answers are cached (artifact `claude-<name>`, keyed by the item id and a hash of the
prompt, model, browser flag and staged files), each saved as soon as its call returns: an
interrupt loses nothing, and a `--resume` after logging in, or with a larger cap, asks again
for every item that did not end `ok`. Keep timestamps and other run-varying text out of the
prompt. Every call is a line in `RUN/claude/calls.jsonl` (name, item, model, seconds,
cost_usd, num_turns, state, why), a line in `run.log` (role `claude-check`) and a `claude_call` event;
`wf.totals()` counts `claude_calls` and `claude_cost_usd`, and its wall time includes them.
What the prompt names and what the browser reaches leave this machine: say so in
`notice(cfg)`. Offline, the first failed call, a connection error included, trips the breaker
at once: one failed call, not one per item. Before that first call a cheap probe (~0.2 s, no
tokens: `claude auth status` plus one HTTPS HEAD) asks whether claude can be used at all. When
it says not because the login is gone or the API is unreachable -- every item comes back
`unavailable` with zero calls made, one `run.log` line and one `claude_probe` event (`name`,
`state`, `why`). When it says not because claude itself will not start, it makes no call
decision: that is the brief mid-update 126/127 the retry above rides out, so the call goes out
and decides, with no event. An undecided probe decides by the real call, exactly as before,
`QWEN_CLAUDE_PROBE=off` skips the probe and
`QWEN_CLAUDE_PROBE_URL` points its HEAD elsewhere
(see [configuration.md](configuration.md)).

### Session verdicts in a workflow

A workflow that gives the main session the final say on its rows defines three pure
module-level functions and saves an artifact `final` at the end of its run:
`read_verdicts(run_dir)` returns `{id: verdict}` from `RUN/verdicts/*.json`, skipping a file
whose `id` is not its file name or whose `verdict` is not `CONFIRMED`, `FALSE_ALARM` or
`NEEDS_HUMAN`; `apply_verdicts(final, verdicts)` returns `(rows, unmet)`; `render(final, rows)`
returns the report's markdown. None of them touches a Workflow or a Swarm, so the live run
and `--record-verdict` (below) render the same report from the same inputs.
`runner.exit_for(dropped, not_run, unmet)` is the one exit rule for both: 4 when any unit was
dropped, any item was not run or the goal is unmet, else 0. `--record-verdict` refuses a run
whose workflow has no `apply_verdicts` (exit 2); ui-test is the model.

### --check

`qwen-swarm --check WORKFLOW` validates the manifest and runs `run(wf)` twice against fake
agents (at the `quick` depth, or at the manifest's `default_depth` when it has no `quick`
preset, with `rounds` capped at 2; no agent starts, no command runs, nothing touches a
target), failing with exit 2 when the script raises or the two runs make different calls.
A `check.py` in the folder may define `answer(role, prompt) -> text`,
`run_cmd(cmd, patch) -> dict` and `patch(role, prompt) -> str` so the dry run reaches every
phase; without it each unit gets the first of `[]` and `{}` its parse accepts. A workflow that
cannot start without a knob the preset leaves empty (`ui-test`'s `scenarios` file) ends its dry
run at that `wf.fail`, so what `--check` proves there is its manifest and that `run(wf)` ends
cleanly, not its agents.

## Flags and env

```bash
qwen-swarm WORKFLOW "GOAL" [--depth NAME] [--set KNOB=VALUE]... [--target DIR]
           [--max-agents N] [--max-items N] [--seats N] [--web-seats N] [--timeout S]
           [--retries N] [--rounds N|until] [--hours H] [--effort LEVEL]
           [--role-effort ROLE=LEVEL[,ROLE=LEVEL...]] [--deep ROLE[,ROLE...]|all]
           [--shallow ROLE[,ROLE...]|all] [--out DIR] [--keep-sandboxes]
qwen-swarm WORKFLOW --stdin [...]
qwen-swarm --resume RUN_DIR [--seats N] [--web-seats N] [--timeout S] [--retries N]
           [--rounds N|until] [--hours H] [--effort LEVEL] [--role-effort ...] [--deep ...]
           [--shallow ...] [--keep-sandboxes]
qwen-swarm --check WORKFLOW | --preflight [WORKFLOW] | --list
qwen-swarm --record-verdict RUN --id ID --verdict CONFIRMED|FALSE_ALARM|NEEDS_HUMAN --evidence TEXT [--evidence TEXT ...]
```

A resume takes its goal and settings from the run folder and accepts the flags of the line
above; `qwen-deep-research --resume` accepts the same except `--deep` and `--shallow` —
which `qwen-deep-research` does not take at all — but its own resume message and the
synopsis on its page list only the seven the released `qwen-deep-research` had (without
`--rounds` and `--keep-sandboxes`, which came in with the engine), and `--keep-sandboxes`
keeps nothing in a research run — no research role has a `sandbox` fence
([deep-research.md](deep-research.md)).

A `WORKFLOW` argument that contains a `/` (a `\` on Windows) or starts with `.` is read as a
workflow folder, and one that has no `workflow.json` in it is a usage error; anything else
is a built-in name (`qwen-swarm --list`), so a folder must be given as `./my-workflow` or
`../my-workflow` or an absolute path, never as a bare name.

Precedence: a flag, then `--set`, then the environment (`QWEN_SWARM_MAX_AGENTS`,
`_MAX_ITEMS`, `_SEATS`, `_WEB_SEATS`, `_TIMEOUT`, `_RETRIES`, `_HOURS`, `_MAX_UNIT_SECONDS`,
`_BACKOFF`; the research workflow also reads the `QWEN_DR_*` names), then the preset.
`--timeout` is the per-item budget: an agent holding k items gets
max(300, weight x k x budget) seconds, one `wf.agent` counts as 2 items. `--hours` is a
hard deadline stored in `config.json`: no new uncached unit starts after it (a cached
unit is still served past the deadline and its rows count, as the first deal group's
always were), items not run are logged as `deadline`, synthesis-style `always=True`
agents still run, and no new round starts. `--rounds until` needs a deadline. Depth is
the default, so the two flags are
requests in opposite directions: `--deep ROLE[,ROLE...]|all` forces a review round and the
delegation nudge on those roles — a fence-`none` role has no tools, so it never gets the
review round: `--deep` gives it its subagent switch only and says so once on stderr —
(stored in `config.json` as `deep`) and `--shallow
ROLE[,ROLE...]|all` forces neither (stored as `shallow`, and it wins if a role ends up in
both lists); each is merged into the stored list by a resume, and the units it touches run
again under their new cache keys. Those stored lists only grow on `--resume`, so a role
once made shallow stays shallow for that run even if a later `--deep` names it (shallow
wins). Either way a unit is passed qwen-agent `--shallow`
first, so the unit's own switches are its whole depth and no agent ever gets the `--probe`
sandbox qwen-agent would otherwise imply for its own empty `agents/<unit>` folder. A review
round is two qwen-agent calls and qwen-agent gives each call the full `--timeout` it is
handed, so a deep unit is passed half its unit timeout (rounded down) to keep one unit's
timeout one unit's budget, but never less than 300 s (nor more than the unit's own
timeout); `qwen-deep-research` takes neither flag. Because the depth switches are part of
each unit's cache key, a `--resume` of a run started before depth became the default
re-runs the roles that are now deep.

What re-runs on a `--resume` follows from what the cache key holds: a unit's cache key
includes its role's depth switches, effort, tool grants and the MCP config. So (a)
changing `--deep`/`--shallow`/`--effort`/`--role-effort` on `--resume` re-runs that role
and every later unit whose prompt depends on its output; and (b) a run folder made by an
earlier version re-runs, on resume, the tool-less roles that used to get the review round
(research's scoper, planner when its plan is missing, and synthesizer; debug's planner
and writer), the roles whose default effort changed (research's searcher, reader and
verifier; optimize's checker and reviewer; debug's triager and reviewer; ui-test's
tester), and every web-fence unit (its grants now include the `page` tool).

### Claude calls in a swarm

Off on a fresh install. The config file's `QWEN_PLAN` / `QWEN_ADVISOR` are the defaults for
every role they apply to (advisor any role; plan only a role whose fence writes, today
debug's prober and optimize's implementer; rescue applies to no swarm role), and an exported
variable of the same name is ignored. `--claude ROLE:KEY=MODEL[,KEY=MODEL...]` (repeatable)
overrides them for one role in one run, `KEY` being `advisor` or `plan`, `MODEL` a model or
`off`: `--claude prober:advisor=off` leaves the prober local while every other role gets the
configured advisor. Neither a `workflow.json` nor a `workflow.py` can turn Claude on (see `wf.claude_check` below). `--show-claude` (also with
`--resume RUN`) prints each role's effective settings, `claude checks:`, `claude read tools (plan/advisor/rescue):` (the `claude checks:` pass always has Read) and ui-test's confirm, then exits
without launching anything. The ui-test confirm pass is its own setting, `QWEN_CONFIRM`
(below). Read tools (Read, Grep, Glob) are on for plan and advisor calls unless
`QWEN_CLAUDE_TOOLS=0`. A repair session (a resumed unit) gets `--advisor` but never `--plan`.
A swarm unit is a single `qwen-agent` run, so its Claude calls are logged as single runs are
(`advisor-<stamp>-<pid>.md` / `plan-<stamp>-<pid>.md` in `QWEN_OUTDIR` or
`~/.cache/qwen-agent/advisor/`), not in the run folder; only ui-test's confirm pass logs to
`RUN/claude/calls.jsonl`. What each call sends is as in [qwen-agent.md](qwen-agent.md#advisor---advisor-model-experimental).

## The run folder

Unless `--out DIR` names one, a run lands in `swarm/<workflow>/<UTC stamp>-<slug>` under the
current directory; the `research` workflow keeps its own root, `deep-research/...`.
The run folder must be OUTSIDE `--target`: a run writes its prompts, patches, logs and
sandboxes into the run folder, and one inside the target (or a target inside it) would put
them in the user's tree. The runner refuses either nesting before it creates anything
(exit 2, naming both paths), so pass `--out DIR` outside the target.

The folder holds `config.json` (the resolved settings and the manifest summary), the goal file
(`goal.md`; `question.md` for research), `run.log`, `totals.json` (cumulative; with
`stop_reason` in a multi-round run: `rounds`, `hours`, `deadline` or `converged: ...`),
`report.md`, `error.log` after an exit 8, `agents/<unit>.*` per agent, `browser/<unit>/`
(qwen-agent's own timestamped folders of screenshots and page snapshots, inside the evidence
root a `browser` unit was given) as soon as such a unit runs, and the workflow's own
`<key>.json` artifacts, `events.jsonl` (below), `.lock` while a runner holds the run,
`verdicts/<ID>.json` once a session verdict is recorded, `claude/calls.jsonl` once a
`wf.claude_check` call is made, and `agents/<unit>/fixtures/` for a unit given `stage=`.
`rounds.json` (the rounds started, so a resume finishes the
interrupted one), the `round-<r>/` directories (round 2 onwards; round 1 writes at the top
level) and `report-round-<r>.md` are a multi-round run's: they appear only when `rounds` is
above 1 (from `--rounds`, a resume's or a preset's) and the workflow works in a
`wf.rounds()` loop, and `report-round-<r>.md` only where the report is written inside that
loop (`research` reports per round, `debug` once after it). `cmds/<hash>.json` (cached
`run_cmd` results) appears as soon as a workflow runs a command, and, while agents run,
`sandboxes/`.
A one-round `research` run's folder is byte-identical to what the released
`qwen-deep-research` wrote on Linux and macOS (Windows writes LF), apart from
`events.jsonl`.

## Progress events: `RUN/events.jsonl`

A run is headless, and its event stream is how a session that launched it learns what
happened without reading `run.log`. Only the runner process writes it (its worker threads
included): one JSON object per line, each line written whole with a single `write()` and
flushed, string fields cut so a line stays under 4 KB. Every line has `kind` and `t` (unix
time). A reader skips a line that does not parse or has no newline yet: it is still being
written.

| kind | fields | when |
|---|---|---|
| `run_start` | `workflow`, `goal`, `run` (absolute path), `resumed`, `depth`, `knobs` | the run folder exists |
| `unit_done` | `unit`, `role`, `ok`, `cached`, `seconds`, `why` (on failure only) | every unit, cached ones included |
| `unit_dropped` | `unit`, `role`, `why` | a unit is dropped |
| `claude_call` | `name`, `item`, `state`, `seconds`, `cost_usd` | every `wf.claude_check` item |
| `claude_probe` | `name`, `state`, `why` | a `wf.claude_check` run's pre-call probe says claude is not available (below) |
| `run_end` | `exit`, `report` (path or `null`) | every exit once the folder exists: 0, 4, 5, 8, 130 |

Exits 2 and 3 come before any folder and write no event. A `--resume` appends to the same
file, its `run_start` with `resumed: true`. A runner that is killed outright writes no
`run_end`, so a watcher also watches the process. `run_end.exit` is the exit when the
runner finished: a session verdict that lands at the very end can change the process exit
afterwards, so gate on the process exit code (or the last `--record-verdict`'s), not on
`run_end.exit`. Right after the folder is made, stderr says
`qwen-swarm: run folder: <absolute path>` (`qwen-deep-research: run folder: ...` under that
command), the first line after the preflight lines, so a background launch knows where to
look; `skill/local-swarm/SKILL.md` step 3 is the watch
recipe.

`wf.event(kind, **fields)` adds a workflow's own kinds; an engine kind is refused with a
`ValueError`. By convention `attention` means "the main session should look at this", with
fields `item`, `reason`, `detail`. ui-test emits `scored` (`id`, `status`, `notes`) for every
row after the testers, `verdict` (`id`, `verdict`, `by`, `evidence`: the first evidence item)
whenever a confirmer's verdict is applied, and `attention` for a row the confirmer found
CONFIRMED (reason `confirmed failure`) or NEEDS_HUMAN (`needs human`) and for a FALSE_ALARM
on a tester FAIL (`false alarm on a tester FAIL`). `qwen-swarm --check` writes
the same `unit_done` events into its dry-run folder, which is deleted afterwards.

## The run lock

A runner holds `RUN/.lock` (`{"pid", "host", "started"}`, created with `O_CREAT|O_EXCL`) from
start to finish, on a `--resume` too. A lock is live when its host is this one and its pid is
alive, or when it names another host (which cannot be checked, so it counts as live).
`--resume` of a live run is exit 2 (`run is live (pid N)`, its message naming `RUN/.lock` as
the file to delete by hand when no runner is running), and of two resumes started at once only
one takes the lock. A lock whose pid is dead is stale: `--resume` and `--record-verdict`
remove it with one stderr line.

## Session verdicts: `--record-verdict`

```bash
qwen-swarm --record-verdict RUN --id ID --verdict CONFIRMED|FALSE_ALARM|NEEDS_HUMAN --evidence TEXT [--evidence TEXT ...]
```

The main Claude session has the final say on a non-PASS row of a workflow that supports it
(ui-test). The command is a top-level mode, like `--check`:

1. RUN must be a run folder whose workflow defines `apply_verdicts`, and ID a scenario whose
   tester status was FAIL or BLOCKED; anything else is exit 2 naming what it found (PASS and
   NOT RUN rows cannot be overridden).
2. It writes `RUN/verdicts/<ID>.json` = `{"id", "verdict", "evidence": [...], "by": "session",
   "t"}` atomically; a later call for the same id replaces it.
3. A live run: it stops there with exit 0 and `recorded; the running workflow will apply it`.
   The workflow reads the verdicts before it confirms each row (a row that has one is not
   sent to the confirmer) and again when it builds its final rows; one written after that is
   picked up by the runner itself once it has released the lock, so none is lost.
4. A run that is not live and never finished (exit 5, 8 or 130, or killed): exit 5, `run did
   not finish; verdict saved, applied on --resume`.
5. Otherwise it re-renders under `RUN/.render.lock` (waiting up to 30 s; a lock older than
   5 minutes is stale): `results.json` and `report.md` are rewritten from the saved `final`
   artifact and every verdict file, and `totals.json` is left alone. The exit code is the
   run's new one, through `exit_for`, with its reason printed (`exit 4: 1 agent(s) dropped
   (a verdict does not clear a drop)`). Calls made in parallel, one per subagent, take turns
   on the lock; gate on the exit code of the last one, not on `run_end.exit` in
   `events.jsonl` (written before this re-render).
6. An exit 8 means the verdict was **saved but not applied** (`RUN/.render.lock` or
   `.lock.clear` was held by someone else): run the same command again. The runner's own
   post-run re-render obeys the same rule -- when that lock is busy the run exits 4,
   never 0, and says to record the verdict again.

Neither `--record-verdict` nor the post-run re-render emits `verdict` events: the event
stream ends with `run_end`, so watch the process exit, not the events, for verdict effects.

Trust model: anything that can write the run folder can change its verdicts, just as it can
already edit `results.json`. The report shows every session verdict in a "Session verdicts"
section, and both opinions where they differ (`confirmer: CONFIRMED · session: FALSE_ALARM —
<evidence>`). A verdict file that does not validate is ignored, with a `run.log` line and a
note in the report.

## The debug workflow

`qwen-swarm debug "SYMPTOM" --target REPO --out RUN_DIR [--set repro="CMD"] [--set tests="CMD"]`

`RUN_DIR` is outside `REPO` — a run folder inside the target is refused (above).

| depth | hypotheses | voters | budget | retries | rounds |
|---|---|---|---|---|---|
| quick | 3 | 1 | 600 | 1 | 1 |
| standard | 5 | 3 | 900 | 1 | 2 |
| deep | 8 | 3 | 1200 | 2 | 3 |
| overnight | 12 | 5 | 1800 | 3 | until, 8 h |

The triager and the reviewer default to `medium` effort; on a run, `--effort` and
`--role-effort` override that.

triage (`read`; with no `--set repro=` the triager proposes the reproduction command, and
`run.log` records it as the triager's) -> reproduce (`run_cmd(repro)` in a fresh sandbox must
fail, else the report says it did not reproduce; with no `repro` at all — no knob, no
proposal — the report says so; either way the run exits 4) -> hypothesize (`read`) -> probe
(`sandbox`, one hypothesis per agent; a confirmed hypothesis leaves its fix as the agent's
patch) -> check fixes (each patch applied to a fresh sandbox; it passes when it applies,
`repro` exits 0 and `tests`, if set, exits 0; a patch touching a
`test`/`tests`/`spec`/`__tests__` directory, or a `test_*`, `*_test.*`, `*.spec.*`,
`*.test.*`, `conftest.py`, `FooTest.<ext>` or `FooTests.<ext>` file, is flagged and cannot
win — build artifacts a probe's own commands left behind never flag it) -> review
(`read`, a vote: root cause or symptom suppression; a patch longer than 20000 characters
reaches a reviewer clipped, and the report marks it too long to review) -> rounds (the
`none`-fenced planner turns the probes' verdicts and the patches that did not win into new
hypotheses) -> report (`none`; the writer is shown the winning patch).
Patches are ranked into `RUN/patches/<n>.diff`; exit 0 when one passed, was approved,
leaves the tests alone and was reviewed whole, exit 4 otherwise. The engine never writes
`--target`; you apply a patch with `git apply`.

## The optimize workflow

`qwen-swarm optimize "GOAL" --target REPO --out RUN_DIR --set bench=bench.json` makes a test
suite faster without changing what it checks, toward a target the calling session sets. The
bench file:

```json
{
  "measure": "python -m pytest -q tests/test_cli.py",
  "guard":   "python -m pytest -q",
  "focus":   ["tests/test_cli.py", "tests/conftest.py"],
  "target":  "-30%",
  "keep":    "no test removed or skipped; assertions unchanged"
}
```

| field | meaning |
|---|---|
| `"measure"` | required: the command timed on every candidate |
| `"guard"` | default `measure`: must still pass, with no fewer tests, on the final patch |
| `"focus"` | default the whole target: paths or globs a patch may change |
| `"target"` | optional, `-N%` or `Ns` on measure: reached = the rounds stop |
| `"keep"` | what reviewers judge each patch against |

Focus globs use `fnmatch`, where `*` also crosses `/`: `tests/*.py` matches `tests/a/b.py`.

`--set tests=CMD` without a bench times the whole suite with no focus and no target.
Any runner works: the wrapper times the command itself; pytest, unittest, jest, vitest,
`go test -v` and cargo test also give a pass count (a patch must keep it) and pytest and jest
a per-file profile (go a per-test one). Another runner is judged by its exit code alone, and the report
says "pass count not checked". A pass count adds up every test summary in the output, so a
chained command (`pytest tests/a && pytest tests/b`, `python -m unittest && pytest`) is
counted whole.

`measure` and `guard` run in a fresh sandbox: a clone of `--target`'s HEAD, the candidate
patch applied on top. Uncommitted changes are not in that clone, and neither are
git-ignored dependencies (`node_modules`, `.venv`): a bench command must find its tools on
`PATH` or install what it needs itself.

The checker and the reviewers default to `medium` effort; on a run, `--effort` and
`--role-effort` override that.

The run checks the bench before round 1 (measure passes twice, guard passes and runs at
least as many tests, the target is above the timing noise) and a checker agent may report
a problem, quoting the output. The "profile reaches focus" check is advisory: it is
reported, it never stops the run. A failed check stops the run:
exit 4, `report.md` starts with `bench: problem (see bench-review.md): <reason>`, and
`bench-review.md` holds the evidence. The local model never changes the bench; its
suggestion goes to `bench-suggested.json` and is not run. The same stop happens mid-run when
measure fails twice on the unchanged tree, when its timing drifts past 3x the noise twice,
or when a round's ideas all fall outside focus; approved patches so far stay in `patches/`.
The rounds end early when a round adds no new idea that passes its checks, or no speed-up
to the stack: the run then finishes and reports what it has.

Every claim a local agent makes is checked: an idea names a real `path:line` inside focus
and quotes a real profile line; speed is measured by the engine, never taken from the
agent; a reviewer's verdict counts only when the diff lines it cites are in the patch.
A test patch that weakens an assertion but keeps the pass count is caught only by the
reviewer vote (`quick` has one voter). Approved patches are stacked when the stack gets
faster by more than the noise; the guard then runs once on the stack, dropping patches
from the end until it passes.

Output: `patches/stacked.diff` (apply it yourself; `--target` is never written),
`patches/<n>-<id>.diff`, `bench-review.md`, `report.md`. Exit 0 when a stack was kept,
guard passed and the target (if any) was reached; exit 4 otherwise.

## The ui-test workflow

```bash
qwen-swarm ui-test "RUN NAME" --set scenarios=SUITE.md --out RUN_DIR [--set base=URL]
           [--set fixtures=DIR] [--set confirm=claude|local|none] [--set confirm_model=MODEL]
           [--set confirm_max=N]
```

| knob | default | meaning |
|---|---|---|
| `scenarios` | none: required | the suite file |
| `base` | the file's `base:` line | where the app under test listens |
| `fixtures` | the file's `fixtures:` line | the folder of files the scenarios upload |
| `confirm` | `local` | who re-checks each FAIL/BLOCKED: `local`, `none` or `claude`; anything else is exit 2. The config's `QWEN_CONFIRM` sets it; a workflow preset cannot make it `claude` |
| `confirm_model` | `opus` | `claude --model` for `confirm=claude` |
| `confirm_max` | `10` | Claude calls per run |

The suite file is the `scenarios` knob: the `# Suite:` markdown `qwen-agent --scenarios` takes
([qwen-agent.md](qwen-agent.md), "Scripted UI suites"), and the goal only names the run. The
same `lib/scenarios.py` validates the file, writes each tester's prompt and scores its answer
— what a tester was asked and what its answer is judged against are one description of one
job. The file is read and parsed before anything starts, as `qwen-agent --scenarios` does: no
knob, an unreadable file or a suite that does not parse is exit 2 naming its line, with no run
folder and no agent. The parsed suite is saved as `RUN/suite.json`, so a `--resume` replays the
same scenarios from the artifact rather than from the file. A relative `--set scenarios=PATH`
is resolved against the directory the command ran in and stored as an absolute path: the run's
`config.json` and `report.md` name the very file it read, whichever directory a `--resume`
happens to run from.

The app under test must already be listening at the suite's URLs: the workflow starts no
server. `--set base=URL` points the whole suite somewhere other than the file's `base:` line.

The manifest has one depth, `quick`: 900 s per scenario, one retry, one round. A suite's size
comes from its scenario count and from `--seats` (how many scenarios run at once —
`--max-agents` only shapes the deal), not from depth, and `--hours`
bounds a long suite. The `tester` role says `"deep": false` — a review round would be a second
browser session for the same scenario — which `--deep tester` overrides. The browsers run
headless: `--headed` is qwen-agent's own per-session flag (it implies `--browser` and needs a
display), and the swarm never passes it — watch one scenario with
`qwen-agent -r tester --headed "open http://localhost:3000 ..."`.

The tester defaults to `medium` effort; on a run, `--effort` and `--role-effort`
override that.

Every scenario is dealt to a `browser` agent of its own (`max_items=1`), so a suite of twenty
scenarios is twenty browser sessions at `--seats` rather than one long one, and a suite the
deadline stops names the scenarios that never got an agent.

`RUN/results.json` holds one entry per scenario, in file order (`id`, `status`,
`failed_expectations`, `evidence`, `notes`, and `unit`: which agent ran it, `null` if none
did); `report.md` holds the suite's own table with this workflow's fourth status added to
its counts (`PASS n / FAIL n / BLOCKED n / NOT RUN n`), the `RUN/browser/<unit>/` evidence
root of every unit that ran and, under each scenario, the evidence its tester named and
what did not hold — a PASS listed with what it rests on, not just its status. A scenario is
scored, not trusted: an answer with no json block is BLOCKED (`no result block`), and so is
a unit whose call failed or timed out — BLOCKED with the unit's own reason (`agent failed:
...`). A scenario whose unit the deadline kept from starting is `NOT RUN` instead, with
`unit` null and the note `deadline: not started; --resume runs it`: that unit made no
session and no browser folder, so the report names none for it, and the counts hold these
apart from the BLOCKED ones. No scenario is ever left out of the report.

**A missing result block.** A tester that ends without a usable result block (no json block,
or one without its scenario id) is resumed once with: "Your last answer had no usable result
block ({why}). The browser has been restarted. If you did not finish every step, run the
scenario again from the start, then end with the one json block. If you did finish, reply
with only the block." A fresh browser per call cannot be avoided. If that repair and the
unit's retry fail as well, the unit is dropped (the run exits 4) and its row is BLOCKED with
the note `no result block after repair (<reason>)`; the row still goes to the confirm pass.

**Fixtures.** A scenario that uploads a file needs the file inside the browser's reach:
Playwright MCP accepts an upload only from its output folder or its working directory. The
suite names a folder with a header line `fixtures: DIR` (between `# Suite:` and the first
scenario, like `base:`), relative to the suite file; `--set fixtures=DIR` overrides it,
relative to the directory the command ran in, and is stored absolute. The folder must exist
and hold at most 200 MB (the size walk does not follow linked folders), else exit 2; a symlink
in it whose target points outside the folder is refused too (exit 2 naming the link), so what
a unit may stage, upload and be told about stays inside the folder. The folder is taken as
constant for the duration of a run. Every
tester gets its own copy in `RUN/agents/<unit>/fixtures/` before its first call, kept for its
retries and its repair round, and its prompt lists them under "Files for uploads:" as `- <name> at <absolute path>`
(`C:\...` on Windows), ending "Pass that absolute path to browser_file_upload."; steps name a
fixture by file name.
The folder's file list and content hash are saved in `suite.json`: a `--resume` whose
fixtures folder is gone or changed is exit 5 (`fixtures dir changed or missing: ...`), and a
changed file gives every tester a new cache key. Two scenario ids that differ only by case are
refused (exit 2), because verdict files are named by id.
Keep fixtures non-secret: the confirmer can read the fixtures and every tester's evidence,
and a hostile page under test could prompt-inject a tester into posting what it can read.

**The confirm pass.** A tester's FAIL is not trusted on its own word. After the testers, every
FAIL or BLOCKED row, in suite order, goes to a confirmer with the scenario, the base URL, the
fixture paths and its browser folder; the tester's whole result block -- status, notes,
failed_expectations, evidence and final answer -- rides inside an untrusted-data delimiter
that holds a per-run random token (a fixed token under --check, so the two dry runs ask the
same question) in both marker lines, so a hostile page that prompt-injects
the tester cannot forge the closing marker and pose as the rest of the prompt. The confirmer re-checks the
expectations the tester said failed (the whole scenario when it was blocked), prefers a direct
probe (`browser_evaluate`, the console or the request list) to looking, reads each
expectation's exact words, and answers one json block `{"id", "verdict", "evidence": [...]}`.

- `confirm=claude` (only from `QWEN_CONFIRM` or a typed `--set confirm=claude`): `wf.claude_check` with `confirm_model`, a browser, the
  fixtures staged, the tester's browser folder readable, at most `confirm_max` calls.
- `confirm=local` (the default): role `confirmer` on the `browser-probe` fence, one unit per row named
  `confirm-<id>`, with the usual cache and resume and no cap.
- `confirm=none`: no pass; `results.json` holds what it held before the pass existed, and
  while any non-PASS row has no final verdict "What did not pass" says "No confirm pass
  ran": check each FAIL by hand or with a scripted probe before treating it as a regression.

Each checked row gains `confirmation` = `{verdict, evidence, by, notes}` (`by` is
`claude:<model>` or `local`) and `final` = `{verdict, by}`: the session's verdict when one was
recorded, else the confirmer's. `status` keeps the tester's word. CONFIRMED means the failure
is real; FALSE_ALARM, that the expectation holds or the step can be done; NEEDS_HUMAN, that no
answer came: the confirmer was unavailable (no login, or offline), over its cap, past the
deadline, or failed on that row, and the notes say which (`confirmer unavailable: ...`).
A `local` FALSE_ALARM is **advisory**: it is reported (and `run.log` names it
`advisory, still counts`), but the row keeps counting against the exit and against the Run
table's `false alarms` -- only a Claude or a session FALSE_ALARM clears a row. The
`claude unavailable` breaker trips only on the claude CLI's own login/network failures:
offline, the first failed call (a connection error included) trips it and every remaining
row is NEEDS_HUMAN without a call. Words from the page under test are not claude failures:
a prompt-injected or junk answer fails only that row. A row that already has a session
verdict is not sent to the confirmer at all.

**What leaves the machine.** Nothing, by default: `confirm=local` keeps the check on the local
model. With `confirm=claude` (the config file's `QWEN_CONFIRM`, or a typed `--set confirm=claude`),
each FAIL/BLOCKED scenario's text, the tester's report and screenshots, and
a browser session on the app's base URL go to Claude (`confirm_model`) through your claude
login. The run says so on stderr when it starts, fresh or resumed (a line beginning
`confirm=claude:`). For a private app, or to
stay offline, leave it at `local`, or `--set confirm=none` to skip it. A `--resume` of a run
made before these knobs existed fills each from the preset and names it on stderr.

`results.json` rows that were checked carry `confirmation` and `final` too. `report.md` gains
a `confirmed` column, a "## False alarms" section (the tester said FAIL or BLOCKED, the
confirmer showed it works, with its evidence), "What did not pass" listing only the rows that
still count, each with its verdict, a "Session verdicts" section (every session verdict, plus
each ignored verdict file with the reason it was ignored), and Run rows `confirmed`,
`false alarms` (counting only the rows that no longer count -- a local advisory FALSE_ALARM
is not one), `needs human` and, with `claude`, `claude calls` and `claude cost`.

Exit 0 when every row passed or ended with the final verdict FALSE_ALARM, no tester was
dropped and none was left NOT RUN; 4 otherwise. A dropped tester's row is still confirmed and
reported, but the run exits 4 whatever its verdict: it was not fully run. A suite the
deadline only stopped — every unfinished scenario NOT RUN — still ends with its goal unmet,
naming the count, and `--resume RUN_DIR --hours H` runs those scenarios. The session's final
say, during the run or after it, is `--record-verdict` (above).
