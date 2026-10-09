# desktop-driver (DRAFT)

A draft, Windows-only screenshot / click / type driver for one application window,
so that a `qwen-agent` run can do computer use (see issue #2). It is shared to show
what exists. It is not a supported feature, and it has been exercised in one trial only.

## What it is

`guidrv.py` is a single file that uses `ctypes` for `user32` and Pillow for screenshots.
It does not need pywin32. A model agent calls it through a Bash allow-rule that is fenced
to this one script. Each command prints one JSON object. Screenshots are written to files,
and the agent opens them with its Read tool, which is how images reach the model.

## Configure the target

The driver needs a target. Set one or both of these:

| Setting | Meaning |
|---|---|
| `GUIDRV_EXE` | process image name, e.g. `notepad.exe` |
| `GUIDRV_TITLE` | window-title substring (case-sensitive), e.g. `Notepad` |

The same values can be passed for one call as `--exe NAME` or `--title TEXT`, placed before the
command. If neither is set, every command fails with `ok: false`. When both are set, a window
must match both.

## Invocation pattern (qwen-agent)

```
GUIDRV_EXE=notepad.exe GUIDRV_TITLE=Notepad \
qwen-agent --toolset 'Read,Glob,Grep,Bash' \
           -t "Bash(python <path-to>/guidrv.py:*)" \
           <task prompt>
```

- `--toolset` is the real restriction. It sets which built-in tools exist.
- `-t` (`--allowed-tools`) only grants permission. It applies to tools already in the toolset.
- The `:*` suffix means the model can pass any arguments to the driver. The fence limits which
  program runs, not what that program does.
- Add `-D <dir>` for any directory the agent must read, and `--timeout` for a long run.

The task prompt should tell the agent to take a screenshot after every action and to look
at it before the next step.

## Commands

| Command | Purpose |
|---|---|
| `windows` | list visible windows of the target (hwnd, title, rect) |
| `shot <out.png> [hwnd\|-] [maxw]` | screenshot the target window, downscaled to `maxw` (default 1920) |
| `shotrect <out.png> L T R B` | screenshot a physical-pixel screen rectangle |
| `focus [hwnd\|-]` | bring the target window to the foreground |
| `click X Y [hwnd\|-] [right]` | click at window-relative physical pixels |
| `dclick X Y [hwnd\|-]` | double-click |
| `type "text"` | type unicode text into the focused control |
| `key NAME [NAME ...]` | press a key chord, e.g. `key ctrl o`, `key enter`, `key esc` |

## Safety properties

- `PER_MONITOR_AWARE_V2` is set before any coordinate call, so coordinates are physical
  pixels on mixed-DPI displays.
- The `INPUT` struct size is checked at import (40 bytes on x64, 28 on x86). `SendInput`'s
  return count is checked, and a short count fails with `ok: false`.
- Every command prints a JSON object. Any failure, including an unexpected exception, gives
  `ok: false` and exit status 1.
- Every window lookup is limited to the configured process and/or title. An explicit hwnd
  must also be one of the target's own windows.
- `click` reads the cursor back and refuses to send the button event if the cursor is not
  where it was asked to be. It also refuses points outside the target window's rectangle.
- `alt+f4` is refused, for any chord that contains both `alt` and `f4`.
- `type` refuses characters above U+FFFF, which `SendInput` cannot carry in one unit.
- `focus` reports `ok` only when a read-back shows the target in the foreground.
- A sent action is not proof. Take a screenshot after every action and look at it.

## Screenshots and coordinates

- `shot` writes the downscaled PNG and also `<name>_full.png` at full resolution. Its JSON
  output has `scale_to_full`. Multiply coordinates read off the downscaled image by that value
  to get window-relative pixels for `click`.
- Read coordinates off the full-resolution image whenever precision matters.
- Use `shotrect` for tight crops of a small target, such as a toolbar icon. The crop
  coordinates are screen pixels: add the window's `rect` offset from `shot`'s JSON. In the trial,
  bisecting with tight crops is what fixed the click misses on small icons.

## Measured in one trial (issue #2)

- The agent completed a multi-step desktop task in about 9 minutes: it opened a file, opened
  two analysis windows, saved their text output and closed only its own tabs.
- The fence held. One Bash call outside the driver pattern was refused, and the run continued.
- Click accuracy on small toolbar icons was poor. Positions were misread by about 30 px, the
  agent clicked the wrong controls several times (including a Print button), and one tab click
  missed. Print jobs may have been sent to the default printer. This could not be ruled out.
- It was about twice as slow as a Claude Haiku agent on the same kind of task (9 to 11 minutes
  against 4 to 6). This is one run with one model.

## Not done / for the real build

- Forbidden click zones (close, print and similar controls), enforced by the driver
- A dry mode that reports the intended action without sending input
- A `--desktop` mode in qwen-agent: a bundled driver, a run-scoped action log with screenshots,
  and an action budget
- Multi-monitor and negative-coordinate testing
- Non-Windows platforms
- A foreground check for `click`, `type` and `key`. They act on whatever window has focus.

## Caveats

- Bash is not sandboxed. The fence restricts which program runs, not its arguments.
- Input goes to whatever window has focus. Run this only on a machine that nobody is using.
- The driver has been run once, against one application, on one display setup.
