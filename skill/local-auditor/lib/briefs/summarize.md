You are digesting text that has been chunked for you.

Each `t<N>.md` names a chunk and `context<N>.md` holds it. **You have no other access.**

## What is being asked of you

Extraction, not opinion. For each chunk:

1. **What is in it** — the distinct events, errors, or topics present, not an impression.
2. **What recurs** — anything appearing more than once, with a count if you can give one.
3. **What stands out** — the lines a human would want to see first, quoted exactly.

Quote lines rather than paraphrasing them. If a chunk is unremarkable, say so in one
line; padding a summary to look thorough makes it useless.

## Output

Emit exactly {{N}} blocks, one per key, in order, and nothing else. The keys are:
{{ITEMS}}

```
## <key>
FINDING: <one line: what this chunk is>
EVIDENCE: <quoted lines, or line numbers where the chunk has them>
WHY: <what recurs, what stands out, what a human should look at first>
```

No preamble, no summary, no closing remarks.
