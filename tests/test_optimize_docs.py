"""The optimize docs name what the workflow actually reads and writes."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_swarm_reference_documents_optimize():
    s = read("skill/local-auditor/reference/swarm.md")
    for frag in ["## The optimize workflow", "--set bench=", '"measure"', '"guard"', '"focus"', '"target"',
                 '"keep"', "bench-review.md", "bench-suggested.json", "patches/stacked.diff",
                 "bench: problem (see bench-review.md)", "exit 4"]:
        assert frag in s, frag


def test_skill_has_the_pre_handover_checklist():
    s = read("skill/local-swarm/SKILL.md")
    for frag in ["optimize", "bench", "run measure twice", "guard", "bench-review.md", "noise"]:
        assert frag in s, frag


def test_readme_lists_optimize():
    assert "`optimize`" in read("README.md")
