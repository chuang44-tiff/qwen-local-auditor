# What this lane gets wrong

Measured over ~91 verdicts on one real codebase, with one self-hosted model and one
Claude Code version. The failure MODES below are general; the rates are that setup's,
so re-measure on yours. Read this before acting on any output.

## The one recorded fail-open, and what actually caused it

One `APPEARS_FIXED` on a still-open defect. The model cited a module docstring
*describing* a fix and stopped at the favourable half — two lines above that docstring's
own "closes the MEASURED defect, NOT the class".

For a long time this was written up as a multi-clause item being crushed into one label.
**That was wrong.** The root cause, found later, was deterministic and lived in the
context builder, not the model:

`_is_prose` recognised only a docstring's OPENING delimiter line, so every *subsequent*
line of a multi-line docstring was classified as code — and because the extractor ranks
code hits ABOVE prose hits, the ranking meant to demote docstrings actively **promoted
their bodies into the evidence window**. Measured on one 611-line module: the symbol's
hits were lines [1, 10, 28, 238] and the "code" hits were [10, 28, 238] — all three
inside module docstrings.

Fixed by `prose_mask` in `lib/context.py`, which locates docstrings with `ast`, so a
string used as a *value* still counts as code and only bare string expression statements
are prose.

**Two consequences that remain your problem:**

1. **Require a code line.** The collator now flags any favourable verdict citing a prose
   line ("invariant 8": a favourable verdict must cite code, not a comment or docstring), but that check is fail-safe, not complete — it cannot resolve the
   ~40% of citations that are bare filenames.
2. **Non-Python files are only approximately covered.** C-family files (C/C++, Java,
   JS/TS, Go, Rust, C#, ...) get a `//` and `/* */` comment state machine that ignores
   string literals. Other block-comment languages fall back to `#`-style handling, where
   a comment block describing a fix WILL be ranked as code and promoted. Trust
   non-Python findings less until measured on your code.

## `STILL_PRESENT` is not a disposition

Context is built by grepping around symbols **the item itself names**. So the verdict
establishes only: *the named mechanism is unmoved at its named site.* It cannot see a
remediation that landed at a caller, in a new module, or under a different name. Never
close or escalate on `STILL_PRESENT` alone.

## Empty context is worthless — and now refused

Items yielding no evidence returned `CANNOT_DETERMINE` 100% of the time. The builder now
withholds them with a reason and the engine routes them to `needs-human.txt`.

Measured on one real 72-item corpus: **30 withheld (42%)**. The `windows == 0` test is
stricter than the older `files == 0` — it also catches items that name files their
symbols never appear in.

## Citations

- Reliable when the context carries **path headers**: 35/35 correct.
- Unreliable in free exploration: ~40% are bare filenames with no directory, so any
  consumer needs a resolver and must tolerate failure.
- Path *segments* can be assimilated to neighbours (a `_` becoming `-` to match an
  adjacent hyphenated path). Verify the path, not just the line.

## Success is content, not exit code

A tiny body with none of the expected blocks (~259 bytes when it was observed) is the
autocompact self-destruct, and it has been seen at **both rc=0 and rc=8**. The exit code
is unreliable in both directions -- and so is size alone, because a correct short answer
is small too. `qwen-sweep` checks that every expected block is present, and retries an
incomplete batch once.

## Verdict per clause

Nearly half of one real corpus (18 of 43 dispatched items) enumerates multiple numbered
claims. Collapsing those into one label loses the disagreement between them — which is
the whole signal. The `claims` builder enumerates clauses and each gets its own key and
verdict; do not undo that by asking a single blended question.

## The rule that explains all of the above

**Ask for extraction, not judgment.** Every failure mode here is the harness asking the
model to decide something when it should have asked it to report something.
