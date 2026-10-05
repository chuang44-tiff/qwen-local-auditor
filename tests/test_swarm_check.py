import pytest

from lib.swarm_engine import runner
from swarm_fixtures import agent_args, calls, make_workflow


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    return d


def check(spec):
    return runner.main(agent_args() + ["--check", str(spec)])


def test_check_passes_a_good_workflow_without_starting_agents(tmp_path, fake, capsys):
    assert check(make_workflow(tmp_path)) == 0
    out = capsys.readouterr().out
    assert out.startswith("ok: echo: manifest valid, 3 calls, deterministic")
    assert calls(fake) == []                         # no agent, no preflight


def test_check_passes_every_builtin(capsys):
    for name in runner.builtin_names():
        assert check(name) == 0, capsys.readouterr().err


def test_check_catches_a_nondeterministic_script(tmp_path, fake, capsys):
    script = '''
import itertools
_n = itertools.count()
def run(wf):
    wf.agent("a", "worker", "call %d" % next(_n), lambda text: text)
    wf.report("x\\n")
'''
    assert check(make_workflow(tmp_path, script=script)) == 2
    assert "different calls" in capsys.readouterr().err


def test_check_catches_a_raising_script(tmp_path, fake, capsys):
    script = "def run(wf):\n    {}['missing']\n"
    assert check(make_workflow(tmp_path, script=script)) == 2
    err = capsys.readouterr().err
    assert "run(wf) raised" in err and "KeyError" in err


def test_check_reports_manifest_and_import_errors(tmp_path, fake, capsys):
    assert check(make_workflow(tmp_path / "a", {"knobs": {"items": "list"}})) == 2
    assert "knobs.items" in capsys.readouterr().err
    assert check(make_workflow(tmp_path / "b", script="def run(wf)\n    pass\n")) == 2
    assert "does not import" in capsys.readouterr().err
    assert check(make_workflow(tmp_path / "c", script="x = 1\n")) == 2
    assert "defines no run(wf)" in capsys.readouterr().err


def test_check_uses_the_workflows_check_py(tmp_path, fake, capsys):
    script = '''
def run(wf):
    got = wf.agent("a", "worker", "q", lambda text: text if text == "fine" else int("x"))
    if got != "fine":
        raise RuntimeError("check.py answer was not used")
    wf.report("x\\n")
'''
    folder = make_workflow(tmp_path, script=script,
                           extra={"check.py": "def answer(role, prompt):\n    return 'fine'\n"})
    assert check(folder) == 0, capsys.readouterr().err
