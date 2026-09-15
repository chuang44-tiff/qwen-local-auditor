"""Briefs are prompts; the tests guard the properties that were measured."""
import pathlib
import pytest
from lib.engine import render_brief

BRIEFS = pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor" / "lib" / "briefs"


def _text(name):
    return (BRIEFS / f"{name}.md").read_text(encoding="utf-8")


def test_substitutes_declared_vars():
    assert render_brief("Emit {{N}} blocks.", {"N": 3}) == "Emit 3 blocks."


def test_unfilled_variable_fails_loudly():
    with pytest.raises(KeyError):
        render_brief("Emit {{N}} for {{ITEMS}}.", {"N": 3})


@pytest.mark.parametrize("name", ["claims", "review", "summarize"])
def test_every_brief_renders_with_the_engine_vars(name):
    out = render_brief(_text(name), {"N": 2, "ITEMS": "t1, t2"})
    assert "{{" not in out and "Emit exactly 2 blocks" in out


def test_claims_brief_forbids_docstring_evidence():
    t = _text("claims")
    assert "not evidence the fix landed" in t.lower() or "NOT evidence the fix landed" in t
    assert "CANNOT_DETERMINE" in t


def test_claims_brief_warns_still_present_is_not_a_disposition():
    assert "not a disposition" in _text("claims").lower()


def test_claims_brief_demands_per_clause_verdicts():
    assert "clause" in _text("claims").lower()


def test_review_brief_is_extraction_shaped_not_a_rating():
    t = _text("review")
    assert 'is this good' in t.lower(), "must explicitly rule the rating question out"
    assert "VERDICT" not in t.split("There is no VERDICT")[0], \
        "a diff review has no verdict vocabulary"


@pytest.mark.parametrize("name", ["claims", "review", "summarize"])
def test_no_brief_promises_repo_access(name):
    assert "no repo access" in _text(name).lower() or "no other access" in _text(name).lower()
