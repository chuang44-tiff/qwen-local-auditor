# Benchmark: a local model, with and without the loop

## Setup

One local model (Qwen3.8-Flash-Next, NVFP4, served by vLLM on one workstation GPU) driving
Claude Code at `--effort xhigh`, on two task sets:

- **Aider polyglot, 20 exercises**: a fixed, seeded, stratified sample (Python 4,
  JavaScript 4, Go 3, Rust 3, Java 3, C++ 3). Well-known public exercises.
- **Terminal-Bench 2.0, 15 hard tasks**: reverse engineering, cryptanalysis, interpreters,
  image reconstruction, code golf, numerical work. Each task's reference solution was run
  through the same grader first; six tasks whose reference solution did not pass here were
  dropped.

`qwen-agent --until-done` runs the coding task in rounds: after a round the supervisor runs
the hidden tests and hands the failures to the next round (Aider's own protocol: one attempt,
then one more with the test output). Every run here allows two rounds. A round that already
passes becomes a review round ("try to break your change").

## Aider polyglot, 20 exercises

| setup | server slots | Claude Code | first attempt | with a second chance |
|---|---|---|---|---|
| Claude Code alone (Harbor `claude-code` agent) | 4 | 2.1.286 | 12/20 | 14/20 (independent retry) |
| Claude Code alone (Harbor `claude-code` agent) | 6 | 2.1.295 | 11/20 | 12/20 (independent retry) |
| `--until-done` | 4 | 2.1.286 | 10/20 | **18/20** |
| `--until-done` | 6 | 2.1.295 | 10/20 | **18/20** |
| `--until-done` | 6 | 2.1.296 | 11/20 | **17/20** |

The same model and the same exercises went from 12-14/20 to 17-18/20 with the loop. The
gain is the second round: the loop's first round scored 10-11/20, no better than Claude Code
alone, and the two setups also differ in role rules, shell access and the appended
instruction. The loop's result held across server settings and releases, and the same
exercises kept failing. Claude Code alone does not change with this repo's releases, so the
Harbor rows were not rerun on 2.1.296. Zero harness failures in these runs: no API errors,
timeouts, context overflows or crashes. One Harbor trial was excluded for fetching the
exercise's upstream tests.

On Terminal-Bench 2.0's easier tasks (10 tasks: 3 easy, 6 medium, 1 hard) Claude Code alone
already solved 8-9/10 on the first attempt and 10/10 with a retry, so the loop was not run
there; the hard set below replaces it.

## Terminal-Bench 2.0, 15 hard tasks

Claude Code 2.1.296, two rounds, each round limited to the task's own agent timeout.

| setup | sessions at once | first round | after the second | tokens | advisor calls, cost |
|---|---|---|---|---|---|
| `--until-done` | 6 | 8/15 | 10/15 | 46M | |
| `--until-done` | 4 | 11/15 | **13/15** | 71M | |
| `--until-done --advisor opus`, original advisor note | 6 | 9/15 | 10/15 | 43M | 6, $1.01 |
| `--until-done --advisor opus`, reworked advisor note | 4 | 9/15 | **13/15** | 88M | 11, $1.95 |

The four-session plain row reruns at four sessions the five tasks the six-session run failed,
and keeps the ten it passed. Three of the five then passed, all on the first round: the
six-session runs lost tasks to a busy server, not to the model. In the original-note advisor
run one task did not start (a harness bug, since fixed), and the server was also shared.

Tokens are everything the local model read and wrote, cache included, counted from the
Claude Code transcripts. They measure cost on this machine better than wall time, which
depends on what else the server is doing.

## The advisor

