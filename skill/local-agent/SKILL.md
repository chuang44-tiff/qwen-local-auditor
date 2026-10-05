---
name: local-agent
description: Use when the user wants work done by the local, LAN or offline model and has not said which kind of work. This skill only routes: it runs a preflight, then hands off to local-coder, local-auditor, local-sweep, local-deep-research or local-swarm. Trigger on "local agent", "use the local model", "have qwen do it", "run it locally", "offline model".
---

# Local agent (router)

## 1. Preflight, every session

```bash
qwen-agent --preflight-only
```

Exit 0 means usable. Anything else: report the message and stop.

## 2. Pick the sub-skill, then load it with the Skill tool

| the job | load |
|---|---|
| Make code changes against a spec until checks pass; fix failing tests | `local-coder` |
| Review code, a diff or a repo; second opinion; explain why code differs from a spec; write a reproduction test | `local-auditor` |
| The same read-only question over many files or items at once | `local-sweep` |
| A question answered from the web: research it, deep research it, fact-check it against sources | `local-deep-research` |
| Debug a bug down to a checked patch; an overnight multi-agent run; a custom swarm workflow | `local-swarm` |
| Open an interactive session the user types into themselves | no skill: `qwen-cc` (section 3) |

Load one at a time. If a job is two of these (fix then review), run them in order:
`local-coder` first, then `local-auditor` on its diff.

## 3. Launch: a session the user can type into

When the user wants to watch or steer the model rather than get a report back, open an
interactive session for them with `qwen-cc` (tmux; no fence, because the person at the
keyboard answers Claude Code's own permission prompts):

```bash
qwen-cc ~/proj                        # detached session; prints session: and attach:
qwen-cc --list                        # the sessions this opened
qwen-cc --stop NAME                   # end it (--stop --force NAME kills it)
```

Report the `attach:` line it prints verbatim — that is how the user joins. When the user
is on SSH it also prints a `remote:` line to paste into
a terminal on their own machine; report that too. The first launch in a folder stops at
Claude Code's "trust this folder?" prompt: tell the user to attach and answer it. Then:

| to | run |
|---|---|
| report what it is doing, or what it is asking | `qwen-cc --peek NAME 100` |
| hand it something the USER told you to hand it | `qwen-cc --say NAME "run the tests"` |
| end it | `qwen-cc --stop NAME` |

NEVER answer one of the session's permission prompts with `--say` unless the user
explicitly asked you to accept that prompt: the permission decision is theirs. `--peek`,
`--say` and `--stop` refuse a session `qwen-cc` did not create. Windows has no tmux — run
`qwen-agent --interactive` in a terminal there instead.

## 4. What stays the same in all of them

- Read-only is the default; writing and tests are explicit flags.
- A plain `qwen-agent` run gets no shell: `qwen-test` is the one command `--test` grants it
  (an interactive `qwen-cc` session is the user's own Claude Code, with its normal
  permission prompts). A `local-swarm` workflow chooses its tools per role's fence instead:
  a `sandbox` agent gets a shell inside a throwaway clone of the target, and a `read` agent
  gets read-only tools (Read, Glob, Grep) in the target itself. Neither ever writes the
  target — a patch is a file you apply with `git apply`.
- Ask for extraction, not judgment (see local-auditor's `reference/limits.md`).
- Judge results by content, not exit code.
- `local-deep-research` (and a `local-swarm` workflow with `search` or `web` roles) is the
  only job that needs the internet (search plus fetched pages); every other route runs
  without it — but a swarm `sandbox` agent has a shell, and that shell is the user's own,
  with their network.
