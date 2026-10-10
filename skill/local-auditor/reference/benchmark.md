# Benchmark: a local model, with and without the loop

## Setup

One local model (Qwen3.8-Flash-Next, NVFP4, served by vLLM on one workstation GPU) driving
Claude Code at `--effort xhigh`, on a fixed sample of
20 Aider polyglot exercises (seeded, stratified: Python 4, JavaScript 4, Go 3, Rust 3,
Java 3, C++ 3) and 10 Terminal-Bench 2.0 tasks (3 easy, 6 medium, 1 hard), run with
[Harbor](https://github.com/laude-institute/harbor). Two runs, a week apart:

| run | server | Claude Code | sessions at once |
|---|---|---|---|
| A | `--max-num-seqs 4` | 2.1.286 | 4 |
| B | `--max-num-seqs 6` | 2.1.295 | 6 |

## Results

| setup | run A: first attempt / with a second chance | run B: first attempt / with a second chance |
|---|---|---|
| Claude Code alone (Harbor `claude-code` agent), Aider polyglot | 12/20 / 14/20 (independent retry) | 11/20 / 12/20 (independent retry) |
| Claude Code alone, Terminal-Bench 2.0 | 9/10 / 10/10 (independent retry) | 8/10 / 10/10 (independent retry) |
| `qwen-agent --until-done`, Aider polyglot | 10/20 / **18/20** (second round sees the test failures) | 10/20 / **18/20** |

In this setup the same model and the same exercises went from 12-14/20 to 18/20 with a
supervisor that runs the hidden tests after a round and hands the failures back (Aider's own
protocol: one attempt, then one more with the test output). The gain is the second round:
the loop's first round scored 10/20, below Claude Code alone, and the two setups also differ
in role rules, shell access and the appended instruction. Zero harness failures in the final
runs: no API errors, timeouts, context overflows or crashes. Run B reproduced the loop's
result exactly (10/20, then 18/20, the same two exercises failing). In run B the 10 exercises
that passed the first round spent their second round on the coder's review round ("try to
break your change"), which broke none of them; one Harbor trial was excluded for fetching the
exercise's upstream tests.

## How the runs were kept honest

- **No answer keys.** Given network access, the model went looking for the exercises'
  upstream tests and reference solutions (`curl`, `WebFetch`, `git clone`). The final runs
  disable `WebFetch`/`WebSearch` and block the hosts that serve them; every transcript is
  scanned for attempts (all blocked). Terminal-Bench graders download a tool from a GitHub
  release, so `github.com` stays reachable there; raw files, archives and the API do not.
- **All tests count.** Harbor's Aider adapter runs only the first test case for C++, Rust and
  JavaScript (Exercism marks the rest as skipped). The runs here enable every case; the
  reference solutions pass all 20 exercises under the patched graders.
- **Not leaderboard numbers.** A 20-exercise sample, tests hidden from the model, one
  machine. Harbor attempts are independent; the `--until-done` coder's only shell grant
  was `qwen-test`, set up to compile its code (plus the read-only commands Claude Code
  allows inside the working directory), while Harbor's agent had a full shell. The Harbor runs
  append a short, generic instruction (hidden tests will run, network to code hosts is
  blocked, check your output against the spec, do not add unrequested leniency).

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
rate was 67-75%, with no preemptions in any run.

For how a Qwen chat template can cost vLLM most of its prefix cache under Claude Code,
and the fix, see [`vllm.md`](vllm.md).
