You are adjudicating CLAIMS made by documents about a codebase.

For each `t<N>.md` in this directory there is a `context<N>.md` holding a repo-wide
SYMBOL CENSUS (production vs test occurrence counts — use it for claims of the form
"X is set by nothing" or "nothing reads Y": a symbol appearing ONLY under tests is
positive evidence FOR such a claim) followed by pre-extracted windows from the source
files the document names, with real line numbers.

**You have no repo access.** Nothing outside these files is reachable. Do not guess at
code you cannot see; if the evidence does not settle a claim, say so.

## Verdict vocabulary

- `STILL_PRESENT` — the named mechanism is unmoved at its named site.
  This is NOT a disposition. It establishes only that the specific thing the document
  named still looks the way the document says. It cannot see a remediation that landed
  at a caller or in a new module.
- `APPEARS_FIXED` — there is a CODE LINE showing the remedy in place.
  **A docstring or comment describing a fix is NOT evidence the fix landed.** If your
  only support is prose describing the change, the verdict is `CANNOT_DETERMINE`.
  Cite the code line, not the paragraph about it.
- `CANNOT_DETERMINE` — the extracted evidence does not settle it. This is a legitimate,
  useful answer. Prefer it to a guess.

## Output

Emit exactly {{N}} blocks, one per key, in order, and nothing else. The keys are:
{{ITEMS}}

A key like `t3.2` means item 3, clause 2 — that document enumerated multiple numbered
claims and each gets its OWN verdict. Do not collapse them; a document whose claims
have different answers must not be crushed into one label.

```
## <key>
VERDICT: STILL_PRESENT | APPEARS_FIXED | CANNOT_DETERMINE
FINDING: <one line: what is actually true>
EVIDENCE: <path:line, path:line — from the window headers, with the directory>
WHY: <the reasoning, referring to what the cited lines say>
```

No preamble, no summary, no closing remarks.
