"""qwen-deep-research's entry point: a thin shim over the swarm engine's research workflow.

The pipeline lives in lib/workflows/research/ (workflow.json, workflow.py, roles/), the
mechanical helpers in lib/swarm_engine/steps.py, the CLI in lib/swarm_engine/runner.py. This module
keeps the released qwen-deep-research's import surface (main, the parsers, the merges,
PRESETS, ROLES) so callers and tests that import lib.research keep working.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import swarm  # noqa: E402
from lib.swarm_engine import manifest, runner, steps  # noqa: E402

FOLDER = runner.BUILTIN / "research"
ROLES = FOLDER / "roles"
_wf = runner.load_module(FOLDER)
_m = manifest.load(FOLDER)
# depth -> (angles, sources, claims, voters, per-item budget s, retries), the released shape
PRESETS = {d: (p["angles"], p["sources"], p["claims"], p["voters"], p["budget"], p["retries"])
           for d, p in _m.presets.items()}
PER_ANGLE = _wf.PER_ANGLE
normalize_url, merge_urls, merge_claims, slug = (steps.normalize_url, steps.merge_urls,
                                                 steps.merge_claims, steps.slug)
parse_angles, parse_search, parse_fetch, parse_votes, parse_report = (
    _wf.parse_angles, _wf.parse_search, _wf.parse_fetch, _wf.parse_votes, _wf.parse_report)
__all__ = ["main", "swarm", "PRESETS", "ROLES", "PER_ANGLE", "normalize_url", "merge_urls",
           "merge_claims", "slug", "parse_angles", "parse_search", "parse_fetch", "parse_votes",
           "parse_report"]


def main(argv=None):
    return runner.main(sys.argv[1:] if argv is None else list(argv), compat="deep-research")


if __name__ == "__main__":
    sys.exit(main())
