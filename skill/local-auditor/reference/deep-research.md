# Deep research: `qwen-deep-research`

The one job in this toolkit that goes online on purpose: a swarm of Claude Code sessions on
your local model researches one question on the web and writes a cited, vote-verified
report. Every unit of work is a real session with real tools; the harness only sequences the
phases, splits the work, caps how many agents run at once, and does the mechanical steps in
between. The short version your session loads is `skill/local-deep-research/SKILL.md`.

`qwen-deep-research` is the built-in `research` workflow of `qwen-swarm`
([swarm.md](swarm.md)) under its old name: `qwen-swarm research "QUESTION"` runs the same
pipeline, and every flag, `QWEN_DR_*` variable, exit code and run-folder file below is
unchanged.

## What it does

Each phase's agents are named for the phase — scoper, searcher, reader, verifier,
planner (from round 2), synthesizer:

| phase | role | work per agent | tools |
|---|---|---|---|
| scope | scoper | the whole question, split into search angles (1 agent) | none |
| search | searcher | one angle: its queries through `search`, the most useful sources picked | `search` |
| fetch | reader | one source: fetched, its falsifiable claims pulled out with snippets | `WebFetch`, `search` |
| verify | verifier | its votes, adversarially: break the claim, then check it again | `WebFetch`, `search` |
| plan (round 2+) | planner | the claims so far and the last report's Gaps, into new angles and claims to re-check (1 agent) | none |
| synthesize | synthesizer | the voted claims, into the report (1 agent) | none |

Every agent is a Claude Code session on the local model, run through `qwen-agent`.

Depth (a review round of its own answer, and the nudge to delegate) is the default for a
role, so the three single-agent roles — scoper, planner, synthesizer — get it. The three
high fan-out roles do not: search, fetch and verify mark themselves `"deep": false` in the
manifest, because a review round is a second call and these phases already run dozens of
agents. `qwen-deep-research` has no flag for any of it (that is what its manifest says);
`qwen-swarm research` takes `--deep ROLE` and `--shallow ROLE` to steer it from outside.
Every unit is dispatched with `--shallow` first, so no agent gets the `--probe` sandbox
qwen-agent would otherwise imply for the empty folder it runs in.

Between phases the harness does only mechanical work, deliberately: those are the steps an LLM
loses track of. URLs are normalised (scheme and host lowercased, fragment and the `utm_*`,
`fbclid`, `gclid` and `ref` params dropped), deduped, ranked by best relevance then by how
many angles found them, and cut to the source cap. Claims with no snippet and exact-text
duplicates are dropped, ranked by importance and cut to the claim cap. Votes stay
independent: no agent is ever dealt two votes on the same claim.

A claim is **refuted** when a majority of its voters refute it, **supported** when a majority
support it, and **unclear** otherwise. A vote that was never cast counts as unclear, never as
agreement, so a dropped verifier costs confidence instead of quietly helping.

### Depth presets

| `--depth` | angles | sources | claims | voters | per-item budget | retries | rounds |
|---|---|---|---|---|---|---|---|
| `quick` | 3 | 6 | 10 | 1 | 240 s | 1 | 1 |
| `standard` (default) | 5 | 15 | 25 | 3 | 240 s | 1 | 1 |
| `deep` | 8 | 30 | 50 | 3 | 600 s | 2 | 2 |
| `overnight` | 10 | 40 | 80 | 5 | 900 s | 3 | until the 8 h deadline |

With one voter, a single `refuted` vote kills the claim — so `quick` is a look-around, not a
verdict. Use `standard` before you repeat anything the report says. Deeper presets are meant
for long unattended runs: locally, time is cheap, so they trade wall time for completeness.

### Rounds (deep, overnight)

