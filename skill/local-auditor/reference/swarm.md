# Swarm workflows: `qwen-swarm`

`qwen-swarm` runs a **workflow** on a swarm of Claude Code sessions on your local model.
A workflow states a purpose; the engine runs it the way `qwen-deep-research` always ran:
work dealt round-robin over at most `--max-agents` agents per phase, at most `--seats`
running at once, one repair round for an unusable answer, retries for timeouts, every
answer cached in the run folder so `--resume` continues where a run stopped, and a hard
`--hours` deadline. The short version your session loads is `skill/local-swarm/SKILL.md`.

Built-ins: `research` (this is `qwen-deep-research`; see [deep-research.md](deep-research.md))
and `debug`. `qwen-swarm --list` prints them.

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
    "reader": {"file": "roles/reader.md", "fence": "web", "budget_weight": 2, "effort": "high"}
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
| `roles.R.fence` | `none`, `search`, `web`, `read` or `sandbox` (below); `read` and `sandbox` need `"target": "required"` |
| `roles.R.budget_weight` | number >= 1 (default 1): the per-item budget multiplier |
| `roles.R.effort` | optional default effort; `--effort` and `--role-effort` beat it |
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
| `search` | the `search` MCP tool | its own empty folder | `--web-seats` |
| `web` | `search` + `WebFetch` | its own empty folder | `--web-seats` |
| `read` | Read, Glob, Grep | `--target` | `--seats` |
| `sandbox` | Read, Edit, Write, Bash, Glob, Grep | a fresh copy of `--target` per agent | `--seats` |

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
| `wf.agent(name, role, prompt, parse)` | one agent, unit `name-1`; returns `parse(text)`, or `None` when it failed (then `wf.last_unit` has `ok`, `why`, `deadline`). Options: `cache=False`, `always=True` (runs past the deadline), `item="goal"` (the deadline log label) |
| `wf.fan_out(name, role, items, prompt, parse)` | deals `items` over <= `--max-agents` agents in waves of `--max-agents` x `--max-items`; `prompt(batch)` -> str, `parse(text, batch)` -> list of rows. Returns `Result`: `.rows`, `.dropped_items`, `.not_run_items`, `.units`, `.ok`. Options: `item_id=`, `max_items=` (e.g. 1 = one item per agent) |
| `wf.vote(name, role, claims, voters, prompt, parse)` | claim-major vote slots — `(claim, k)` with `k` = 0..`voters`-1, dealt so no agent holds two slots of one claim (`voters` above `--max-agents` is an error). `prompt(batch)` and `parse(text, batch)` get the batch as that list of `(claim, k)` tuples; `parse` returns rows with `claim` (a claim id) and `verdict` (`supported`, `refuted`, `unclear`) — a row naming a claim this vote never asked about, or whose `verdict` is not a string, is ignored. `claim_id=fn` says how a claim is identified (default `c["id"]`); it also labels `run.log`'s deadline lines. Returns `{claim_id: verdict}` (majority of the votes requested) with `.cast`, `.requested`, `.result` |
| `wf.rounds()` | `for r in wf.rounds():` yields 1, 2, ... until the `rounds` knob, the deadline, or `wf.converged` |
| `wf.converged(reason)` | end the rounds loop after this round |
| `wf.round` | the current round (1 outside the loop) |
| `wf.steps.NAME` | `normalize_url`, `merge_urls`, `merge_claims`, `vote_slots`, `tally`, `extract_json`, `clip`, `slug`, and `run_cmd` (`merge_urls` and `merge_claims` expect PARSED rows, as the research parsers produce, not raw agent output) |
| `wf.steps.run_cmd(cmd, patch=None, timeout=600)` | `bash -c cmd` in a fresh sandbox of the target, `patch` applied first; returns `{applied, rc, timed_out, output_tail}` |
| `wf.save(key, data)` / `wf.load(key)` / `wf.exists(key)` / `wf.forget(*keys)` | JSON artifacts `<key>.json` in the run folder (`round-<r>/` from round 2) |
| `wf.write(relpath, text)` | a text file in the run folder, e.g. `patches/1.diff` |
| `wf.report(markdown)` | writes `report.md` (and `report-round-<r>.md` in a multi-round run) |
| `wf.totals()` | the run's cumulative `agents_run`, `tokens`, `seconds`, `invocations` (for a Run table) |
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

