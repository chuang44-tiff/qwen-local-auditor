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