`--advisor opus` lets the local coder ask Claude Opus for advice through your own `claude`
login (see [`qwen-agent.md`](qwen-agent.md#advisor---advisor-model-experimental)). It stays
in the tools as an experimental flag, but these runs show no major benefit, for two reasons
on our side:

- **The local model asks at the wrong time.** It is not trained to use an advisor, so it
  decides on its own when to ask. All 11 calls on the hard set went to tasks the loop solves
  alone; the two tasks neither arm solved got none.
- **It does not reliably act on the answer.** In go-counting it argued back from its own
  recollection instead of applying the advice, and asked again.

The advisor is not a silver bullet: it pays off only when the executor asks at the right
moment and acts on the answer, and neither comes for free.

The first problem is similar, not the same, in Anthropic's own smaller executors: "The
second, and the fragile one, is whether the executor actually asks (the consult rate). Neither of those executors asked. In two runs each, Claude Haiku 5.5 and
Claude Sonnet 5.5 called the Opus 5.5 advisor on none of the 198 questions, so both pairings
scored within run-to-run noise of the executor alone." Those runs had no system prompt asking
for calls; with only the tool's description, "executors under-call, especially on coding
work", and Anthropic's fix is a prompt that asks for a call at fixed points, before
substantive work and before finishing
([Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence#advisor-strategy-escalate-hard-decisions),
[Advisor tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool)).
The difference: those executors had no prompt and asked none at all, while ours had one,
asked 11 times, and still asked on the wrong tasks, because our note leaves the timing to
the model. Anthropic also reports that a reminder after the first turn raised Claude Haiku
4.5 executors' pass rates by about 7 points. It does not report executors ignoring advice;
that one is our own observation.

Making the advisor pay off would take major work on the executor side: deciding when to ask
from the harness rather than leaving it to the model, and checking that the advice was
applied. The details follow.

Two things decide whether it helps.

**Whether the coder asks.** With the original note, the local model asked rarely: 6 calls
over 15 hard tasks, none of which changed an outcome. The reworked note tells it to ask when
it is unsure of the approach, when a check fails and it does not know why, and when it is
not converging, and to act on each concrete finding rather than set the advice aside. It
then asked 11 times, and the advice was real analysis: on path-tracing Opus found an
arithmetic slip in the coder's own camera fit, and on path-tracing-reverse it traced a
one-pixel difference to a single float rounding step. It did not change the score. Both
tasks also passed without the advisor once the server was not busy, and the advisor arm
finished at 13/15, the same as the loop alone, with fewer first-round passes (9 against
11) and more tokens (88M against 71M). Neither arm solved gpt2-codegolf or
make-mips-interpreter.

**Whether the advisor already knows the answer.** On Aider's exercises it does. With the
original note the advisor arm scored 16/20 on the first round and 20/20 after the second,
and that score cannot count: in every exercise the coder asked Opus what the hidden
Exercism tests assert, and Opus answered from memory, in one case reconstructing the test
cases and the reference solution. Blocking the web did not block this: the answer key came
from the stronger model's memory of a public benchmark, not from Claude Code's own memory
(each advisor call is a fresh `claude -p` with no tools, settings or saved sessions).

For the reworked-note runs, both models were told, in the benchmark harness only, not to
reproduce the benchmark's tests, expected outputs or reference solutions from memory. Opus
then declined to quote tests ("I recognise this exercise, so I won't quote its tests") and
judged against the spec instead. It still slipped once: in go-counting it recalled what the
tests import and what they expect on a stone, and that decided the exercise. Counting that
exercise as failed, the advisor arm scored 12/20, then 17/20, the same as the loop without
it, with 21 calls ($1.87) and 10.8M tokens against 12.6M.

Every advisor answer on the hard set was read for recalled tests, expected outputs or
reference solutions; none were found, so on this set what the advisor adds is the advice
itself.

## How the runs were kept honest

- **No answer keys.** Given network access, the model went looking for the exercises'
  upstream tests and reference solutions (`curl`, `WebFetch`, `git clone`). These runs
  disable `WebFetch`/`WebSearch` and block the hosts that serve them; every transcript is
  scanned for attempts (all blocked). Terminal-Bench graders download a tool from a GitHub
  release, so `github.com` stays reachable for the grader only.
- **No recalled answer keys.** Every advisor answer is read for test assertions, expected
  values or reference solutions, and an exercise where one decided the outcome counts as
  failed.
- **All tests count.** Harbor's Aider adapter runs only the first test case for C++, Rust and
  JavaScript (Exercism marks the rest as skipped). The runs here enable every case; the
  reference solutions pass all 20 exercises under the patched graders.
- **Terminal-Bench grading is separate from the work.** The coder runs its scripts in the
  task's own container; grading copies the result into a fresh container with the task's CPU
  and memory limits.
- **Not leaderboard numbers.** Small samples, tests hidden from the model, one machine.
  Harbor attempts are independent; the `--until-done` coder's only shell grant was
  `qwen-test`, set up to compile its code or run it in the task's container (plus the
  read-only commands Claude Code allows inside the working directory), while Harbor's agent
  had a full shell. The Harbor runs append a short, generic instruction (hidden tests will
  run, network to code hosts is blocked, check your output against the spec, do not add
  unrequested leniency).

Two of these findings became policy in the tools: web access is opt-in (`--web`) and
warns when combined with `--test`, and the coder role is told to match the spec rather
than stop at "it works".

## Concurrency

Aider sample, one attempt per exercise, at 4 to 10 concurrent sessions. Decode is aggregate
generated tokens per second over the active part of each run; time-to-first-token (TTFT) is
the server's mean.

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
are different: their sessions run for up to an hour with long contexts. The six-session runs
shared the server with other clients and saw preemptions, so the later runs use four.

For how a Qwen chat template can cost vLLM most of its prefix cache under Claude Code,
and the fix, see [`vllm.md`](vllm.md).