Round 1 is the pipeline above. From round 2 a planner reads the question, the claims so
far by verdict and the last report's Gaps section, and returns new angles (each with a
reason; an angle or query repeating an earlier one is dropped) plus unclear claims worth
fresh votes. The round searches only the new angles (at most the `angles` cap — the
preset's value, or `--set angles=N` on `qwen-swarm`), skips URLs seen in earlier rounds,
drops claims that repeat an earlier claim, and verifies the new claims plus the re-checks:
votes accumulate, and a claim's verdict is the majority of all votes requested for it.
Synthesis runs after every round. The run stops when the rounds are done, the planner
finds nothing new, a round adds no supported claim, or the deadline passes before the next
round, and `totals.json` says which in `stop_reason`: `rounds` (the round count is done),
`hours` (the `--hours` deadline passed, everything queued ran), `deadline` (the deadline
passed and left items unrun) or `converged: <reason>`, where research's reasons are
`units dropped in round <r>: --resume retries them`, `round <r> added no supported claim`,
`the planner produced no plan` and `the planner found no new angle`.
`--rounds N` (or `until`, which needs `--hours`) overrides the preset; a resume may change
it too (the flag came in with the engine, so this command's own resume message does not
name it — see the resume section below).

## Setup: a search backend

Claude Code's own `WebSearch` is a server-side tool your server rejects, so the workers
search through one MCP tool, `search`, which has two possible backends. Either goes in
`~/.config/qwen-agent/config`.

**SearXNG (self-hosted, no key).**

```bash
docker run -d --name searxng -p 127.0.0.1:8888:8080 -v "$HOME/searxng:/etc/searxng" searxng/searxng
```

The mount keeps your settings across restarts. On first start SearXNG generates
`$HOME/searxng/settings.yml` itself — it already carries `use_default_settings: true` and a
`server:` key (holding `secret_key`), and has no `search:` key — and the generated files
belong to the container's user, so editing them may need `sudo` (or
`docker exec searxng sh -c '...'`). SearXNG answers JSON only if the format is allowed, so
make two edits to that file. First add `limiter: false` under the existing `server:` key —
not a second `server:` block, which is a duplicate key, not a merge:

```yaml
server:
  limiter: false    # a private instance: the limiter throttles repeated queries
```

Then append a new top-level block:

```yaml
search:
  formats:
    - html
    - json        # without json in the list, every search comes back 403
```

`docker restart searxng`, then set:

```bash
QWEN_SEARCH_URL=http://127.0.0.1:8888
```

If the restart comes back wrong, `docker logs searxng` shows the start-up errors — a
settings.yml that fails to parse is why an instance never answers.

**Brave.** `QWEN_SEARCH_KEY=<brave key>` in the same file. When both are configured,
`QWEN_SEARCH_BACKEND=searxng|brave` picks one; without it SearXNG wins. The key is read from
the environment only and is never written to the run folder — `mcp.json` holds the non-secret
part of the backend config, and the key reaches the search server by environment inheritance,
so it appears in no prompt, log or report.

Either way, check before a long run:

```bash
qwen-deep-research --check      # "ok: model and search reachable", or it names which one failed
```

## Flags and env

```bash
qwen-deep-research "QUESTION" [--depth quick|standard|deep|overnight] [--max-agents N]
                   [--max-items N] [--seats N]
                   [--web-seats N] [--timeout S] [--retries N] [--rounds N|until] [--hours H]
                   [--effort LEVEL] [--role-effort ROLE=LEVEL[,ROLE=LEVEL...]] [--out DIR]
qwen-deep-research --stdin                                     # the question from stdin
qwen-deep-research --resume RUN_DIR [--seats N] [--web-seats N]
                   [--timeout S] [--retries N] [--hours H]
                   [--effort LEVEL] [--role-effort ROLE=LEVEL[,ROLE=LEVEL...]]
                                                                # continue an interrupted run
qwen-deep-research --check                                     # preflight only, writes nothing
```

