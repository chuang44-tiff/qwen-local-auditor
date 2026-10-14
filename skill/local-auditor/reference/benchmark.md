# Benchmark: quality and price

Which coder gets the most tasks right, and what does it cost? Three kinds of coder, same
loop, same tasks, same graders:

- **Qwen3.8-Flash-Next**, a local model. Cost in API dollars: $0. You pay in electricity and
  hardware.
- **Qwen with an Opus advisor.** The local coder, plus a few Claude Opus calls.
- **Claude Haiku 5.5** as the coder, on its own, through Claude Code and Anthropic's API.

Claude costs are API-equivalent dollars at list price. Two task sets:

- **Aider polyglot, 20 exercises**: a fixed, seeded sample (Python 4, JavaScript 4, Go 3,
  Rust 3, Java 3, C++ 3) of well-known public exercises.
- **Terminal-Bench 2.0, 15 hard tasks**: reverse engineering, cryptanalysis, interpreters,
  image reconstruction, code golf, numerical work. Six tasks whose reference solution did
  not pass the grader here were dropped.

Every run used `qwen-agent --until-done` with at most two rounds. After each round the hidden
tests run and the failures go to the next round, as in Aider's own protocol. "Round 1" is the
first try; "final" is after the second chance. One run per row.

The loop itself is where most of the gain comes from. Without it, the same local model in
Claude Code alone solved 12-14/20 Aider exercises even with a retry (two runs); with
`--until-done` it solved 17-18/20 in three runs, from a first round no better (10-11/20).

## Results

Aider polyglot, 20 exercises:

| coder | advisor | round 1 | final | tokens | Claude cost |
|---|---|---|---|---|---|
| Qwen (local) | none | 11/20 | 17/20 | 15.2M | $0 |
| Qwen | Opus, the coder asks only (21 calls) | 12/20 | 17/20 | 12.4M | $1.87 |
| Qwen | Opus, failure call only (8 calls) | 12/20 | 18/20 | 13.0M | $0.70 |
| Qwen | Opus, both (8 failure + 19 own calls) | 12/20 | **19/20** | 12.5M | $2.30 |
| Haiku 5.5 | none | 11/20 | **20/20** | 14.7M | $1.30 |

Terminal-Bench 2.0, 15 hard tasks:

| coder | advisor | round 1 | final | tokens | Claude cost |
|---|---|---|---|---|---|
| Qwen (local) | none | 11/15 | 13/15 | 96M | $0 |
| Qwen | Opus, the coder asks only (11 calls) | 9/15 | 13/15 | 96M | $1.95 |
| Qwen | Opus, both (5 failure + 16 own calls) | 10/15 | 14/15 | 114M | $3.67 |
| Haiku 5.5 | none | 14/15 | **15/15** | 57.4M | $7.07 |

Tokens are everything the coder read and wrote, cache and subagents included. Read them as
"how much work the coder did", not as a price.

## What the numbers say

**Haiku 5.5 is very good.** On Aider it matches the local model on the first try (11/20 each)
and then corrects itself far better: 20/20, against 17/20 for Qwen alone and 19/20 for Qwen
with Opus helping. It costs less than Qwen plus Opus there ($1.30 against $2.30). On the hard
set it leads on both rounds: 14/15 on the first try and 15/15 in the end. Only one task
(dna-assembly) needed a second round. An exact run-for-run comparison would take more runs,
but a cheap model that reads its own failures this well is a real achievement.

**The price bar moved.** Before Haiku 5.5, the case here was simple: Sonnet or Opus as the
coder cost far more than a local model plus a few dollars of advisor calls. The repo was right
about that. Haiku 5.5 moved the bar. On its own it is about as good as the local model on a
first try, much better at correcting itself, and on easy tasks it is also cheaper.

**What the local setup still offers:**

- It works offline, and your code stays on your machine.
- It costs nothing per token. Long runs, retries and overnight jobs have no meter.
- On the hard set it is still cheaper: Qwen with the failure call got 14/15 for $3.67, about
  half of Haiku's $7.07 for one task fewer. Qwen alone got 13/15 for $0.

