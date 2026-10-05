import os

import pytest

from lib.swarm_engine import manifest
from swarm_fixtures import BASE, make_workflow


def load(tmp_path, **changes):
    return manifest.load(make_workflow(tmp_path, changes))


def symlink(target, link_path):
    """Replace link_path with a symlink to target; skip the test if symlinks are
    unavailable (e.g. Windows without the privilege)."""
    link_path.unlink(missing_ok=True)
    try:
        os.symlink(str(target), str(link_path))
    except (OSError, NotImplementedError) as e:
        pytest.skip("symlinks unavailable: %s" % e)


def test_valid_manifest_loads(tmp_path):
    m = load(tmp_path)
    assert m.name == "echo" and m.target == "none" and m.default_depth == "quick"
    r = m.roles["worker"]
    assert r.fence == "none" and r.budget_weight == 1 and r.effort is None
    assert r.file.is_file() and r.file.name == "worker.md"
    assert m.knobs == {"items": "int"} and m.presets["standard"]["items"] == 5


@pytest.mark.parametrize("changes,needle", [
    ({"name": "Echo"}, "name"),
    ({"name": "1echo"}, "name"),
    ({"description": ""}, "description"),
    ({"goal": "  "}, "goal"),
    ({"target": "maybe"}, "target"),
    ({"roles": {}}, "roles"),
    ({"roles": {"worker": {"file": "roles/worker.md", "fence": "net"}}}, "roles.worker.fence"),
    ({"roles": {"worker": {"file": "../worker.md", "fence": "none"}}}, "roles.worker.file"),
    ({"roles": {"worker": {"file": "/etc/passwd", "fence": "none"}}}, "roles.worker.file"),
    ({"roles": {"worker": {"file": "roles/missing.md", "fence": "none"}}}, "roles.worker.file"),
    ({"roles": {"worker": {"file": "roles/worker.md", "fence": "none", "budget_weight": 0.5}}},
     "budget_weight"),
    ({"roles": {"worker": {"file": "roles/worker.md", "fence": "none", "effort": "very high"}}},
     "effort"),
    ({"roles": {"worker": {"file": "roles/worker.md", "fence": "none", "tools": "Bash"}}},
     "unknown key"),
    ({"roles": {"worker": {"file": "roles/worker.md", "fence": "sandbox"}}}, "target"),
    ({"roles": {"worker": {"file": "roles/worker.md", "fence": "read"}}}, "target"),
    ({"knobs": {"items": "list"}}, "knobs.items"),
    ({"knobs": {"depth": "str"}}, "knobs.depth"),
    ({"default_depth": "huge"}, "default_depth"),
    ({"extra": 1}, "unknown key"),
])
def test_each_rule_names_its_field(tmp_path, changes, needle):
    with pytest.raises(manifest.ManifestError) as e:
        load(tmp_path, **changes)
    assert needle in str(e.value)


@pytest.mark.parametrize("preset,needle", [
    ({"budget": 100, "retries": 0, "rounds": 1}, "must set knob 'items'"),
    ({"items": -1, "budget": 100, "retries": 0, "rounds": 1}, "presets.quick.items"),
    ({"items": True, "budget": 100, "retries": 0, "rounds": 1}, "presets.quick.items"),
    ({"items": 3, "budget": 0, "retries": 0, "rounds": 1}, "budget"),
    ({"items": 3, "budget": 100, "retries": -1, "rounds": 1}, "retries"),
    ({"items": 3, "budget": 100, "retries": 0, "rounds": 0}, "rounds"),
    ({"items": 3, "budget": 100, "retries": 0, "rounds": "forever"}, "rounds"),
    ({"items": 3, "budget": 100, "retries": 0, "rounds": "until"}, "hours is required"),
    ({"items": 3, "budget": 100, "retries": 0, "rounds": 1, "hours": 0}, "hours"),
    ({"items": 3, "budget": 100, "retries": 0, "rounds": 1, "colour": 2}, "colour"),
])
def test_preset_rules(tmp_path, preset, needle):
    presets = dict(BASE["presets"], quick=preset)
    with pytest.raises(manifest.ManifestError) as e:
        load(tmp_path, presets=presets)
    assert needle in str(e.value)


def test_until_with_hours_is_valid(tmp_path):
    presets = dict(BASE["presets"], quick={"items": 3, "budget": 100, "retries": 0,
                                            "rounds": "until", "hours": 8})
    assert load(tmp_path, presets=presets).presets["quick"]["rounds"] == "until"


def test_bad_json_and_missing_file(tmp_path):
    folder = make_workflow(tmp_path)
    (folder / "workflow.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(manifest.ManifestError) as e:
        manifest.load(folder)
    assert "not valid JSON" in str(e.value)
    with pytest.raises(manifest.ManifestError):
        manifest.load(tmp_path / "nowhere")


def test_knob_types(tmp_path):
    m = load(tmp_path, knobs={"n": "int", "x": "float", "s": "str", "b": "bool"},
             presets={"quick": {"n": 1, "x": 0.5, "s": "", "b": False, "budget": 1,
                                "retries": 0, "rounds": 1}})
    got = manifest.parse_sets(m, ["n=4", "x=2.5", "s=a=b", "b=yes", "budget=30", "rounds=until",
                                  "hours=1.5", "retries=0"])
    assert got == {"n": 4, "x": 2.5, "s": "a=b", "b": True, "budget": 30, "rounds": "until",
                   "hours": 1.5, "retries": 0}


@pytest.mark.parametrize("pair,needle", [
    ("n=four", "--set n=four"), ("n=-1", "--set n=-1"), ("x=nan", "--set x=nan"),
    ("b=maybe", "--set b=maybe"), ("budget=0", "--set budget=0"), ("rounds=0", "--set rounds"),
    ("hours=0", "--set hours"), ("colour=red", "no such knob"), ("n", "not KNOB=VALUE"),
])
def test_set_type_errors(tmp_path, pair, needle):
    m = load(tmp_path, knobs={"n": "int", "x": "float", "b": "bool"},
             presets={"quick": {"n": 1, "x": 0.5, "b": False, "budget": 1, "retries": 0,
                                "rounds": 1}})
    with pytest.raises(manifest.ManifestError) as e:
        manifest.parse_sets(m, [pair])
    assert needle in str(e.value)


def test_symlinked_role_file_outside_is_rejected(tmp_path):
    folder = make_workflow(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("role worker", encoding="utf-8")
    symlink(outside, folder / "roles" / "worker.md")
    with pytest.raises(manifest.ManifestError) as e:
        manifest.load(folder)
    assert "stay inside the workflow folder" in str(e.value)


def test_symlinked_role_file_inside_is_ok(tmp_path):
    folder = make_workflow(tmp_path, roles=())
    inside = folder / "roles" / "helper.md"
    inside.write_text("role worker", encoding="utf-8")
    symlink(inside, folder / "roles" / "worker.md")
    m = manifest.load(folder)
    assert m.roles["worker"].file.is_file() and m.roles["worker"].file.name == "helper.md"


@pytest.mark.parametrize("depth", [["quick"], {}, 3, None])
def test_non_string_default_depth(tmp_path, depth):
    with pytest.raises(manifest.ManifestError) as e:
        load(tmp_path, default_depth=depth)
    assert "default_depth must name a preset" in str(e.value)
