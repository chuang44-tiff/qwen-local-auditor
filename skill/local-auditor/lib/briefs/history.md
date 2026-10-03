You are reading what past Claude Code sessions recorded about some files.

For each `t<N>.md` there is a `context<N>.md` holding EXTRACTED transcript lines, each
headed `[session:<id> <time>]`. You have no other access.

This is EXTRACTION. For each item, report the decisions the lines show: what was asked,
what was changed, which test result drove a change, and the stated reason. Cite the
`[session:... time]` headers. Do not judge whether a decision was right.

Emit exactly {{N}} blocks, one per key, in order, and nothing else. The keys are:
{{ITEMS}}

```
## <key>
FINDING: <one line: the decision that matters most for this file>
EVIDENCE: <session:<id> <time>, ...>
WHY: <the recorded reason, quoted where possible; "no reason recorded" if none>
```
