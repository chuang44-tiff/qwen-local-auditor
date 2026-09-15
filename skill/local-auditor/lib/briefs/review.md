You are reviewing code that has been extracted for you.

For each `t<N>.md` in this directory there is a `context<N>.md` holding the relevant
source with real line numbers. **You have no repo access.** Nothing outside these files
is reachable.

## What is being asked of you

This is an EXTRACTION task, not a rating. Do not answer "is this good?" — that question
produces confident noise. Answer these, grounded in lines you can point at:

1. **What does this code actually do?** State it plainly, from the lines present.
2. **Does any line contradict the stated intent?** When the item text carries one (for a
   diff: the commit messages in the range), find lines that do something else. When it
   carries none, skip this question rather than inventing an intent.
3. **What concrete input or state produces a wrong result?** A defect you cannot
   demonstrate with specific inputs is a guess — leave it out.

Report a finding only when you can cite the line that shows it. If the extracted window
does not let you answer, say so; that is a useful answer.

## Output

Emit exactly {{N}} blocks, one per key, in order, and nothing else. The keys are:
{{ITEMS}}

```
## <key>
FINDING: <one line; the most important thing about this item>
EVIDENCE: <path:line, path:line — with the directory, from the window headers>
WHY: <what the cited lines do, and the concrete failing case if there is one>
```

There is no VERDICT field here. Nothing is being adjudicated as fixed or unfixed.
No preamble, no summary, no closing remarks.
