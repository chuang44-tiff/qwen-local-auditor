# Interactive sessions: `qwen-agent --interactive` and `qwen-cc`

The headless modes return a result; sometimes you want to type into the local model
instead. `qwen-agent --interactive` opens a real interactive Claude Code on your server,
and `qwen-cc` runs that inside tmux so a session can be opened, read, typed into and
stopped from another Claude Code session without anyone attaching a terminal. The router
skill (`skill/local-agent/SKILL.md`) is what teaches your session to drive it.

## `qwen-agent --interactive`

```bash
qwen-agent --interactive -C ./proj
```

It runs preflight and resolves the config like any other run, then execs an interactive
`claude` (no `-p`) on your server, in the `-C` directory (default: the current one): the
same model, context window, effort and scrubbed environment as a headless run, and **no
tool fence at all**: no `--tools`, `--allowed-tools`, `--restricted`, `--permission-mode`,
`--output-format`, `--append-system-prompt` or `--strict-mcp-config` is passed, because
the person at the keyboard answers Claude Code's own permission prompts. `--timeout` does
not apply; the person ends the session.

It takes no prompt, and refuses every flag that belongs to a headless run and would be
silently dropped here: a prompt, `-f`, `--stdin`, `--until-done`, `--test`, `--write`,
`--all-tools`/`--unrestricted`, `--toolset`, `-t`/`--tools`, `--web`, `--subagents`,
`--json`, `-o`, `-w`, `--resume`, `-r`/`--role`, `--role-file`, `-s`/`--system`,
`--read-only`, `--strict-mcp`. `--dry-run` is allowed and prints the exact command line
and child environment with the auth token redacted.

On Windows, where there is no tmux, this is the command to run in a terminal.

## `qwen-cc`

```bash
qwen-cc ~/proj                     # a detached session: prints session: and attach:
qwen-cc --list                     # the sessions qwen-cc created, and only those
qwen-cc --peek NAME 100            # what it is doing, and what it is asking
qwen-cc --say NAME "run the tests" # type a line into it, then Enter
qwen-cc --stop NAME                # Ctrl-C and /exit; --stop --force NAME kills it
```

### Launch

`qwen-cc [DIR] [window flags] [-- QWEN_AGENT_ARGS...]` starts a detached tmux session
running `qwen-agent --interactive -C DIR [QWEN_AGENT_ARGS...]`. DIR defaults to the
current directory and must exist. Everything after `--` goes to `qwen-agent`, e.g.
`-- --model other-model --effort medium`.

The session is named `qwen-<dir>-<HHMMSS>` (non-alphanumerics in the directory name
become `-`; `-2`, `-3`, ... is appended when the name is taken) and tagged `@qwen_cc=1`,
which is what marks it as `qwen-cc`'s. A launch prints:

```
session: NAME
attach:  tmux attach -t NAME
```

Over SSH there is no desktop to open a window on, so a launch also prints a `remote:`
line (`ssh -t user@host tmux attach -t NAME`) to paste into a terminal on your own
machine. The first session in a folder stops at Claude Code's "trust this folder?"
prompt; attach and answer it yourself.

| window flag | effect |
|---|---|
| default | open a terminal attached to the new session when `DISPLAY` or `WAYLAND_DISPLAY` is set. The first opener found is used: `gnome-terminal -- tmux attach -t NAME`, else `x-terminal-emulator -e tmux attach -t NAME`; on macOS, Terminal.app through `osascript`. With no opener: `window: none (attach with the command above)` |
| `--window` | open one even with no display set |
| `--no-window` | do not. What a headless caller wants: it reads the pane with `--peek` instead |
| `--dry-run` | print the tmux and window commands instead of running them |

### Read, type, stop

| command | what it does |
|---|---|
| `qwen-cc --list` | the name of every `qwen-cc` session, one per line |
| `qwen-cc --peek NAME [LINES]` | the session's pane: its last LINES lines of scrollback plus what is on screen (default 60) |
| `qwen-cc --say NAME TEXT...` | type TEXT literally into the session and press Enter |
| `qwen-cc --stop NAME` | Ctrl-C, then `/exit` and Enter, and wait up to 10 s; prints `stopped NAME` or `still running: NAME (use --stop --force)` |
| `qwen-cc --stop --force NAME` | `tmux kill-session` instead of asking |

One mode's flags stay in their mode: `--window`, `--no-window` and `--dry-run` belong to
a launch, `--force` to `--stop`, and any of them given to another mode is refused instead
of quietly ignored.

## Safety

`--peek`, `--say` and `--stop` refuse a session this script did not create (one that
does not exist, or whose `@qwen_cc` is not 1) and exit 2 naming it. What `qwen-cc` cannot
limit is what a typed line does: `--say` is how an interactive session's permission
prompts get answered, so that stays a human decision. The skills tell your Claude Code
session to use `--say` for what the user asked it to hand over and for nothing else, and
never to answer a permission prompt unless the user explicitly asked for that prompt to be
accepted.

tmux is required. Windows has none: run `qwen-agent --interactive` directly in a terminal
there instead.
