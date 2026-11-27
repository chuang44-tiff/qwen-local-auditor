# Agents talking back: share, ask, ask_main, confinement

A run is not silent. The main session can steer an agent, ask it how far along it is, and
answer its questions; units of one swarm phase can tell each other what they have found;
swarm units that run Bash are confined so they cannot plant notes for their siblings. This
page is the map of those channels, their limits, and what confinement cannot do.

## The four directions

**Main to agent** -- `qwen-agent --send ADDRESS TEXT` queues a note; the agent reads it as a
plain user message at its next model call and keeps working. The address is the 12-hex run
id the `watch:` line prints, or `<run id>/<unit>` for a swarm unit
(`reference/qwen-agent.md`, "Steer a running agent").

**Main asks an agent** -- `qwen-agent --ask ADDRESS TEXT` queues a question and prints the
agent's answer when it comes:

    qwen-agent --ask 1a2b3c4d5e6f "how far along are you?"
    asked 1750953101234567890-ab12cd
    answer from 1a2b3c4d5e6f (14:02:11):
    Two of five probes are done; a shared finding settled the third.

The answer is what the agent sends with its `reply` tool; an agent that never calls it never
answers. Exits: 0 the answer was printed · 10 no answer yet (the wait ran out; it may still
be answered later) · 11 the agent ended without answering · 3 the run or address is
unavailable · 2 usage. `--wait SECONDS` (1..3600, default 600) caps the wait; it goes
before or after the address and text (`--ask --wait 300 ADDRESS TEXT`, `--ask ADDRESS TEXT --wait 300`).
`qwen-agent --answer ADDRESS NOTE_ID` fetches the answer later by the ask's id -- also after
the run ended, for an answer that arrived late; an unknown id exits 3.

**Agent asks the main session** -- until-done rounds and swarm units get the `ask_main` tool
(a unit fenced to the web or a browser gets none: its context holds fetched page text). A
question becomes a `q<N>.md` ask file, and the main session picks it up with the very same
`qwen-agent --next-ask ID` that serves failing-round asks (`reference/ask-main.md`). The
agent never waits for it: an answer, if any, arrives as a note. Each agent may hold one open
question, and a run admits at most `QWEN_ASK_MAIN_MAX` in total (default 5, clamped to 0..20); a question that
is dropped is reported and the agent decides for itself. The run's report counts them on the
`questions: admitted N, dropped N, answered N` line. A question is heard only while a
`--next-ask` waits on the run: one asked with nobody listening is dropped (`no main
session is listening`), so start `--next-ask` in the background as soon as the run's id is known.

**Agent to its siblings** -- the units of a fan-out that opted in (`share=True` on
`wf.fan_out`; debug's `probe`, optimize's `implement` and research's `fetch` do) get the
`share` tool. A shared finding reaches every running sibling of the same phase as a note
naming the sender, and a unit that starts later in the phase gets it as backlog. report.md
lists every finding, its sender, the full text and the units it reached, under
`## Shared findings`.

Two caps bound sharing: `QWEN_SHARE_MAX` (default 3, 0..5) is each sender's budget of findings
per phase, and `QWEN_SHARE_IN_MAX` (default 10, 0..15) is each receiver's budget of share notes
per attempt. Every note of every kind -- shared findings, harness notes and main's own -- counts
against one per-attempt delivery budget (`QWEN_NOTE_MAX_PER_RUN`, default 20); past it the remaining
queued notes, whoever sent them, move to `undelivered/` with reason `cap`, so under the defaults
about ten slots are left for main. A finding is text, nothing more: it stops nothing, grants
nothing, and a sibling weighs it against its own evidence. Text sizes are capped too: a finding
1500 characters (sharing the identical text twice is refused), a reply 4000, a question 800 with
at most four options of 80 characters, a note 8000 characters / 32 KiB.

## Sharing and the cache

A unit that RECEIVED a shared finding is not cached: its answer was steered by text that
arrived mid-run, so its next run is a real rerun. The same holds for an attempt that received a
`--send` note or an `--ask` question (harness notes alone do not count); a share sticks across
the unit's retries, main-note steering clears on a clean retry. A unit that only SENT findings
is cached normally. On `--resume`, an opted phase replays its findings as backlog to every
rerunning unit -- an early share can make most of that phase rerun once.

## One switch turns it all off

`QWEN_STREAM=0` starts an agent with no inbox, and without an inbox none of this exists: no
message dir, no `share`, `reply` or `ask_main` tools, no questions, no delivery markers. The
run launches exactly as it did before the messaging features.

## Confinement of Bash units

A swarm unit whose grants include Bash (debug's `prober`, optimize's `implementer`) runs
confined, so it cannot forge the channels above: its Bash works inside a sandbox, its Edit
and Write grants are scoped to the unit's sandbox folder, and deny rules keep Edit and Write
out of the run's `inbox/`, `msg/`, `asks/` and `agents/` folders whatever the platform.
Where the host cannot confine at all, the scoped grants and deny rules still apply and the
run's report says `bash not confined: <reason>`.

Confinement is probed once per run, at the first Bash unit. When the probe passes, a unit's sandbox
is mandatory: it never falls back to an unsandboxed shell -- the settings block sets
`failIfUnavailable`, so a sandbox that cannot start ends that unit's session instead. Each confined
unit also gets a private Claude temp folder named by `CLAUDE_CODE_TMPDIR` (a `qla-u-*` under `/tmp`);
one that cannot be made drops the unit rather than run it unconfined, and leftovers of killed runs
are swept at the next swarm start after 48 hours. The reasons a host cannot confine are
`no bash sandbox on this platform` (Windows, or any platform other than Linux and macOS),
`run folder under TMPDIR`, `sandbox probe failed: bwrap or socat missing`, or the probe's own error;
the first Bash unit prints `swarm: bash not confined: <reason>` on stderr, and
`bash not confined: <reason>` closes report.md.

## What confinement cannot do

- A confined unit has no outbound network. Package installs inside one fail; rely on tools
  already on the machine.
- A server started in one Bash call is unreachable from the next: each call is a fresh
  sandbox, so a test cannot start the app in one call and query it in the next.
- A run folder under TMPDIR cannot be confined (the sandbox always lets Bash write there,
  and that would include the run folder itself): the report says
  `bash not confined: run folder under TMPDIR`. Keep run folders outside TMPDIR.
- macOS: confinement is wired the same way but not yet verified on a real host.
- Windows: there is no Bash sandbox; units get the deny rules and the scoped grants only,
  and the report says `bash not confined: <reason>` there too.

## The risk that remains

Every channel above is a file, and every agent runs as the same OS user as its runner. Reads stay
open: an agent's own Bash can read its siblings' note files. An unconfined unit -- a platform without
the sandbox, or a run whose probe failed -- can forge notes, findings and questions: a note file names
its sender as plain text, so a planted note can read as a unit's finding, as main's note or as the
harness's. A delivered shared finding is quoted line by line under a header naming the sender; plain
notes, questions and harness notes arrive unquoted under theirs. The report shows every finding with
its sender and every message file stays on disk to audit: forgeries are findable afterwards, not
blocked.

Where the files live: an until-done loop or swarm run keeps them in its run folder (`RUN/inbox/<unit>/`, `RUN/msg/units/<unit>/`,
`RUN/asks/`); a single-shot agent has no run folder, its two folders are `<run id>.inbox` and `<run id>.msg` under `$XDG_CACHE_HOME/qwen-agent/watch`.