---
name: local-sweep
description: Run the same read-only local-model question over many files or items at once with `qwen-sweep`: every file matching a glob, every file in a diff, every claim in a set of documents, chunks of a large log, a whole-repo audit, or `--builder deviations` / `history` over many files. Trigger on "sweep these files", "summarise this log", "audit the whole repo", "review every file". For one file, one diff or one question, use local-auditor instead.
---

# Local sweep

```bash
qwen-sweep --builder files      --repo . --glob 'src/**/*.py' --dry-run   # always dry-run first
qwen-sweep --builder diff       --repo . --base main --test
qwen-sweep --builder deviations --repo . --base main --arg spec=docs/spec.md
qwen-sweep --builder history    --repo . --arg files=src/net.py,src/retry.py
```

- `--test` lets every batch run tests through `qwen-test` (needs `QWEN_TEST_CMD`).
  `--builder deviations` implies it.
- A big repo is just a big glob: batches are sized to the model's window automatically.
- Batch EDITS are not a sweep job: use `local-coder` with one checklist item per file.

Read `local-auditor`'s `reference/sweep.md` for mechanics and `reference/limits.md`
before acting on any verdict.
