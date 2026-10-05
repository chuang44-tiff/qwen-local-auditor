"""Helpers shared by the swarm-engine tests: a tiny workflow folder written into tmp_path."""
import json
import pathlib
import subprocess
import sys

FAKE = pathlib.Path(__file__).resolve().parent / "fake_swarm_agent.py"

BASE = {
    "name": "echo",
    "description": "A test workflow",
    "goal": "the thing to echo",
    "target": "none",
    "roles": {"worker": {"file": "roles/worker.md", "fence": "none"}},
    "knobs": {"items": "int"},
    "presets": {"quick": {"items": 3, "budget": 100, "retries": 0, "rounds": 1},
                "standard": {"items": 5, "budget": 100, "retries": 0, "rounds": 1}},
    "default_depth": "quick",
}

ECHO_SCRIPT = '''
def run(wf):
    items = [{"id": "I%d" % i} for i in range(1, wf.knob("items") + 1)]
    res = wf.fan_out("work", "worker", items,
                     lambda batch: "items:\\n" + "\\n".join("- %s:" % it["id"] for it in batch),
                     lambda text, batch: [{"id": it["id"]} for it in batch])
    wf.save("rows", res.rows)
    wf.report("# Echo\\n\\n%d rows\\n" % len(res.rows))
'''


def make_workflow(root, manifest=None, script=ECHO_SCRIPT, roles=("worker",), extra=None):
    """A workflow folder under root/<name>: workflow.json (BASE updated with `manifest`),
    workflow.py (`script`), roles/<role>.md for each role, and `extra` {relpath: text}."""
    data = json.loads(json.dumps(BASE))
    data.update(manifest or {})
    folder = pathlib.Path(root) / data["name"]
    (folder / "roles").mkdir(parents=True, exist_ok=True)
    for r in roles:
        (folder / "roles" / ("%s.md" % r)).write_text("role %s" % r, encoding="utf-8")
    (folder / "workflow.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    (folder / "workflow.py").write_text(script, encoding="utf-8")
    for rel, text in (extra or {}).items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return folder


def agent_args():
    return ["--agent", sys.executable, "--agent", str(FAKE)]


def calls(fake_dir):
    path = pathlib.Path(fake_dir) / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
            if "--preflight-only" not in x]


def unit_names(fake_dir):
    return [pathlib.Path(a[a.index("-C") + 1]).name for a in calls(fake_dir)]


def git_repo(path, files):
    """A git repo at path with `files` {relpath: text} committed."""
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
             "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git"] + ident + ["add", "-A"], cwd=str(path), check=True)
    subprocess.run(["git"] + ident + ["commit", "-q", "-m", "init"], cwd=str(path), check=True)
    return path
