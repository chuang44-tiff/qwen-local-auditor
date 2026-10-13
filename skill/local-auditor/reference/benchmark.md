# Benchmark: a local model, with and without the loop

## Setup

One local model (Qwen3.8-Flash-Next, NVFP4, served by vLLM on one workstation GPU) driving
Claude Code at `--effort xhigh`, on two task sets:

- **Aider polyglot, 20 exercises**: a fixed, seeded sample (Python 4, JavaScript 4, Go 3,
  Rust 3, Java 3, C++ 3) of well-known public exercises.
- **Terminal-Bench 2.0, 15 hard tasks**: reverse engineering, cryptanalysis, interpreters,
  image reconstruction, code golf, numerical work. Six tasks whose reference solution did
  not pass the grader here were dropped.

`qwen-agent --until-done` works in rounds: after each round the hidden tests run and the
failures go to the next round, as in Aider's own protocol. Every run here allows two rounds.

## Aider polyglot, 20 exercises

| setup | server slots | Claude Code | first attempt | with a second chance |
|---|---|---|---|---|
| Claude Code alone (Harbor `claude-code` agent) | 4 | 2.1.286 | 12/20 | 14/20 (independent retry) |
| Claude Code alone (Harbor `claude-code` agent) | 6 | 2.1.295 | 11/20 | 12/20 (independent retry) |
| `--until-done` | 4 | 2.1.286 | 10/20 | **18/20** |
| `--until-done` | 6 | 2.1.295 | 10/20 | **18/20** |
| `--until-done` | 6 | 2.1.296 | 11/20 | **17/20** |

With the loop, the same model on the same exercises went from 12-14/20 to 17-18/20. The gain
is the second round: the loop's first round (10-11/20) was no better than Claude Code alone.
Claude Code alone does not change with this repo's releases, so those rows were not rerun on
2.1.296. One Harbor trial was excluded for fetching the exercise's upstream tests.

On Terminal-Bench 2.0's easier tasks (10 tasks: 3 easy, 6 medium, 1 hard) Claude Code alone
already solved 8-9/10 at the first attempt and 10/10 with a retry, so the hard set below is
used instead.

## Terminal-Bench 2.0, 15 hard tasks

Claude Code 2.1.296, two rounds, each limited to the task's own agent timeout.

| setup | sessions at once | first round | after the second | tokens |
|---|---|---|---|---|
| `--until-done` | 6 | 8/15 | 10/15 | 63M |
| `--until-done` | 4 | 11/15 | **13/15** | 96M |

The four-session row reruns the five tasks the six-session run failed and keeps the ten it
passed; three of the five then passed on the first round, so the six-session run lost tasks
to a busy server, not to the model. The advisor runs below use four sessions.

Tokens are everything the local model read and wrote, cache and subagents included (an
earlier version of this page left subagents out and undercounted).

## The advisor

