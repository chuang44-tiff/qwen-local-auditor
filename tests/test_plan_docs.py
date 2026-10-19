"""--plan is documented where the other advisor options are: --help and the reference."""
import pathlib
import subprocess

import pytest

from test_cli import AGENT, BASH, posix

REF = pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor" / "reference"


def test_help_lists_plan_and_plan_state():
    r = subprocess.run([BASH, posix(AGENT), "--help"], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=60)
    text = r.stdout + r.stderr
    assert "--plan MODEL" in text and "--plan-state DIR" in text and "FIRST file edit" in text


@pytest.mark.parametrize("name,needle", [
    ("qwen-agent.md", "## Planning (`--plan MODEL`"),
    ("qwen-agent.md", "The digest leaves this machine"),
    ("qwen-agent.md", "| `--plan MODEL` |"),
    ("coding.md", "| `--plan MODEL` |"),
])
def test_reference_documents_plan(name, needle):
    assert needle in (REF / name).read_text(encoding="utf-8")


ROOT = REF.parents[2]
_DOC_FILES = ([ROOT / "README.md", ROOT / "SECURITY.md", ROOT / "config.example"]
              + sorted(REF.glob("*.md")) + sorted((ROOT / "skill").glob("*/SKILL.md")))


@pytest.mark.parametrize("term", ["--advisor-triggers", "failure call", "harness advice",
                                  "No environment variable or config line", "nothing else changes"])
def test_docs_no_stale_terms(term):
    hits = [p.name for p in _DOC_FILES if term in p.read_text(encoding="utf-8")]
    assert not hits, "%s still in %s" % (term, hits)


def test_config_example_keys():
    text = (ROOT / "config.example").read_text(encoding="utf-8")
    for key in ("QWEN_PLAN", "QWEN_ADVISOR", "QWEN_RESCUE", "QWEN_CONFIRM", "QWEN_CLAUDE_TOOLS"):
        assert "# " + key + "=" in text, key


def test_security_lists_four_calls():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8").lower()
    for word in ("plan", "advisor", "rescue", "confirm", "read tools"):
        assert word in text, word


def test_readme_benchmark_labels():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    sentences = [s for s in text.replace("\n", " ").split(". ") if "19/20" in s]
    assert sentences
    for s in sentences:
        assert "advisor" in s and "rescue" in s, s
    assert "18/20" in text
