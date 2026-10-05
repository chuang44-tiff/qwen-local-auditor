"""qwen-swarm --check WORKFLOW: validate the manifest, then dry-run run(wf) twice against
fake agents and fail when the script raises or the two runs make different calls.

No real agent starts and nothing touches --target: answers come from the workflow's own
check.py (answer(role, prompt) -> text; optionally run_cmd(cmd, patch) -> dict and
patch(role, prompt) -> the diff a sandbox unit "made") or,
without one, the first of '```json\\n[]\\n```', '```json\\n{}\\n```' that the unit's parse
accepts. The dry runs use the quick preset (else the default one) with rounds capped at 2.
"""
import importlib.util
import pathlib
import shutil
import tempfile
import traceback

from lib.swarm_engine import api, manifest

DEFAULT_ANSWERS = ("```json\n[]\n```", "```json\n{}\n```")
DEFAULT_CMD = {"applied": True, "rc": 1, "timed_out": False, "output_tail": ""}


class CheckFailed(Exception):
    pass


class FakeSwarm:
    """Stands in for swarm.Swarm: answers every unit at once, starts nothing."""

    def __init__(self, run, answer):
        self.agents_dir = pathlib.Path(run) / "agents"
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.answer = answer
        self.deadline = None
        self.dropped = self.agents_run = self.tokens = 0

    def run_phase(self, units, seats=None):
        out = []
        for u in units:
            role = pathlib.Path(u.role_file).stem
            texts = [self.answer(role, u.prompt)] if self.answer else list(DEFAULT_ANSWERS)
            res = {"name": u.name, "ok": False, "data": None, "why": "check: no fake answer parses",
                   "tokens": 0, "seconds": 0.0, "cached": False, "deadline": False}
            for text in texts:
                try:
                    data = u.parse(text)
                except ValueError:
                    continue
                except Exception as e:
                    raise CheckFailed("unit %s: parse raised %s: %s" % (u.name, type(e).__name__, e))
                res.update(ok=True, data=data, why="")
                break
            self.agents_run += 1
            if not res["ok"]:
                self.dropped += 1
            out.append(res)
        return out


class FakeCommands:
    def __init__(self, run_cmd, patch=None):
        self._run_cmd, self._patch = run_cmd, patch

    def patch(self, role, prompt):
        """A sandbox unit's patch in a dry run: check.py's patch(role, prompt), else none."""
        text = self._patch(role, prompt) if self._patch else ""
        if not isinstance(text, str):
            raise CheckFailed("check.py patch must return a str")
        return text

    def run_cmd(self, cmd, patch):
        res = self._run_cmd(cmd, patch) if self._run_cmd else dict(DEFAULT_CMD)
        if not isinstance(res, dict) or not {"applied", "rc", "timed_out", "output_tail"} <= set(res):
            raise CheckFailed("check.py run_cmd must return {applied, rc, timed_out, output_tail}")
        return res


def _load_check(folder):
    path = pathlib.Path(folder) / "check.py"
    if not path.is_file():
        return None, None, None
    spec = importlib.util.spec_from_file_location("qwen_swarm_check", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "answer", None), getattr(mod, "run_cmd", None), getattr(mod, "patch", None)


def _cfg(m, runner):
    depth = "quick" if "quick" in m.presets else m.default_depth
    preset = m.presets[depth]
    rounds = preset["rounds"]
    if rounds == "until" or rounds > 2:
        rounds = 2
    prof = runner.profile(m)
    cfg = {prof["goal_key"]: "check: what is the answer?", "depth": depth}
    cfg.update({k: preset[k] for k in m.knobs})
    cfg.update({"max_agents": runner.DEFAULT_MAX_AGENTS, "max_items": runner.DEFAULT_MAX_ITEMS,
                "timeout_per_item": preset["budget"], "retries": preset["retries"],
                "effort": None, "role_effort": {}, "hours": None, "deadline": None,
                "workflow": m.name, "rounds": rounds, "target": None, "workflow_dir": None})
    return cfg, prof["goal_key"]


def dry_run(m, mod, answer, run_cmd, patch, runner):
    """One dry run in a temporary run folder; returns the call sequence."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="qwen-swarm-check-"))
    try:
        cfg, goal_key = _cfg(m, runner)
        if m.target == "required":
            (tmp / "target").mkdir()
            cfg["target"] = str(tmp / "target")
        sw = FakeSwarm(tmp / "run", answer)
        wf = api.Workflow(m, cfg, tmp / "run", sw, goal=cfg[goal_key],
                          mcp=tmp / "run" / "mcp.json", check=FakeCommands(run_cmd, patch))
        try:
            mod.run(wf)
        except api.Empty:
            pass                        # "nothing usable" is a clean end for a dry run
        except CheckFailed:
            raise
        except Exception:
            raise CheckFailed("run(wf) raised:\n%s" % traceback.format_exc())
        return wf.calls
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def check_workflow(spec, err):
    from lib.swarm_engine import runner
    try:
        folder = runner.resolve_workflow(spec)
        m = manifest.load(folder)
    except (runner.Usage, manifest.ManifestError) as e:
        err("check: %s" % e)
        return runner.EXIT_USAGE
    try:
        mod = runner.load_module(folder)
        answer, run_cmd, patch = _load_check(folder)
    except Exception as e:
        err("check: workflow.py or check.py does not import: %s: %s" % (type(e).__name__, e))
        return runner.EXIT_USAGE
    if not callable(getattr(mod, "run", None)):
        err("check: workflow.py defines no run(wf)")
        return runner.EXIT_USAGE
    try:
        first = dry_run(m, mod, answer, run_cmd, patch, runner)
        second = dry_run(m, mod, answer, run_cmd, patch, runner)
    except CheckFailed as e:
        err("check: %s" % e)
        return runner.EXIT_USAGE
    if first != second:
        n = next((i for i, (a, b) in enumerate(zip(first, second)) if a != b), min(len(first), len(second)))
        a = first[n] if n < len(first) else None
        b = second[n] if n < len(second) else None
        err("check: the two dry runs made different calls (call %d: %r vs %r); a workflow must "
            "not read the clock, randomness or the environment" % (n + 1, a, b))
        return runner.EXIT_USAGE
    print("ok: %s: manifest valid, %d calls, deterministic" % (m.name, len(first)))
    return runner.EXIT_OK
