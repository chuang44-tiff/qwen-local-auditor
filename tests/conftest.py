import sys
import pathlib
# The skill directory is the install unit, so the library lives inside it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor"))
