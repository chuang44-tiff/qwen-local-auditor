"""Readable run titles: lib/watch.py's describe() and the two file-backed launchers.

The until-done loop and the single-shot `-f FILE` run are titled for what the file
ASKS FOR -- the short description describe() takes from its own text -- with the
file's name kept at the end for identification: "<description> (<file name>)" for
the loop, "<role>: <description> (<file name>)" for the single shot. Either falls
back to the plain file-name title when describe() returns "", and everything else
(inline prompts, `-f -`, the swarm/research goal titles, hand-written pointers)
keeps today's title; the single-shot ones are pinned end to end against the real
launcher here.

The page is switched on for this file (like tests/test_watch_hooks.py) with the
registry pinned under each test's tmp_path and QWEN_WATCH_NOSERVE=1, so the suite
never touches the real watch dir or port.
"""
import json
import os
import subprocess

import pytest

from lib import supervisor, watch
from test_cli import AGENT, BASH, base_url, posix, run, server, fake  # noqa: F401
from test_supervisor import env, script, sup  # noqa: F401  (used by name as fixtures)


@pytest.fixture(autouse=True)
def _watch_home(tmp_path, monkeypatch):
    """The registry under this test's tmp_path (so a registered run -- the supervisor's
    or the launcher's -- never lands in the developer's real watch dir), no real config
    file able to move host or port, and none of a previous test's register() stamps
    left in the environment (register() writes them into os.environ, and the fake
    agent script name is handed straight to os.environ by test_supervisor.script)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "watch-cache"))
    monkeypatch.setenv("QWEN_CONFIG", str(tmp_path / "no-such-config"))
    for name in ("QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT", "QWEN_WATCH_ID"):
        monkeypatch.delenv(name, raising=False)
    yield
    for name in ("QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT", "QWEN_WATCH_ID",
                 "FAKE_AGENT_SCRIPT"):
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def _watch_on(monkeypatch):
    """The page is on (the session has it off) and the viewer is never spawned."""
    monkeypatch.setenv("QWEN_WATCH", "1")
    monkeypatch.setenv("QWEN_WATCH_NOSERVE", "1")


def pointers():
    """Every pointer the run left in the registry, one dict each (the same by-name
    filter test_watch_hooks.py applies: only *.json files name runs)."""
    d = watch.watch_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(d, name), encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


def _file(tmp_path, name, data):
    """A file with these exact bytes (str or bytes) and its path."""
    p = tmp_path / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return str(p)


def launcher_env(tmp_path, server, fake, extra=None):  # noqa: F811  (the fixture names)
    """The launcher-child env test_cli.run() builds from scratch -- replicated for the
    one call that needs its own stdin (`-f -`), which run() cannot give."""
    child = {k: v for k, v in os.environ.items()
             if not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
    child["QWEN_CONFIG"] = posix(tmp_path / "no-such-config")
    child["QWEN_NO_NOTICE"] = "1"
    child["QWEN_DEPTH"] = os.environ.get("QWEN_DEPTH", "shallow")
    child["QA_SUPERVISED"] = "0"
    child["FAKE_RECORD"] = posix(tmp_path / "record.txt")
    child["QWEN_WATCH"] = "1"
    child["QWEN_WATCH_NOSERVE"] = "1"
    if server is not None:
        child["QWEN_BASE_URL"] = base_url(server)
    if fake is not None:
        child["QWEN_CLAUDE_BIN"] = posix(fake)
    child.update(extra or {})
    return child


def run_with_stdin(tmp_path, args, server, fake, stdin_text, extra=None,  # noqa: F811
                   timeout=90):
    """test_cli.run with stdin wired: what `-f -` needs."""
    argv = [BASH, posix(AGENT), *[str(a) for a in args]]
    p = subprocess.Popen(argv, env=launcher_env(tmp_path, server, fake, extra),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                         cwd=str(tmp_path))
    try:
        out, err = p.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


# ------------------------------------------------------------------ describe()


def test_describe_goal_section(tmp_path):
    p = _file(tmp_path, "t.md",
              "# Goal\nMake the value good.\n\n- [ ] value is good -- check: test ALL\n")
    assert watch.describe(p) == "Make the value good"


def test_describe_goal_heading_any_level_and_case(tmp_path):
    p = _file(tmp_path, "t.md", "####   GOAL\nShip the watcher nightly.\nignored too\n")
    assert watch.describe(p) == "Ship the watcher nightly"


def test_describe_keeps_underscores_inside_words(tmp_path):
    # emphasis is stripped where it IS emphasis -- a paired delimiter; an underscore
    # inside a word is literal text and stays (and a lone * is no emphasis either)
    p = _file(tmp_path, "t.md", "Fix the check_no_leak script and the *risky* path.\n")
    assert watch.describe(p) == "Fix the check_no_leak script and the risky path"


def test_describe_first_prose_line_skips_front_matter_headings_and_code(tmp_path):
    p = _file(tmp_path, "t.md",
              "---\ntitle: Front Matter\n# not a heading here\n---\n\n"
              "# Setup\n\n```bash\nmake all\n```\n\n"
              "Build the report from the logs. Everything after this is noise.\n")
    assert watch.describe(p) == "Build the report from the logs"


def test_describe_headings_only_fall_back_to_the_first_heading(tmp_path):
    p = _file(tmp_path, "t.md", "---\ntitle: x\n---\n\n## Audit **the parser**\n\n### More\n")
    assert watch.describe(p) == "Audit the parser"


def test_describe_cleans_markdown(tmp_path):
    p = _file(tmp_path, "t.md",
              "## Goal\n\n- Rewrite the **[cache layer](https://x.example/a)** `get` path\n")
    assert watch.describe(p) == "Rewrite the cache layer get path"


def test_describe_cuts_at_the_first_sentence(tmp_path):
    p = _file(tmp_path, "t.md", "Start the server. Then run `pytest -q` against it.\n")
    assert watch.describe(p) == "Start the server"
    p = _file(tmp_path, "q.md", "Why does it fail? Because reasons.\n")
    assert watch.describe(p) == "Why does it fail?"      # only a trailing . or : goes


def test_describe_cuts_long_text_at_a_word_boundary_with_an_ellipsis(tmp_path):
    text = ("Repair the broken report formatter so every until-done loop on the"
            " watch page reads correctly now")
    p = _file(tmp_path, "t.md", text + "\n")
    assert watch.describe(p) == "Repair the broken report formatter so every until-done loop…"


def test_describe_cuts_a_long_single_word_at_the_limit(tmp_path):
    p = _file(tmp_path, "t.md", "w" * 70 + "\n")
    assert watch.describe(p) == "w" * 60 + "…"


def test_describe_honours_the_limit_argument(tmp_path):
    text = ("Repair the broken report formatter so every until-done loop on the"
            " watch page reads correctly now")
    p = _file(tmp_path, "t.md", text + "\n")
    assert watch.describe(p, limit=20) == "Repair the broken…"


def test_describe_unreadable_file_is_empty(tmp_path):
    assert watch.describe(str(tmp_path / "nope.md")) == ""
    assert watch.describe(str(tmp_path)) == ""           # a directory: still an OSError
    locked = _file(tmp_path, "locked.md", "# Goal\nSecrets here.\n")
    if os.name == "posix" and os.getuid() != 0:
        os.chmod(locked, 0)
        try:
            assert watch.describe(locked) == ""
        finally:
            os.chmod(locked, 0o644)


def test_describe_reads_only_the_first_64_kib(tmp_path):
    # unclosed code fence: nothing in the first 64 KiB is prose at all, and what
    # stands past the cap (a real Goal section) is never read
    p = _file(tmp_path, "deep.md",
              ("```\n" + "x = 1\n" * 20000 + "```\n# Goal\nToo deep to read.\n").encode())
    assert watch.describe(p) == ""


def test_describe_reads_crlf_lines_cleanly(tmp_path):
    # a Windows-written task file: the \r never reaches the description
    p = _file(tmp_path, "crlf.md", b"# Goal\r\nMake the value good.\r\n")
    assert watch.describe(p) == "Make the value good"


def test_describe_replaces_undecodable_bytes(tmp_path):
    p = _file(tmp_path, "latin.md", b"# Goal\nCaf\xe9 works fine.\n")
    assert watch.describe(p) == "Caf� works fine"   # the bad byte reads as U+FFFD


def test_describe_empty_and_fenced_only_files(tmp_path):
    assert watch.describe(_file(tmp_path, "empty.md", "")) == ""
    assert watch.describe(_file(tmp_path, "blank.md", "\n\n \n")) == ""
    assert watch.describe(_file(tmp_path, "front.md", "---\ntitle: x\n---\n")) == ""


def test_describe_empty_goal_section_falls_back_to_the_heading(tmp_path):
    # nothing after the Goal heading: no such line, but there IS a heading -- its own
    # text (the `#`s gone) becomes the description
    p = _file(tmp_path, "t.md", "#### GOAL\n\n\n")
    assert watch.describe(p) == "GOAL"


def test_describe_goal_section_takes_its_first_nonempty_line_literally(tmp_path):
    # the Goal source is "the first non-empty line after it", taken as it stands:
    # an (uncleaned) heading line there is the description's raw material
    p = _file(tmp_path, "t.md", "# Goal\n## Details\nBody prose\n")
    assert watch.describe(p) == "## Details"


# ------------------------------------------------------------------ until-done


def test_until_done_title_comes_from_the_task_file(tmp_path):
    task = _file(tmp_path, "task.md", "# Goal\nTidy the watch titles.\n\n- [ ] x\n")
    assert supervisor._watch_title(task) == "Tidy the watch titles (task.md)"


def test_until_done_title_falls_back_to_the_file_name(tmp_path):
    task = _file(tmp_path, "task.md", "---\ntitle: nothing usable\n---\n")
    assert supervisor._watch_title(task) == "task.md"
    assert supervisor._watch_title(str(tmp_path / "missing.md")) == "missing.md"


def test_until_done_registers_the_description_title(env):  # noqa: F811
    """End to end: the real loop on the real page titles itself for what the task
    file's text says (the test_supervisor env fixture's task.md is a `# Goal`
    section) with the file's name kept at the end."""
    repo, task, tmp = env
    script(tmp, [{"result": "tried"},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--max-rounds", "3") == 0
    ps = pointers()
    assert len(ps) == 1, ps
    assert ps[0]["kind"] == "until-done"
    assert ps[0]["title"] == "Make the value good (task.md)"


# ------------------------------------------------------------------ single-shot


ON = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1"}


def test_single_shot_dash_f_title_uses_the_file_description(tmp_path, server, fake):  # noqa: F811
    ask = tmp_path / "the task.md"                       # a space in the name, as any file may
    ask.write_text("# Goal\nRebuild the index before the migration runs.\n", encoding="utf-8")
    r = run(tmp_path, ["-r", "tester", "--no-timeout", "-f", posix(ask)],
            server, fake, extra=dict(ON))
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1, ps
    assert ps[0]["kind"] == "agent"
    assert ps[0]["title"] == ("tester: Rebuild the index before the migration runs"
                              " (the task.md)")


def test_single_shot_dash_f_falls_back_to_the_file_name(tmp_path, server, fake):  # noqa: F811
    ask = tmp_path / "blank.md"                          # nothing readable: file name only
    ask.write_text("---\ntitle: x\n---\n", encoding="utf-8")
    r = run(tmp_path, ["-r", "tester", "--no-timeout", "-f", posix(ask)],
            server, fake, extra=dict(ON))
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1, ps
    assert ps[0]["title"] == "tester: blank.md"


def test_single_shot_dash_f_stdin_keeps_todays_title(tmp_path, server, fake):  # noqa: F811
    """`-f -` reads the prompt from stdin -- no file to describe, so the title stays
    what it has always been for that mode: the role and the file-name token `-`."""
    r = run_with_stdin(tmp_path, ["-r", "tester", "--no-timeout", "-f", "-"],
                       server, fake, "pipe the notes into the session prompt\n")
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1, ps
    assert ps[0]["title"] == "tester: -"


def test_single_shot_inline_prompt_keeps_todays_title(tmp_path, server, fake):  # noqa: F811
    ask = "fix the bug in the loader and add a regression test"
    r = run(tmp_path, ["-r", "tester", "--no-timeout", ask], server, fake, extra=dict(ON))
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1, ps
    assert ps[0]["title"] == "tester: " + ask
