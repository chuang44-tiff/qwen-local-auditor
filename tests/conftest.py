import os
import pathlib
import sys
# The skill directory is the install unit, so the library lives inside it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_qwen_swarm_env(monkeypatch):
    """A developer's QWEN_SWARM_* or QWEN_DR_* shell settings must not change test results."""
    for name in list(os.environ):
        if name.startswith(("QWEN_SWARM_", "QWEN_DR_")):
            monkeypatch.delenv(name, raising=False)
