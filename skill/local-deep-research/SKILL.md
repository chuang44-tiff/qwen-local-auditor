---
name: local-deep-research
description: Research a question on the web with a swarm of local-model Claude Code sessions via `qwen-deep-research` - searches from several angles, reads the sources, has independent agents try to refute each claim, and writes a cited report. Use for "research X", "deep research", "find out what is known about X", "fact-check X with sources" when the work should run on the local model. Needs a search backend (SearXNG or a Brave key) and internet access.
---

# Local deep research

## 1. Sharpen the question (you, in this session)

If the question is underspecified (no scope, timeframe, region, budget or use case where one
matters), ask 2-3 short clarifying questions first and fold the answers into one precise
question. Do not research a vague question.

## 2. Check, then run in the background

    qwen-deep-research --check
    qwen-deep-research "PRECISE QUESTION" [--depth quick|standard|deep|overnight] [--max-agents N]
                                          [--max-items N]
                                          [--seats N] [--web-seats N] [--retries N] [--hours H]
                                          [--effort LEVEL] [--role-effort ROLE=LEVEL[,ROLE=LEVEL...]]

Run it with run_in_background: a standard run takes tens of minutes. The `--depth` presets:

| `--depth` | angles | sources | claims | voters | per-item budget | retries |
|---|---|---|---|---|---|---|
| `quick` | 3 | 6 | 10 | 1 | 240 s | 1 |
| `standard` (default) | 5 | 15 | 25 | 3 | 240 s | 1 |
| `deep` | 8 | 30 | 50 | 3 | 600 s | 2 |
| `overnight` | 10 | 40 | 80 | 5 | 900 s | 3 |

`--depth quick` for a fast look, `deep` for a thorough one. Deeper presets are meant for
long unattended runs: locally, time is cheap, so they trade wall time for completeness.
`--max-agents` (default 8) caps agents started per phase; work is split among them, never
cut (it must be at least the voters per claim — 5 for `overnight`). `--max-items` (default
10) caps the items one agent holds: a phase with more items than `--max-agents x --max-items`
splits them in order into waves, one wave run after another. `--seats` (default 4)
caps agents running at once; keep it at or below the server's concurrent-request capacity.
`--web-seats N` caps the search/fetch/verify agents among them (default: `--seats`; must be
between 1 and `--seats`) — web agents are told to call one tool at a time, so lower
`--web-seats` if the server shows requests waiting during the search, fetch and verify
phases. `--stdin` takes the question from stdin. `--timeout S` is a per-item budget
(default: the preset's): an agent holding k items gets max(300, k x timeout) seconds,
readers get twice the per-item budget since each source is a whole page
(max(300, 2 x k x timeout)), and scope and synthesis max(300, 2 x timeout); no unit's
timeout, retry doublings included, ever exceeds 4 h (14400 s). `--retries N`
(default: the preset's) re-runs a unit only when its last failure was a timeout (exit 5),
a server error (exit 3 or 4) after its backoff, or an answer still unusable after its
repair round (or with no session to repair) — any other exit code drops it at once — each
try with double the previous timeout (up to the 4 h cap), before it counts as dropped.
`--hours H` (default: 8 for `overnight`, none otherwise) is a hard stop for the whole
run: once H hours from the first start have passed, no new wave or unit starts (running
units finish, queued ones of the current wave do not), every item not run is logged as
`deadline` in `run.log`, the phase continues with what it has — its file stays unwritten —
and synthesis always runs. Each agent is capped at 4 h; --hours bounds the whole run.
`--resume RUN --hours H` sets a new deadline from then; without `--hours` a resume keeps
the stored deadline.
`--effort LEVEL` sets the reasoning effort of every role (qwen-agent validates
the level), and `--role-effort ROLE=LEVEL[,ROLE=LEVEL...]` — roles scoper, searcher, reader,
verifier, synthesizer — beats it for single roles; both are stored with the run and are part
of each agent's cache key. `--out DIR` picks the run folder; an existing run folder cannot be reused
without `--resume`.

`--check` failing with "search:" means no backend is configured: point the user to
`local-auditor`'s `reference/deep-research.md` (SearXNG one-liner, or QWEN_SEARCH_KEY for
Brave).

## 3. Report back

Read `<run>/report.md` (the path is the last line of stdout). Give the user the direct
answer, the 3-5 strongest findings with their source links, anything refuted that they
might otherwise believe, and the Run line (sources, claims, supported/refuted, wall time).

| Exit | Meaning |
|---|---|
| 0 | report written |
| 4 | report written, some agents dropped or the deadline left items unrun (see run.log) |
| 2 | usage (empty question, bad flag, a non-positive `--hours`, an existing run folder without `--resume`) |
| 3 | preflight: model or search unreachable |
| 5 | a phase produced nothing usable (no report) |
| 8 | internal error: stderr has the message, error.log the traceback (when a run folder exists) |
| 130 | interrupted: `qwen-deep-research --resume <run>` continues |

What to do after each outcome:

- Exit 4: read `<run>/run.log` and tell the user how many agents were dropped and in which
  phase, and that the deadline left items unrun; the report is still usable.
- Exit 3 or 5: report the cause from stderr and do not retry blindly — for 3, fix the search
  backend or the model server first; for 5, the question may be too narrow or the backend
  returned nothing.
- Exit 8: internal error — relay the stderr message; when a run folder exists, its `error.log`
  has the traceback. This is a bug in the run, not a research outcome.
- Exit 130, or the run crashes: continue from what already finished:

      qwen-deep-research --resume <run folder>

  On resume only `--seats`, `--web-seats`, `--timeout`, `--retries`, `--hours`, `--effort`
  and `--role-effort` may be given; a `--timeout` there applies only to the agents that
  still have to run, the stored effort is reused unless `--effort`/`--role-effort` is given
  again (a `--role-effort` there merges into the stored dict, later wins; `--effort`
  replaces the stored global level), and the stored deadline stands unless `--hours` starts
  a new one from then — work the old deadline left unrun is logged as `deadline` in
  `run.log`. The settings a resume ran with are written back to `config.json` for the
  next one.

For long or multi-line questions, pass the question through a file, not argv:

    qwen-deep-research --stdin < question.md

While the run is going, check progress by reading the run folder's `run.log` (one line per
agent attempt), not by polling the process. And do not use it for questions about the local
codebase — that is `local-auditor` / `local-sweep` work.

A report that opens with a "Thin evidence" notice (fewer than 3 sources yielded a claim, or
none was supported) is a set of leads, not a verdict — say so when you relay it.

Research reaches the internet; everything else in this toolkit stays offline-capable.
