"""Fake answers for `qwen-swarm --check debug`: the dry run walks every phase, including
the patch check and the review vote (no agent starts, no command runs)."""
import json
import re

PATCH = ("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
         "@@ -1 +1 @@\n-def add(a, b): return a - b\n+def add(a, b): return a + b\n")


def _block(data):
    return "```json\n" + json.dumps(data) + "\n```"


def answer(role, prompt):
    if role == "triager":
        return _block({"files": ["calc.py:1"], "notes": "add subtracts", "repro": "check-repro"})
    if role in ("hypothesizer", "planner"):
        if role == "planner":
            return _block([])
        n = int(re.search(r"exactly (\d+) distinct", prompt).group(1))
        return _block([{"location": "calc.py:%d" % i, "mechanism": "mechanism %d" % i,
                        "evidence_needed": "e"} for i in range(1, n + 1)])
    if role == "prober":
        return _block({"id": re.search(r"^- (H\d+):", prompt, re.M).group(1),
                       "verdict": "confirmed", "evidence": "calc.py:1"})
    if role == "reviewer":
        return _block([{"patch": p, "verdict": "root-cause", "reason": "r"}
                       for p in re.findall(r"^- (P\d+) ", prompt, re.M)])
    return "# Root cause\n\nadd subtracts.\n"


def patch(role, prompt):
    return PATCH if "H1:" in prompt else ""


def run_cmd(cmd, patch):
    return {"applied": True, "rc": 0 if patch else 1, "timed_out": False, "output_tail": ""}
