"""SKILL.md is loaded on every trigger, so it must stay small, current and generic."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "local-auditor" / "SKILL.md"
REF = ROOT / "skill" / "local-auditor" / "reference"


def _skill():
    return SKILL.read_text(encoding="utf-8")


def test_skill_is_machine_agnostic():
    t = _skill()
    assert not re.search(r"\b[A-Za-z]:\\", t), "no Windows drive paths"
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+", t), "no hard-coded server address"


def test_skill_starts_with_preflight():
    assert "qwen-agent --preflight-only" in _skill()


def test_skill_is_not_bloated():
    n = len(_skill().splitlines())
    assert n < 200, f"SKILL.md is {n} lines; depth belongs in reference/"


def test_frontmatter_has_name_and_description():
    assert re.match(r"^---\nname: local-auditor\ndescription: .{200,}", _skill(), re.S)


def test_reference_files_exist_and_carry_the_depth():
    assert (REF / "sweep.md").exists() and (REF / "limits.md").exists()
    assert len((REF / "limits.md").read_text(encoding="utf-8").splitlines()) > 30


def test_limits_records_the_failopen_and_its_correction():
    t = (REF / "limits.md").read_text(encoding="utf-8").lower()
    assert "fail-open" in t and "prose_mask" in t
    assert "not a disposition" in t


def test_skill_lists_every_builder():
    t = _skill()
    for b in ("claims", "diff", "files", "logs"):
        assert b in t
