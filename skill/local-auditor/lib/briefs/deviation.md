You are checking changed code against a SPEC.

For each `t<N>.md` there is a `context<N>.md` with the changed lines (real line numbers),
the spec, the decision log, and extracted transcript evidence.

This is EXTRACTION. For each item:
1. Quote any spec text the changed lines do NOT follow, and cite the line.
2. If such a departure exists, find the decision-log entry (D-number) or transcript line
   that records it. Report what reason was recorded. Do not judge whether it was a good idea.
3. If a reason cites a test, re-run that test with `qwen-test <id>` and paste its first
   line (TEST ... PASSED/FAILED) into EVIDENCE.

VERDICT is one of:
- MATCHES_SPEC: the changed lines follow the spec.
- DEVIATION_EXPLAINED: a departure exists AND a recorded reason exists. EVIDENCE must
  include the re-run TEST line.
- DRIFT_UNEXPLAINED: a departure exists and nothing records why.
- CANNOT_DETERMINE: the context does not let you answer.

Emit exactly {{N}} blocks, one per key, in order, and nothing else. The keys are:
{{ITEMS}}

```
## <key>
VERDICT: <one of the four>
FINDING: <one line>
EVIDENCE: <path:line, D-number, TEST line>
WHY: <the spec text, what the code does instead, and the recorded reason if any>
```
