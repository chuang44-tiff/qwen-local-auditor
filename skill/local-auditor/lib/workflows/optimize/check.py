"""Fake answers for `qwen-swarm --check optimize`: the dry run walks every phase -- the
bench check, ideas, a patch, its measurement, the review vote, the stack, the final guard
-- with no agent started and no command run."""
import json
import re

PATCH = ("diff --git a/slow.py b/slow.py\n--- a/slow.py\n+++ b/slow.py\n"
         "@@ -1 +1 @@\n-DELAY = 1.0\n+DELAY = 0.1\n")
PROFILE_LINE = "5.00  slow.py"


def _block(data):
    return "```json\n" + json.dumps(data) + "\n```"


def answer(role, prompt):
    if role == "checker":
        return _block({"verdict": "ok", "findings": [], "suggested_bench": None})
    if role == "profiler":
        n = len(re.findall(r"^- I\d+ at ", prompt, re.M))
        return _block({"ideas": [{"location": "slow.py:1", "profile_line": PROFILE_LINE,
                                  "change": "lower DELAY (%d)" % n, "why": "it sleeps", "saving": "4s"}],
                       "outside_focus": None})
    if role == "implementer":
        return _block({"id": re.search(r"^- (I\d+):", prompt, re.M).group(1), "done": True,
                       "summary": "lower DELAY", "note": ""})
    if role == "reviewer":
        return _block([{"patch": p, "verdict": "keeps", "lines": ["+DELAY = 0.1"], "reason": "r"}
                       for p in re.findall(r"^- (P\d+) ", prompt, re.M)])
    return "# Optimize\n\nslow.py sleeps less.\n"


def patch(role, prompt):
    return PATCH if role == "implementer" else ""


def run_cmd(cmd, patch):
    wall = 2.0 if patch else 10.0
    res = {"rc": 0, "wall": wall, "runner": "pytest", "files": ["slow.py"], "passed": 3,
           "failed": 0, "skipped": 0}
    tail = ("TIME PER FILE (seconds, summed):\n  %s\nOUTPUT TAIL:\n3 passed\nQLA_RESULT %s\n"
            % (PROFILE_LINE, json.dumps(res)))
    return {"applied": True, "rc": 0, "timed_out": False, "output_tail": tail}
