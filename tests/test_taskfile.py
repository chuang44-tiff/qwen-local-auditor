import shlex
import shutil
import subprocess
import sys

import pytest

from lib import checks, taskfile

PY = sys.executable.replace("\\", "/")

TASK = """# Goal
Make retries configurable.

- [ ] retry on 503 -- check: test tests/test_net.py::test_retry_503
- [ ] old api gone -- check: cmd python -c "print(1)"
- [ ] docs updated -- check: none
- [x] changelog entry
"""


def test_parse_items_and_kinds():
    t = taskfile.parse(TASK)
    assert [(i.index, i.kind) for i in t.items] == [(1, "test"), (2, "cmd"), (3, "none"), (4, "none")]
    assert t.items[0].arg == "tests/test_net.py::test_retry_503"
    assert t.items[0].text == "retry on 503"
    assert t.items[1].arg == 'python -c "print(1)"'


def test_crlf_parses_like_lf():
    assert taskfile.parse(TASK.replace("\n", "\r\n")) == taskfile.parse(TASK)


def test_no_items_is_an_error():
    with pytest.raises(ValueError, match="no checklist items"):
        taskfile.parse("# just prose\n")


def test_empty_check_argument_is_an_error():
    with pytest.raises(ValueError, match="item 1"):
        taskfile.parse("- [ ] x -- check: test   \n")


def test_last_check_on_the_line_is_the_check():
    """Item text may quote " -- check: none" without the real check being downgraded
    to UNVERIFIED: the LAST " -- check:" on the line is the check, and everything
    before it -- earlier " -- check:" text included -- is item text."""
    quoted = taskfile.parse(
        "- [ ] the old note reads -- check: none, but now -- check: test tests/net.py::test_retry\n")
    i = quoted.items[0]
    assert (i.kind, i.arg) == ("test", "tests/net.py::test_retry")
    assert i.text == "the old note reads -- check: none, but now"
    later = taskfile.parse("- [ ] a -- check: test first -- check: cmd ls -1\n").items[0]
    assert (later.kind, later.arg) == ("cmd", "ls -1")
    assert later.text == "a -- check: test first"
    # One suffix, unchanged.
    one = taskfile.parse("- [ ] docs updated -- check: none\n").items[0]
    assert (one.kind, one.arg, one.text) == ("none", "", "docs updated")


@pytest.mark.parametrize("line", [
    "- [ ] a -- check: Test a.py\n",    # wrong letter case
    "- [ ] a -- Check: test x\n",
    "- [ ] a --check: test x\n",        # no space after --
    "- [ ] a -- check: tests x\n",      # a word that only starts with a kind
])
def test_unknown_check_kind_is_an_error(line):
    # A typo'd check must NOT become `none`: that would let the item pass
    # without anything ever running.
    with pytest.raises(ValueError, match="item 1: unknown check kind"):
        taskfile.parse(line)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_TEST_WORKTREES", str(tmp_path / "wts"))
    r = tmp_path / "r"
    r.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    (r / "check.py").write_text("import sys\nsys.exit(0 if 'good' in sys.argv else 1)\n")
    (r / "sub").mkdir()
    (r / "sub" / "check.py").write_text("import sys\nsys.exit(0)\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "i"], check=True, capture_output=True)
    return r


def test_run_checks(repo):
    t = taskfile.parse(
        "- [ ] a -- check: test good\n"
        "- [ ] b -- check: test bad\n"
        "- [ ] c -- check: cmd %s -c \"import sys; sys.exit(3)\"\n"
        "- [ ] d -- check: none\n" % PY)
    res = checks.run_checks(t.items, str(repo), test_cmd="%s check.py" % PY, timeout=60)
    assert [r.status for r in res] == ["PASS", "FAIL", "FAIL", "UNVERIFIED"]
    assert res[1].evidence.startswith("TEST bad FAILED")
    assert "exit 3" in res[2].evidence


@pytest.mark.parametrize("cmd", [
    "python check.py && python other.py",      # &&
    "python check.py | wc -l",                 # pipe
    "python check.py > out.txt",               # redirect
    "python check.py ; python other.py",       # ;
    "python check.py 2>&1 log",                # redirect
    "python check.py || python other.py",      # ||
    "FOO=1 python check.py",                   # leading assignment: no program named FOO=1
    "cd sub && python check.py",               # leading cd (a builtin: nothing to exec)
    "export FOO=1 && python check.py",         # leading export
    "source env.sh",                           # leading source
    ". ./env.sh",                              # leading .
])
def test_cmd_check_with_shell_operators_is_refused(cmd):
    # A cmd check runs as argv, without a shell: such a line can never pass, and the
    # loop would burn rounds on it. It is refused when the task file is parsed.
    with pytest.raises(ValueError) as ei:
        taskfile.parse("- [ ] x -- check: cmd %s\n" % cmd)
    assert "sh -c" in str(ei.value)


SAFE_CMDS = [
    'python -c "import x; assert x.y == 1"',
    "pytest -k 'a or b'",
    "grep -q '&&' f",
    "C:\\Python\\python.exe check.py",
    "./run.sh --flag=1",
    "tests/*.py",
    "sh -c 'cd sub && pytest -q'",
    'bash -c "cd sub && pytest -q"',
    'python --opt="a&&b" check.py',            # an operator inside a word is data under argv
    'python -c "print(\\"hi\\")"',             # escaped quotes inside double quotes
    "pytest tests/test_(x).py",
    "grep -E foo$ f",
    "make FOO=1",                              # an assignment that is not the first word
    "grep -E a\\|b f",                         # a backslash-escaped operator
]


def test_quoted_operators_and_sh_c_are_accepted():
    for cmd in SAFE_CMDS:
        assert taskfile.parse("- [ ] x -- check: cmd %s\n" % cmd).items[0].arg == cmd
    # A quoted " -- check: none" inside the command is not shell syntax (the scan
    # accepts it); that whole line is separately refused as a double marker, see
    # test_ambiguous_markers_are_refused.
    taskfile._scan_cmd(1, 'grep -q " -- check: none" t.md')


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_sh_c_cmd_check_passes_end_to_end(repo):
    py = shlex.quote(PY)                       # posix form: what sh will read
    t = taskfile.parse("- [ ] sub passes -- check: cmd sh -c 'cd sub && %s check.py'\n" % py)
    res = checks.run_checks(t.items, str(repo), test_cmd="%s check.py" % PY, timeout=60)
    assert [r.status for r in res] == ["PASS"], res[0].evidence


def test_ambiguous_markers_are_refused():
    # A cmd whose argument quotes "-- check: none" must not turn into an unchecked item,
    # and a trailing marker with an unknown kind must not vanish into the argument.
    for line in ('- [ ] x -- check: cmd grep -q " -- check: none" t.md\n',
                 '- [ ] x -- check: test tests/t.py -- check: maybe\n',
                 '- [ ] x -- check: none, see the docs\n'):
        with pytest.raises(ValueError):
            taskfile.parse(line)
    assert taskfile.parse("- [ ] docs updated -- check: none\n").items[0].kind == "none"
