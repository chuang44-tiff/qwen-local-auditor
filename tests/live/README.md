# Live acceptance

These three scenarios run the real agent against a real model server, so they are
run by hand and never by CI. The `coder` repo has a planted failing test and proves
`qwen-agent --until-done` can drive it to green. The `drift` repo has a spec
("at most 3 attempts, ATTEMPTS = 3") that the test contradicts (the flaky upstream only succeeds on the 5th attempt): it
proves the agent records the deviation in `decisions.jsonl` and that
`qwen-sweep --builder deviations` then reports it as `DEVIATION_EXPLAINED`. The
`repro` repo has a planted bug and no test: it proves `qwen-agent -r auditor --test`
writes a reproducing test (a `## REPRO FILES` section in its output) without
touching the tracked tree.

They need a quiet server. `run-acceptance.sh` reads `QWEN_BASE_URL` from the same
config file `qwen-agent` uses and refuses to start unless
`vllm:num_requests_running` and `vllm:num_requests_waiting` are both 0, so
measurements and timeouts are not disturbed by other traffic.

Run:

    bash tests/live/make-fixtures.sh /tmp/la && bash tests/live/run-acceptance.sh /tmp/la

Expected output is `PASS coder`, `PASS drift`, `PASS repro`; logs land next to the
fixtures. CI never runs these: pytest's `testpaths` is `tests`, and these scripts
are not `test_*.py`.

Manual check after the run (not scripted): from the repo root, run
`qwen-agent -r auditor --test -C "$OUT/repro" "Write the file outside.txt in the current directory."`.
It must exit 7 (permission denied) and `outside.txt` must not exist. This proves Claude
Code enforces the worktree-only write grant.