| flag | env | default | meaning |
|---|---|---|---|
| `--depth` | | `standard` | the preset above |
| `--max-agents N` | `QWEN_DR_MAX_AGENTS` | 8 | most agents one phase starts; its items are dealt among them round-robin, never cut to fit. Must be at least the voters per claim (5 for `overnight`), or the run refuses at start |
| `--max-items N` | `QWEN_DR_MAX_ITEMS` | 10 | most items one agent holds (angles, sources or claim votes); N >= 1. A phase with more items than `--max-agents` x `--max-items` splits them in order into waves of that many items, each wave dealt over `--max-agents` agents and run before the next wave starts; wave 1 keeps the unit names (`verify-3`), later waves carry the wave number (`verify-w2-3`) |
| `--seats N` | `QWEN_DR_SEATS` | 4 | agents running at once; keep it at or below the server's concurrent-request capacity. More seats than slots buys queueing, not speed. Scope and synthesis use these |
| `--web-seats N` | `QWEN_DR_WEB_SEATS` | `--seats` | search, fetch and verify agents running at once; must be between 1 and `--seats`. Web agents are told to call one tool at a time, so the web phases default to the full `--seats`; lower `--web-seats` if the server shows requests waiting during the search, fetch and verify phases |
| `--timeout S` | `QWEN_DR_TIMEOUT` | the depth preset's budget | **per-item** budget, not per-agent: an agent holding k items (angles, sources or claim votes) gets max(300, k x timeout) seconds; readers get twice the per-item budget, since each source is a whole page — a reader holding k sources gets max(300, 2 x k x timeout); scope and synthesis get max(300, 2 x timeout). An overrun drops that agent's items — until its retries run out (below). No unit's `--timeout` ever exceeds `MAX_UNIT_SECONDS` (`QWEN_DR_MAX_UNIT_SECONDS`, default 14400 s = 4 h), retry doublings included |
| `--retries N` | `QWEN_DR_RETRIES` | the depth preset's | a unit is retried only when its last failure was qwen-agent exit 5 (a timeout), exit 3 or 4 after the backoff wait before re-spawning it (`QWEN_DR_BACKOFF` — seconds before re-spawning an agent after a server error (exit 3 or 4); 30), an empty result (exit 6, including on the repair call), or an unusable answer (after its repair round, or with no session to repair); any other exit code (1, 2, 7, 8, a negative/signal exit) drops the unit at once. Each try gets double the previous timeout (up to the 4 h cap); the repair call keeps its attempt's timeout, and only the final failure counts as dropped. N >= 0. A unit stopped by a stopping swarm, and a cached finished unit, are never retried. Every spawn of a re-spawned unit counts its tokens, so `run.log`'s token column and the report's token total cover all of them |
| `--hours H` | `QWEN_DR_HOURS` | 8 for `overnight`, none otherwise | hard stop for the whole run, H > 0: a deadline H hours from the first start, stored in `config.json` as an absolute UTC time (and in `hours`). Once it passes, no new wave and no new unit starts — running units finish, queued units of the current wave are not started — every item not run gets a `deadline` line in `run.log` (not a drop), the phase continues with what it has and its phase file is **not** written, so a later `--resume` finishes the work; synthesis always runs. The report's Run table gains a "stopped at deadline" row, stderr names the count, and the exit code is 4 when anything was not run. Each agent is capped at 4 h; --hours bounds the whole run. On `--resume` the stored deadline stands unless `--hours` is given again, which sets a new deadline from then |
| `--rounds N\|until` | | the depth preset's | rounds of research (above); `until` runs until the deadline or convergence and needs `--hours` |
| `--effort LEVEL` | | qwen-agent's own | reasoning effort for every role, passed to qwen-agent as `-e LEVEL`; qwen-agent validates the level |
| `--role-effort ROLE=LEVEL[,ROLE=LEVEL...]` | | none | effort for single roles: `ROLE=LEVEL[,ROLE=LEVEL...]` with ROLE one of scoper, searcher, reader, verifier, planner, synthesizer; beats `--effort`. An unknown role, a pair without `=` or an empty level is a usage error (exit 2). Stored in `config.json` (reused on resume unless given again, when it merges into the stored dict — later wins) and part of each agent's cache key |
| `--out DIR` | | `./deep-research/<UTC timestamp>-<slug>` | the run folder |

Flags beat the environment. `--check` is the same preflight every run starts with: the model
server is reachable and serves the model, and one real `search` succeeds (no model answer is
asked for). A `QWEN_DR_MAX_UNIT_SECONDS` below 1 or not a whole number is a usage error
(exit 2) that names it — no run starts on a cap it cannot honour.

## The run folder

`question.md`, `config.json`, `mcp.json`, `angles.json`, `urls.json`, `claims.json`,
`fetch_stats.json`, `votes.json`, `totals.json`, `report.md`, `run.log`, and `error.log`
(the traceback of an exit-8 internal error — no error, no file), and per agent
`agents/<phase>-<n>.prompt.md`, `.out` and `.json` beside an empty `agents/<phase>-<n>/`
directory that agent runs in, plus `agents/<name>.repair.md` and `agents/<name>.repair.out`
whenever a repair round ran. `run.log` is one tab-separated line per attempt — name, role,
exit code, tokens, seconds, and `ok`, `repaired`, `cached`, `interrupted` (the run was
stopped, not the agent failing), `dropped: <why>`, `retry <n>: <why>` for a failed
attempt that earned the unit another run (only the last failure is a drop), or
`deadline: <item> not started` — one line per item left unrun once the run deadline had
passed (recorded, never a drop; `--resume` runs it) — which is
what you read after an exit 4; after an exit 8 it is `error.log` you read. Each line
reports that attempt's own tokens and seconds, and a unit's final line reports only its
last attempt, so the token column of `run.log` sums to the run's token total. `totals.json` holds the `agents_run`,
`tokens`, `seconds` and `invocations` summed over every invocation that wrote a report, so
a resumed run's Run table does not undercount. A run folder cannot be reused without
`--resume`: pointing `--out` at one that exists is a usage error (exit 2), so a resume is
the only way back into a half-finished run.

