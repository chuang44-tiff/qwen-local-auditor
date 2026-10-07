---
name: local-swarm
description: Run a multi-agent job on the local model with `qwen-swarm` - a built-in workflow (research a question with cited sources, debug a bug in a codebase down to a checked patch, run a scripted UI suite with one browser agent per scenario) or a workflow you write for the purpose (manifest + workflow.py + role files), often as a long overnight run. Use for "debug this with the swarm", "run an overnight swarm", "find the root cause and a tested fix", "run this scenario file against the app", "swarm this task", or any job that needs many local agents working in rounds. Plain web research has its own skill, local-deep-research.
---

# Local swarm

A workflow is a folder: `workflow.json` (the manifest the user approves: roles and their
fences, knobs, depth presets), `workflow.py` (`run(wf)`: the calls to make) and one role
file per role. The engine owns everything else: dealing, seats, retries, caching, resume,
deadlines, sandboxes, totals and exit codes.

## 1. Pick a built-in when one fits

    qwen-swarm --list

| workflow | for | needs |
|---|---|---|
| `research` | a cited, vote-verified report on a question | a search backend (see `local-deep-research`) |
| `debug` | the root cause of a bug and a patch that passes the reproduction | `--target REPO`; `--set repro="CMD"` optional |
| `ui-test` | a scripted UI suite run as a swarm: one browser agent per scenario, scored against the suite | `--set scenarios=SUITE.md`; the app already running at its URLs |

For research, load `local-deep-research` instead: it covers that workflow fully.

    qwen-swarm debug "SYMPTOM" --target REPO --out RUN_DIR [--set repro="CMD"] [--set tests="CMD"] [--depth quick|standard|deep|overnight]

`RUN_DIR` is outside `REPO`: a run writes its prompts, patches, logs and sandboxes into its
run folder, so a run folder inside the target (or a target inside it) is refused (exit 2).
`repro` must exit non-zero while the bug is there. Without the knob the triager proposes a
reproduction instead, and the run reproduces and checks patches against that command: it
is logged in `run.log` as the triager's, and it runs only in a sandbox of the target.
The debug workflow never writes REPO: probers work in throwaway copies, every candidate
patch is checked in a fresh copy (repro, then tests), a patch that edits test files is
flagged and cannot win, and reviewers vote on root cause vs symptom suppression.
Patches land in `RUN/patches/<n>.diff`; the user applies one with `git apply`.

For a UI suite the scenario file is the `scenarios` knob (the same `# Suite:` markdown
`qwen-agent --scenarios` takes; its format is in `reference/qwen-agent.md`), and the goal only
names the run:

    qwen-swarm ui-test "RUN NAME" --set scenarios=SUITE.md --out RUN_DIR [--set base=URL]

One `browser` agent runs one scenario, its screenshots kept in `RUN/browser/<unit>`, and every
answer is scored by the same `lib/scenarios.py` that wrote the prompt: `RUN/results.json` and
the report say PASS/FAIL/BLOCKED (or NOT RUN, when the deadline kept its unit from
starting) per scenario. Exit 0 when all passed; 4 when any did not, or an agent was dropped. The
app must already be listening at the suite's URLs, and a missing `--set scenarios=` or a suite
that does not parse is exit 2 naming its line before an agent starts.

## 2. Otherwise write a workflow

Ask the user where the folder should live, then copy the closest built-in and change it:
`skill/local-auditor/lib/workflows/debug` is the model for a workflow that edits a target
(sandbox roles, `run_cmd` checks, patches), `.../research` the model for a web workflow
(search roles, fan-out, votes). The API is in
`skill/local-auditor/reference/swarm.md` — read it before writing `workflow.py`.
Rules that keep a workflow resumable:

- `run(wf)` must make the same calls in the same order given the same answers: no clock,
  no randomness, no environment, no file reads outside `wf.load`. `wf.rounds()` owns time.
- Choose tools only through each role's `fence`: `none`, `browser` (a real browser through
  qwen-agent `--browser`, for a local UI suite; its screenshots land in
  `RUN/browser/<unit>`, and it can open any URL it is told to), `search`, `web`, `read`
  (the target, read-only) or `sandbox` (edit + Bash in a throwaway copy of the target).
- Commands run only through `wf.steps.run_cmd`: `bash -c CMD` in a fresh sandbox of the
  target, with the result logged in `run.log`. Where a command comes from is the run's
  business — the user's `--set`, the workflow's own code, or an agent's proposal (with no
  `--set repro=`, debug runs the triager's reproduction) — but it never runs anywhere but
  in that sandbox.
- Depth is the default: an agent reviews its own answer and is nudged to delegate unless
  its role says `"deep": false` (what a high fan-out role says), `--deep ROLE` forces
  depth on and `--shallow ROLE` opts out. Mark a role that fans out over many items
  shallow; the review round is a second call per unit.

Before launching anything (step 3), run

    qwen-swarm --check ./my-workflow

until it passes: it validates the manifest and dry-runs `run(wf)` twice against fake
agents; a `check.py` beside the workflow can supply realistic fake answers.

## 3. Show the user the manifest, then run in the background

Show the roles with their fences and the knobs of the chosen depth (the run prints this
summary to stderr first, too). Start it with run_in_background; with `--target`, `--out`
must name a folder outside it; overnight runs need `--hours` (the `overnight` presets
set 8) and `--rounds until` without them is a usage error:

    qwen-swarm WORKFLOW "GOAL" [--depth NAME] [--set KNOB=VALUE] [--target DIR] [--out DIR] [--hours H]

While it runs, read `RUN/run.log` (one line per agent attempt), not the process.

## 4. Report back

Read the report (the path is the last line of stdout on exit 0 and 4).

| Exit | Meaning |
|---|---|
| 0 | report written, goal met |
| 4 | report written, but agents were dropped, the deadline left work unrun, or the goal was not met (debug: no winning patch) |
| 2 | usage or manifest error, or `--check` failed: the message names the field |
| 3 | preflight: model (or search) unreachable |
| 5 | nothing usable, no report; the last stdout line is the run folder |
| 8 | internal error, including an exception in workflow.py: `RUN/error.log` has the traceback |
| 130 | interrupted |

After an exit 8 in your own workflow, fix `workflow.py` and continue with
`qwen-swarm --resume RUN`: finished agents are reused from the cache. Exit 130 or a crash:
`qwen-swarm --resume RUN` too. On resume only `--seats`, `--web-seats`, `--timeout`,
`--retries`, `--rounds`, `--hours`, `--effort`, `--role-effort`, `--deep`, `--shallow` and
`--keep-sandboxes` may change.
