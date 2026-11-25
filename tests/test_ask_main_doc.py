"""The main session's side of the until-done ask: reference/ask-main.md exists, carries the
CLI and the run-folder names, and both SKILL.md pages point at it."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASK_MAIN = ROOT / "skill" / "local-auditor" / "reference" / "ask-main.md"
AUDITOR_SKILL = ROOT / "skill" / "local-auditor" / "SKILL.md"
CODER_SKILL = ROOT / "skill" / "local-coder" / "SKILL.md"

TOKENS = ("--next-ask", "--release", "--amend", "ask N: advice|amend|none",
          "ADVICE RESPONSE", "QWEN_ASK_LEASE",
          "round-*.prompt.md", "round-*.json", "until-done.log", "plan-digest.md",
          "rescue.md", "plan.md", "audit-*", "decisions.jsonl")


def _ask_main():
    return ASK_MAIN.read_text(encoding="utf-8")


def test_ask_main_reference_exists():
    assert ASK_MAIN.exists(), "skill/local-auditor/reference/ask-main.md is missing"


def test_ask_main_names_the_commands_and_the_run_folder():
    t = _ask_main()
    for token in TOKENS:
        assert token in t, token


def test_ask_main_exit_table_names_the_codes():
    # a code counts as named when some pipe-delimited cell of a table row IS the code
    # (spaces and backticks around it allowed)
    t = _ask_main()
    rows = [line for line in t.splitlines() if line.strip().startswith("|")]
    for code in ("0", "10", "11", "3", "2"):
        assert any(code in [cell.strip().strip("`").strip() for cell in row.strip().strip("|").split("|")]
                   for row in rows), "no exit-table row names code %s" % code


def test_coder_skill_points_at_the_recipe():
    assert "reference/ask-main.md" in CODER_SKILL.read_text(encoding="utf-8"), \
        "local-coder SKILL.md never points at the recipe"


def test_auditor_skill_documents_the_listener():
    t = AUDITOR_SKILL.read_text(encoding="utf-8")
    assert "--next-ask" in t
    assert "reference/ask-main.md" in t


def test_ask_main_never_names_a_helper_model():
    # the helper runs on whatever model the main session offers; the page must never bind
    # it to one in advance
    t = _ask_main()
    for line in t.splitlines():
        assert not re.search(r"(?i)helper.*\bsonnet\b.*(must|only|default)", line), line
    assert not re.search(r"(?i)\bsonnet\b", t), "ask-main.md names a model at all"