Unit names: `name-<k>` in wave 1, `name-w<n>-<k>` in later waves, prefixed `r<r>-` from
round 2. A `parse` that raises `ValueError` makes the answer unusable: one repair round,
then the unit's retries.

Determinism: resume re-runs `run(wf)` from the start and every finished unit returns its
cached answer, so the script must make the same calls in the same order given the same
answers. Never read the clock, randomness, the environment or files outside `wf.load`.
An exception in `workflow.py` is exit 8 with the traceback in `RUN/error.log`; fix the
script and `--resume`: the cache key holds the role text, prompt, fence flags and effort,
not the script.

A module-level `validate(cfg)` returning an error string refuses the run before it
starts (exit 2), e.g. when a knob needs more agents than `--max-agents`.

### --check

`qwen-swarm --check WORKFLOW` validates the manifest and runs `run(wf)` twice against fake
agents (at the `quick` depth, or at the manifest's `default_depth` when it has no `quick`
preset, with `rounds` capped at 2; no agent starts, no command runs, nothing touches a
target), failing with exit 2 when the script raises or the two runs make different calls.
A `check.py` in the folder may define `answer(role, prompt) -> text`,
`run_cmd(cmd, patch) -> dict` and `patch(role, prompt) -> str` so the dry run reaches every
phase; without it each unit gets the first of `[]` and `{}` its parse accepts.

## Flags and env

```bash
qwen-swarm WORKFLOW "GOAL" [--depth NAME] [--set KNOB=VALUE]... [--target DIR]
           [--max-agents N] [--max-items N] [--seats N] [--web-seats N] [--timeout S]
           [--retries N] [--rounds N|until] [--hours H] [--effort LEVEL]
           [--role-effort ROLE=LEVEL[,ROLE=LEVEL...]] [--out DIR] [--keep-sandboxes]
qwen-swarm WORKFLOW --stdin [...]
qwen-swarm --resume RUN_DIR [--seats N] [--web-seats N] [--timeout S] [--retries N]
           [--rounds N|until] [--hours H] [--effort LEVEL] [--role-effort ...] [--keep-sandboxes]
qwen-swarm --check WORKFLOW | --preflight [WORKFLOW] | --list
```

A resume takes its goal and settings from the run folder and accepts the flags of the line
above; `qwen-deep-research --resume` accepts the same, but its own resume message and the
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
hard deadline stored in `config.json`: no new unit starts after it, items not run are
logged as `deadline`, synthesis-style `always=True` agents still run, and no new round
starts. `--rounds until` needs a deadline.

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
`report.md`, `error.log` after an exit 8, `agents/<unit>.*` per agent, and the workflow's
own `<key>.json` artifacts. `rounds.json` (the rounds started, so a resume finishes the
interrupted one), the `round-<r>/` directories (round 2 onwards; round 1 writes at the top
level) and `report-round-<r>.md` are a multi-round run's: they appear only when `rounds` is
above 1 (from `--rounds`, a resume's or a preset's) and the workflow works in a
`wf.rounds()` loop, and `report-round-<r>.md` only where the report is written inside that
loop (`research` reports per round, `debug` once after it). `cmds/<hash>.json` (cached
`run_cmd` results) appears as soon as a workflow runs a command, and, while agents run,
`sandboxes/`.
A one-round `research` run's folder is byte-identical to what the released
`qwen-deep-research` wrote on Linux and macOS (Windows writes LF).

## The debug workflow

`qwen-swarm debug "SYMPTOM" --target REPO --out RUN_DIR [--set repro="CMD"] [--set tests="CMD"]`

`RUN_DIR` is outside `REPO` — a run folder inside the target is refused (above).

| depth | hypotheses | voters | budget | retries | rounds |
|---|---|---|---|---|---|
| quick | 3 | 1 | 600 | 1 | 1 |
| standard | 5 | 3 | 900 | 1 | 2 |
| deep | 8 | 3 | 1200 | 2 | 3 |
| overnight | 12 | 5 | 1800 | 3 | until, 8 h |

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
