"""prose_mask is the fix for the one recorded fail-open. These tests are the guard."""
from lib.context import prose_mask, excerpt, symbol_census

PY_SRC = '''"""ledger.py -- ISSUE-7: persistent orphan reap.

This module closes the MEASURED defect, NOT the class of defects.
Policy 2 is DEFERRED to ISSUE-7b.
"""
import os

def boot_reap():
    # a real comment mentioning ISSUE-7
    marker = "ISSUE-7 in a string VALUE"
    return marker
'''


def test_multiline_docstring_body_is_prose():
    lines = PY_SRC.splitlines()
    m = prose_mask(lines, PY_SRC, "ledger.py")
    assert m[2] is True, "docstring BODY must be prose -- this was the fail-open"
    assert m[3] is True


def test_docstring_opener_and_comment_are_prose():
    lines = PY_SRC.splitlines()
    m = prose_mask(lines, PY_SRC, "x.py")
    assert m[0] is True                     # """ opener
    assert m[8] is True                     # # comment


def test_string_used_as_a_value_is_code():
    lines = PY_SRC.splitlines()
    m = prose_mask(lines, PY_SRC, "x.py")
    assert m[9] is False, "a string VALUE is code, not prose"


def test_real_code_is_code():
    lines = PY_SRC.splitlines()
    m = prose_mask(lines, PY_SRC, "x.py")
    assert m[7] is False                    # def boot_reap():
    assert m[5] is False                    # import os


def test_non_python_uses_fallback_without_crashing():
    src = "/* describes a fix */\nint x = 1;\n"
    m = prose_mask(src.splitlines(), src, "x.c")
    assert len(m) == 2 and m[1] is False


def test_unparseable_python_falls_back():
    src = 'def broken(:\n    """doc"""\n    pass\n'
    m = prose_mask(src.splitlines(), src, "broken.py")
    assert len(m) == 3


def test_excerpt_prefers_code_over_docstring(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(PY_SRC)
    out = "\n".join(excerpt(str(f), ["ISSUE-7"]))
    assert "def boot_reap" in out, "the code hit must be in the window"


def test_census_splits_production_from_tests(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("WIDGET = 1\n")
    (tmp_path / "tests" / "t.py").write_text("ONLY_IN_TESTS = 2\n")
    rep = symbol_census(["WIDGET", "ONLY_IN_TESTS", "NOWHERE"], str(tmp_path), "tests")
    assert "TESTS ONLY" in rep and "ONLY_IN_TESTS" in rep
    assert "ABSENT" in rep



def test_c_family_comments_are_prose():
    src = ("/**\n * Fixed: tokens now expire.\n */\n"
           "int check(void) { return 1; } // trailing comment\n"
           "// only a comment\n")
    m = prose_mask(src.splitlines(), src, "x.ts")
    assert m[:3] == [True, True, True]
    assert m[3] is False and m[4] is True


def test_census_scans_the_given_extensions_and_test_file_names(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.go").write_text("func Reap() {}\n")
    (tmp_path / "pkg" / "a_test.go").write_text("func TestOnlyHelper() {}\n")
    rep = symbol_census(["Reap", "TestOnlyHelper"], str(tmp_path), ["tests"], (".go",))
    assert "ABSENT" not in rep
    assert "TESTS ONLY" in rep and ".go" in rep