How Haiku was run and priced: same loop, through Claude Code, no advisor. Cost comes from the
transcripts' token usage at list price ($0.10 in, $0.50 out, $0.01 cache read, $0.125 cache
write, per million tokens). Prompts over 100k tokens bill at 5x. About a quarter of Haiku's
Terminal-Bench requests crossed that line, which is most of why that run cost $7.07.

## The advisor

`--advisor opus` (experimental, off unless typed) lets the local coder get advice from Claude
Opus through your own `claude` login
([details](qwen-agent.md#advisor---advisor-model-experimental)). It makes two kinds of call:

- The **coder asks** when it decides to (up to 4 times per run).
- Under `--until-done`, the harness makes a **failure call**: after a failing round it asks
  the advisor what went wrong, with the failing output and the round's diff, and puts the
  answer into the next round's prompt.

**The recommended use is the failure call**: `--until-done --advisor opus`. The harness, not
the model, decides when to ask. The best Qwen row on both sets includes it. On Aider the
failure call alone reached 18/20 for $0.70, while the coder's own asks alone stayed at 17/20
for $1.87.

This agrees with Anthropic's own advisor findings: "In two runs each, Claude Haiku 5.5 and
Claude Sonnet 5.5 called the Opus 5.5 advisor on none of the 198 questions, so both pairings
scored within run-to-run noise of the executor alone." Their fix is to ask for calls at fixed
points
([Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence#advisor-strategy-escalate-hard-decisions),
[Advisor tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool)).
The failure call is that idea applied to the loop.

**Caveat, once:** each row is one run on 20 or 15 tasks. The results are suggestive, not
proven. The asks-only rows ran on an earlier release with no failure call, and the
failure-call-only row ran on a benchmark build.

### Why the failure call helps

The coder sees the failing test output in its next round but often does not act on it. The
failure call puts a second model on the same output, asks it to name the mistake, and makes
the answer part of the prompt.

- **scale-generator (Aider)** failed in both runs without the failure call. The advice read
  the failing assertions ("the expected output has 13 notes; `chromatic` emits 12") and named
  the one-line fix. It passed in both runs with the failure call.
- **path-tracing (Terminal-Bench)**: round 1 was all analysis and the required file was never
  written. The advice said so ("write `image.c` now, even if crude") and round 2 passed.

A failure call cost about $0.09 on Aider and $0.13 on the hard set, at most one per failing
round. Most of the default's cost is the coder's own asks.

### The coder's own asks

The coder is not trained to know when to ask. With only its own asks, all 11 calls on the
hard set went to tasks the loop solves alone, and in go-counting it argued back from its own
recollection instead of applying the advice. Once the timing was right, it paid off: on
gpt2-codegolf, stuck after many failed variants, the coder sent measurements of an unknown
weight file and then its code. The answers worked out the file's layout and found an inverted
layer map, and the task passed for the first time in any run. That was luck; a call after
every failing round catches the same moment on purpose.

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
  stays reachable for the Terminal-Bench grader only. Haiku's coder never read the hidden
  tests; it saw only the failing-check output the loop gives every coder.
- **No recalled answer keys.** Every advisor answer is read for test assertions, expected
  values or reference solutions; an exercise where one decided the outcome counts as failed.
- **All tests count.** Harbor's Aider adapter runs only the first test case for C++, Rust and
  JavaScript; these runs enable every case, and the reference solutions pass all 20.
- **Grading is separate from the work.** On Terminal-Bench, grading copies the coder's result
  into a fresh container with the task's CPU and memory limits, for all coders including Haiku.
- **Not leaderboard numbers.** Small samples, tests hidden from the model, and the loop's
  local coder could only compile and run its code through `qwen-test`.

Two findings became policy: web access is opt-in (`--web`) and warns when combined with
`--test`, and the coder role is told to match the spec rather than stop at "it works".