A multi-round run (deep, overnight) also has `rounds.json` (the rounds started, so a
resume finishes an interrupted round instead of starting a new one), one `round-2/`,
`round-3/`, ... folder per later round holding that round's `plan.json`, `urls.json`,
`claims.json`, `fetch_stats.json` and `votes.json` (round 1's stay at the top), unit
names prefixed `r2-`, `r3-`, ..., a `report-round-<r>.md` per round beside `report.md`
(the latest), a `rounds` row in the Run table, and `stop_reason` in `totals.json`.

`report.md` is the direct answer and the findings with `[n]` citations, a "Refuted or
unclear" section, a "Gaps" section, a `## Sources` list and a `## Run` table (sources
fetched, claims extracted, supported, refuted, unclear, agents run, units dropped,
stopped at deadline (yes when the run deadline left items unrun), tokens,
invocations, wall time — the agents, tokens and wall-time figures are the run's
cumulative totals, not just this invocation's). Its path is the last line of stdout on
exit 0 and 4; on exit 5 there is no report and the run folder's path is. If fewer than 3 sources yielded a claim, or
none was supported, the report opens with a **"Thin evidence"** notice instead of padding —
what follows it is leads, not conclusions.

## Exit codes and resume

| exit | meaning |
|---|---|
| 0 | report written |
| 4 | report written, but some agents were dropped or the deadline left items unrun: `run.log` says which and why |
| 2 | usage (`--max-agents` below the voters per claim, an empty question, a bad flag, a negative `--retries`, a non-positive `--hours`, a malformed `--role-effort`, a `QWEN_DR_MAX_UNIT_SECONDS` below 1 or non-numeric, reusing a run folder without `--resume`) |
| 3 | preflight failed — the message says whether it was the model or the search |
| 5 | a phase produced nothing usable (no angles, no sources, no claims); no report |
| 8 | internal error — the message is on stderr; when a run folder exists, its `error.log` has the traceback |
| 130 | interrupted (Ctrl-C) |

`qwen-deep-research --resume RUN_DIR` continues where it stopped: a phase whose output file
is already complete is skipped, and inside the interrupted phase only the agents whose answer
never landed re-run — the rest are read back from `agents/<name>.json`. Phases left
deadline-truncated have no phase file, so a resume finishes exactly the work `run.log`
logged as `deadline`. A resume keeps the run's stored config (question, depth, caps,
`--max-agents`, items per agent, budgets, retries, effort, deadline) so work is re-dealt
exactly as it was. A resume takes `--seats`, `--web-seats`, `--timeout`, `--retries`,
`--hours`, `--effort` and `--role-effort`, and also the engine's `--rounds` (the new round
count from then on) and `--keep-sandboxes`: this command's own resume message and its
synopsis above name only the first seven, the wording this command shipped with before the
engine existed, while `qwen-swarm --resume` names all nine ([`swarm.md`](swarm.md)) — and in
a research run `--keep-sandboxes` keeps nothing, since no research role has a `sandbox`
fence. Asking instead for a setting the run folder owns — a second goal word, `--stdin`,
`--depth`, `--max-agents`, `--max-items`, `--out`, `--set`, `--target`, `--preflight`
(which is what `--check` becomes here) — is a usage error rather than a run that quietly
differs from the first one. The settings the resume actually ran with are written back to
`config.json`, so a later resume reuses them unnamed; a `--hours` on resume sets a new
deadline from then, and without one the stored deadline stands (an already-passed one
leaves only synthesis to do). A
`--timeout` given on resume changes only the unfinished agents — the ones already logged
keep the budget they ran under. `--effort` and `--role-effort` are reused from the config
unless given again; given again, `--effort` replaces the stored global level and
`--role-effort` merges into the stored per-role dict (later wins; a role it does not name
keeps its stored level), and they change what the unfinished agents run at — they are part
of each unit's cache key, so agents finished at another effort re-run.

## The fence

Each agent runs with its working directory in its own empty folder under the run folder, so
there is nothing of yours in reach, and no agent gets Bash, Write, Edit, Read, Glob or Grep.
Only the searcher, reader and verifier reach the network at all — the `search` tool, plus
`WebFetch` for the reader and verifier — and the scoper and synthesizer get no tools at all.
Page text is treated as data: the worst a hostile page can do is plant a false claim, which
is exactly what the adversarial verify phase exists to catch. Nothing writable or executable
of yours is anywhere in an agent's reach.

## Research needs the internet

`qwen-deep-research` is the command here that needs internet access — a search backend, and
pages to fetch. The same holds for `qwen-swarm research` and for any workflow whose roles
carry a `search` or `web` fence: those go online on purpose, through the `search` tool and
`WebFetch` and nothing else. No other workflow needs the network for its work, and no other
fence adds a network tool — but a `sandbox` role has Bash, and Bash in a sandbox is your
shell, with your network: a sandbox is a copy of the target, not a jail
([`swarm.md`](swarm.md)). On a machine that has to stay air-gapped, this is the command to
leave off it.
