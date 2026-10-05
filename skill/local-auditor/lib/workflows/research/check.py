"""Fake answers for `qwen-swarm --check research`: every phase gets a well-formed answer,
so the dry run walks the whole pipeline (no agent starts, nothing reaches the web)."""
import json
import re


def _block(data):
    return "```json\n" + json.dumps(data) + "\n```"


def answer(role, prompt):
    if role == "scoper":
        n = int(re.search(r"exactly (\d+) angles", prompt).group(1))
        return _block({"angles": [{"angle": "facet %d" % i, "queries": ["q%d" % i]}
                                  for i in range(1, n + 1)]})
    if role == "searcher":
        return _block([{"angle": a, "url": "https://check-%s.example/" % a.lower(), "title": a,
                        "why": "w", "relevance": 4} for a in re.findall(r"^- (A\d+):", prompt, re.M)])
    if role == "reader":
        return _block([{"source": s, "claims": [{"claim": "claim from %s" % s, "snippet": "s",
                                                 "importance": 3}]}
                       for s in re.findall(r"^- (S\d+):", prompt, re.M)])
    if role == "verifier":
        return _block([{"claim": c, "verdict": "supported", "evidence_url": "https://e.example/",
                        "snippet": "s", "reason": "r"} for c in re.findall(r"^- (C\d+):", prompt, re.M)])
    if role == "planner":
        return _block({"angles": [], "recheck": []})
    return "# Answer\n\nChecked [1].\n\n## Gaps\n\nNone found.\n"
