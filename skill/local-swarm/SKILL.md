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
| `ui-test` | a scripted UI suite run as a swarm: one browser agent per scenario, scored against the suite, every failure re-checked | `--set scenarios=SUITE.md`; the app already running at its URLs |

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

    qwen-swarm ui-test "RUN NAME" --set scenarios=SUITE.md --out RUN_DIR [--set base=URL] [--set confirm=claude|local|none]

One `browser` agent runs one scenario, its screenshots kept in `RUN/browser/<unit>`, and every
answer is scored by the same `lib/scenarios.py` that wrote the prompt: `RUN/results.json` and
the report say PASS/FAIL/BLOCKED (or NOT RUN, when the deadline kept its unit from
starting) per scenario. A tester that ends without its result block is asked once more. The
app must already be listening at the suite's URLs, and a missing `--set scenarios=` or a suite
that does not parse is exit 2 naming its line before an agent starts. Files a scenario
uploads go in the folder the suite's `fixtures: DIR` line names (relative to the suite;
`--set fixtures=DIR` overrides it): every tester gets its own copy and each file's absolute path.
A link pointing outside that folder is refused, and the folder is taken as constant for the
run; keep it non-secret -- the confirmer can read the fixtures and every tester's evidence,
and a hostile page could prompt-inject a tester into posting what it can read.

Every FAIL or BLOCKED is re-checked before it counts. By default Claude (`confirm_model`,
default opus; at most `confirm_max` = 10 calls a run) re-runs it in a browser of its own
through the user's claude login, so the scenario, the tester's report and screenshots and a
browser session on the app leave this machine; the run says so when it starts. Tell the user
before a run on a private app: `--set confirm=local` keeps the check on the local model,
`--set confirm=none` skips it. Each such row ends CONFIRMED (real), FALSE_ALARM (it works;
the report lists these apart) or NEEDS_HUMAN (no answer, e.g. offline). Under `confirm=local`
a FALSE_ALARM is advisory: reported, but the row still counts -- only a Claude or a
`--record-verdict` FALSE_ALARM clears a row.

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

Then watch the run's events, not the process:

1. The first stderr line after the preflight is `qwen-swarm: run folder: RUN` (the prefix is
   the command's name); read RUN (an absolute path) from the background task's output.
2. Arm the Monitor tool with this command, `timeout_ms` 1800000 (its maximum), RUN filled
   in. N = 0 for a fresh run; for a `--resume`, N = the current line count of
   RUN/events.jsonl taken BEFORE starting the resume -- a resume appends to the same file,
   so arming at 0 replays the PREVIOUS run's `attention` lines and exits on its `run_end`:

       RUN=/abs/run/folder; n=N
       while :; do
         t=$(( $(wc -l 2>/dev/null < "$RUN/events.jsonl") + 0 ))
         if [ "$t" -gt "$n" ]; then
           sed -n "$((n+1)),${t}p" "$RUN/events.jsonl" | awk -v n="$n" '
             {n++} /"kind": ?"(attention|unit_dropped|run_end)"/ {print n": "$0; fflush()}
             /"kind": ?"run_end"/ {e=1} END {exit !e}' && exit 0
           n=$t
         fi
         sleep 5
       done

   Each event arrives as `<line number>: <json>`; only whole lines are read, and the command
   exits by itself after `run_end`. A monitor expires after at most 30 minutes: re-arm the
   same command with N = the last line number you were shown (0 if none) until `run_end`
   has arrived.
3. When the background task itself exits, the run is over even without `run_end` (a killed
   runner never writes it): stop the monitor with TaskStop and go to step 4.
4. Handle each `attention` event as it arrives (section 4, "Report back"): its `reason` is
   `confirmed failure`, `needs human` or `false alarm on a tester FAIL`; `unit_dropped`
   names a unit and why. Read `RUN/run.log` (one line per agent attempt) only for detail.

## 4. Report back

Read the report (the path is the last line of stdout on exit 0 and 4).

ui-test gives you the final say on every FAIL/BLOCKED row. On an `attention` event, or when
the run ends with non-PASS rows, check each one: read the tester's evidence and the
confirmer's (`confirmation` in `RUN/results.json`, screenshots in `RUN/browser/<unit>`),
then prefer a short scripted probe (a Playwright script, a canvas hash, the app's own state,
the console filtered to uncaught errors) over looking at screenshots. With more than 3 rows,
give each to a subagent. Record each conclusion:

    qwen-swarm --record-verdict RUN --id ID --verdict CONFIRMED|FALSE_ALARM|NEEDS_HUMAN --evidence TEXT [--evidence TEXT ...]

Agreeing with the confirmer needs no command. While the run is live the verdict is saved and
the workflow applies it (exit 0); afterwards the command rewrites results.json and report.md
and exits with the run's new code. Its exit 5 means the run never finished: the verdict is
applied by `qwen-swarm --resume RUN`. Its exit 8 means the verdict was saved but not applied
(the render lock is busy): run the same command again. Neither re-render -- this one or the
runner's post-run one -- emits `verdict` events.

Gate on the process exit code (or the exit of the last `--record-verdict`), never on the
`exit` in `run_end`: a verdict that lands at the very end can change the exit after `run_end`
was written.

Offline, or with the claude login unusable, the first failed confirm call (a connection error
included) trips the breaker: the remaining rows are NEEDS_HUMAN with no further call, and the
run's local half is unaffected. That breaker trips only on claude's own login/network failures;
words from the page under test fail only that row. Settle those rows yourself as above.

| Exit | Meaning |
|---|---|
| 0 | report written, goal met |
| 4 | report written, but agents were dropped, the deadline left work unrun, or the goal was not met (debug: no winning patch; ui-test: a FAIL/BLOCKED row whose final verdict is not FALSE_ALARM) |
| 2 | usage or manifest error, `--check` failed, or the run is live (`--resume` of a running run): the message names it |
| 3 | preflight: model (or search) unreachable |
| 5 | nothing usable, no report; the last stdout line is the run folder |
| 8 | internal error, including an exception in workflow.py: `RUN/error.log` has the traceback |
| 130 | interrupted |

A dropped agent keeps exit 4 whatever verdict its row gets. After an exit 8 in your own
workflow, fix `workflow.py` and continue with `qwen-swarm --resume RUN`: finished agents are
reused from the cache. Exit 130 or a crash: `qwen-swarm --resume RUN` too. On resume only
`--seats`, `--web-seats`, `--timeout`, `--retries`, `--rounds`, `--hours`, `--effort`,
`--role-effort`, `--deep`, `--shallow` and `--keep-sandboxes` may change.