`--advisor opus` (experimental, off unless typed) lets the local coder get advice from Claude
Opus through your own `claude` login
([details](qwen-agent.md#advisor---advisor-model-experimental)). It makes two kinds of call,
both on by default: the **coder asks** when it decides to (up to 4 times per run), and under
`--until-done` the loop makes a **failure call**: after a failing round it asks the advisor
what went wrong, with the failing output and the round's diff, and puts the answer into the
next round's prompt.

**The failure call is what helps.** Left to choose, the local coder rarely asks at the right moment.
The best row on both task sets includes the failure call; on Aider the failure call alone
reached 18/20 for $0.70, while the coder's own asks alone left the score at 17/20 for $1.87.
This agrees with Anthropic's finding, quoted below, that smaller executors under-call an
advisor left to decide when to ask.

### Results

Aider polyglot, 20 exercises, 6 sessions, Claude Code 2.1.296:

| setup | first round | final | round-1 failures fixed | local tokens | advisor calls | advisor cost |
|---|---|---|---|---|---|---|
| `--until-done` | 11/20 | 17/20 | 6 of 9 | 15.2M | - | - |
| `--advisor opus`, the coder's own asks only | 12/20 | 17/20 | 5 of 8 | 12.4M | 21 | $1.87 |
| `--advisor opus`, the failure call only | 12/20 | 18/20 | 6 of 8 | 13.0M | 8 | $0.70 |
| `--advisor opus`, both (the default) | 12/20 | **19/20** | **7 of 8** | 12.5M | 8 + 19 | $2.30 |

Terminal-Bench 2.0, 15 hard tasks, 4 sessions:

| setup | first round | final | round-1 failures fixed | local tokens | advisor calls | advisor cost |
|---|---|---|---|---|---|---|
| `--until-done` | 11/15 | 13/15 | 2 of 4 | 96M | - | - |
| `--advisor opus`, the coder's own asks only | 9/15 | 13/15 | 4 of 6 | 96M | 11 | $1.95 |
| `--advisor opus`, both (the default) | 10/15 | **14/15** | **4 of 5** | 114M | 5 + 16 | $3.67 |

One run per row, on 20 and 15 tasks: read the gains as what this setup did, not as a measured
rate. The asks-only rows ran on the previous release, which had no failure call; the
failure-call-only row ran on a benchmark build, because in this release a call budget of 0
does not yet turn the coder's own asks off.

### Why the failure call helps

The coder sees the failing test output in its next round but often does not act on it. The
failure call puts a second model on the same output, asks it to name the mistake, and makes
the answer part of the prompt the coder must respond to.

- **scale-generator (Aider)** failed in both runs without the failure call. The advice read
  the failing assertions ("the expected output has 13 notes; `chromatic` emits 12") and named
  the one-line fix; it passed in both runs with the failure call.
- **path-tracing (Terminal-Bench)**: round 1 was all analysis and the required file was never
  written. The advice said so ("write `image.c` now, even if crude") and round 2 passed.

A failure call cost about $0.09 on Aider and $0.13 on the hard set, at most one per failing
round; most of the default's cost is the coder's own asks. On the hard set the default used
more local tokens because the tasks it rescued ran a second round.

### The coder's own asks

The coder is not trained to know when to ask. With only its own asks, all 11 calls on the
hard set went to tasks the loop solves alone, and in go-counting it argued back from its own
recollection instead of applying the advice. Once the timing was right: on gpt2-codegolf,
stuck after many failed variants, it sent measurements of an unknown weight file and then its
code; the answers worked out the file's layout and found an inverted layer map, and the task
passed for the first time in any run. That was luck; a call made after every failing round
catches the same moment on purpose.

Anthropic reports the same problem with its own smaller executors: "In two runs each, Claude
Haiku 5.5 and Claude Sonnet 5.5 called the Opus 5.5 advisor on none of the 198 questions, so
both pairings scored within run-to-run noise of the executor alone." Their fix is to ask for
calls at fixed points
([Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence#advisor-strategy-escalate-hard-decisions),
[Advisor tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool)).
The failure call is that idea applied to the loop.

### Whether the advisor already knows the answer

On Aider's public exercises it can. With the original advisor note, the asks-only run scored
20/20, and that score is not counted: in every exercise the coder asked Opus what the hidden
tests assert, and Opus answered from memory, once reconstructing the tests and the reference
solution. Blocking the web does not stop this.

For the runs in the tables, both models were told, in the benchmark harness only, not to
reproduce the benchmark's tests, expected outputs or reference solutions from memory, and
every answer was read. Opus declined to quote tests ("I recognise this exercise, so I won't
quote its tests") and judged against the spec. Once, in go-counting with only the coder's
asks, it still recalled what the tests import; that exercise is counted as failed (18/20 raw,
17/20 in the table). The failure-call answers reason from the failing output the coder also
sees; none quoted tests beyond it. On the hard set no answer recited tests or solutions.

## How the runs were kept honest

- **No answer keys.** Given network access, the model went looking for the exercises'
  upstream tests and reference solutions, so these runs disable the web tools and block the
  hosts that serve them; every transcript is scanned for attempts (all blocked). `github.com`
  stays reachable for the Terminal-Bench grader only.
- **No recalled answer keys.** Every advisor answer is read for test assertions, expected
  values or reference solutions; an exercise where one decided the outcome counts as failed.
- **All tests count.** Harbor's Aider adapter runs only the first test case for C++, Rust and
  JavaScript; these runs enable every case, and the reference solutions pass all 20.
- **Grading is separate from the work.** On Terminal-Bench, grading copies the coder's result
  into a fresh container with the task's CPU and memory limits.
- **Not leaderboard numbers.** Small samples, tests hidden from the model, one machine, and
  the setups differ: the loop's coder could only compile and run its code through
  `qwen-test`, while Harbor's agent had a full shell and a short generic instruction (hidden
  tests will run, code hosts are blocked, match the spec).

Two findings became policy: web access is opt-in (`--web`) and warns when combined with
`--test`, and the coder role is told to match the spec rather than stop at "it works".

## Concurrency

Aider sample, one attempt per exercise, at 4 to 10 concurrent sessions. Decode is aggregate
generated tokens per second while a run is active; TTFT is the server's mean time to first
token.

| sessions | 4 server slots: decode, TTFT | 6 server slots: decode, TTFT |
|---|---|---|
| 4 | 233 tok/s, 0.45 s | 334 tok/s, 0.54 s |
| 6 | 317 tok/s, 0.76 s | 387 tok/s, 0.49 s |
| 8 | 340 tok/s, 3.97 s | 426 tok/s, 1.37 s |
| 10 | | 461 tok/s, 2.94 s |

With 4 slots, throughput stopped rising at 8 sessions while requests queued; with 6 slots it
kept rising to 10, and 8 sessions waited at most 2 requests. Agent sessions spend much of
their time in tools, so 4 slots serve about 6 sessions and 6 slots about 8. Prefix-cache hit
rate was 67-75%, with no preemptions on the Aider exercises. The hard Terminal-Bench tasks
run for up to an hour with long contexts; their six-session runs shared the server and saw
preemptions, so the later runs use four.

For how a Qwen chat template can cost vLLM most of its prefix cache under Claude Code,
and the fix, see [`vllm.md`](vllm.md).
