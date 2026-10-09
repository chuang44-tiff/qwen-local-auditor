"""Dry-run answers for `qwen-swarm --check ui-test`.

The shipped `--check` ends at the empty `scenarios` knob (its preset value) before any agent
is asked, so it validates the manifest and nothing else. A dry run that does hold a suite
(tests/test_ui_test_workflow.py patches one in) reaches the tester stage and then the confirm
pass, and needs a realistic answer for each role that can be asked:

- tester: FAIL for every scenario the prompt names, so the confirm pass has candidates;
- confirmer (confirm=local) and claude-check (confirm=claude: wf.claude_check asks the dry
  run's answer("claude-check", prompt)): one FALSE_ALARM verdict block for the first scenario
  the prompt names.

Deterministic: nothing here reads the clock, the environment or randomness.
"""
import json
import re

_SCENARIO = re.compile(r"^Scenario (\S+):", re.M)


def _block(payload):
    return "Checked it.\n\n```json\n" + json.dumps(payload) + "\n```"


def answer(role, prompt):
    ids = _SCENARIO.findall(prompt)
    if role == "tester":
        return _block({"results": [{"id": sid, "status": "FAIL",
                                    "failed_expectations": ["the first expectation"],
                                    "evidence": ["step-1.png"],
                                    "notes": "check: scripted failure"} for sid in ids]})
    if role in ("confirmer", "claude-check"):
        return _block({"id": ids[0] if ids else "", "verdict": "FALSE_ALARM",
                       "evidence": ["check: the probe read the expected state"], "notes": ""})
    return ""
