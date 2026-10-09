"""lib/swarm_engine/staging.py: the one walk that decides what a fixtures folder holds, and
the size, digest and copy built on it."""
import os

import pytest

from lib.swarm_engine import staging


def tree(root, files):
    for rel, data in files.items():
        p = root.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def link(target, at, is_dir):
    try:
        os.symlink(str(target), str(at), target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("cannot make a symlink here")


# where a file symlink points is what these tests are about; making them on Windows
# needs privileges a CI runner may not have
needs_symlink = pytest.mark.skipif(os.name == "nt", reason="file symlinks are the point")


def test_inside_ignores_case_where_the_os_does(tmp_path, monkeypatch):
    # _inside gets two already-resolved paths and decides whether one is the other or
    # under it. On a case-insensitive filesystem the same folder reaches it under two
    # spellings, and one spelling is not an exit from the folder -- normcase is the only
    # thing that knows which rule the OS runs by.
    root = str(tmp_path / "Fixtures")
    inside = os.path.join(str(tmp_path / "Fixtures"), "Docs", "a.csv")
    outside = os.path.join(str(tmp_path), "Other", "a.csv")
    assert staging._inside(root, inside)                      # same case: inside anywhere
    assert not staging._inside(root, outside)                 # a different folder is outside
    monkeypatch.setattr(os.path, "normcase", str.lower)       # what Windows does
    assert staging._inside(root.lower(), inside)              # one folder, two spellings
    assert staging._inside(root, inside.lower())
    assert not staging._inside(root.lower(), outside.lower())  # still a different folder


def test_walk_skips_a_folder_whose_spelling_differs(tmp_path, monkeypatch):
    # `skip` names a folder that may well EXIST -- the run folder inside a fixtures folder,
    # whose digest must not count its own output. On a case-insensitive filesystem it
    # reaches the walk spelled however the caller spelled it, and one spelling still means
    # "keep this folder out of the walk".
    src = tree(tmp_path / "Fixtures", {"a.txt": b"aaaa", "Run/x.bin": b"x"})

    def names(skip):
        return [n for n, _ in staging.files(str(src), skip=skip)]

    assert names(str(tmp_path / "Fixtures" / "Run")) == ["a.txt"]     # the folder as such
    monkeypatch.setattr(os.path, "normcase", str.lower)               # what Windows does
    assert names(str(tmp_path / "Fixtures" / "RUN")) == ["a.txt"]     # same folder, other case
    assert names(str(tmp_path / "Elsewhere")) == ["Run/x.bin", "a.txt"]   # nothing skipped


def test_files_are_posix_names_sorted(tmp_path):
    src = tree(tmp_path / "fx", {"b.txt": b"b", "docs/a b.csv": b"x,y", "a.png": b"\x89PNG"})
    got = staging.files(str(src))
    assert [n for n, _ in got] == ["a.png", "b.txt", "docs/a b.csv"]
    assert got[2][1] == os.path.join(str(src), "docs", "a b.csv")       # native path to read
    assert staging.size(str(src)) == 1 + 3 + 4


def test_a_link_loop_does_not_hang_and_a_linked_file_counts_at_its_target(tmp_path):
    src = tree(tmp_path / "fx", {"a.txt": b"aaaa", "real/big.bin": b"z" * 1000})
    link(src, src / "loop", True)                   # fx/loop -> fx: following it never ends
    link(src / "real" / "big.bin", src / "big.bin", False)
    assert [n for n, _ in staging.files(str(src))] == ["a.txt", "big.bin", "real/big.bin"]
    assert staging.size(str(src)) == 4 + 1000 + 1000       # the target's size, not the link's
    dest = tmp_path / "agents" / "u-1" / "fixtures"
    staging.copy(str(src), str(dest))
    # no "loop" copied, and the linked file is its target's content, not a link
    assert sorted(os.listdir(str(dest))) == ["a.txt", "big.bin", "real"]
    assert not os.path.islink(str(dest / "big.bin"))                    # content, not a link
    assert (dest / "big.bin").read_bytes() == b"z" * 1000


@needs_symlink
def test_symlink_pointing_outside_the_folder_is_refused(tmp_path):
    src = tree(tmp_path / "fx", {"a.txt": b"aaaa"})
    secret = tree(tmp_path / ".ssh", {"id_rsa": b"secret"})
    link(secret / "id_rsa", src / "leak", False)         # fixtures/leak -> ~/.ssh/id_rsa
    plain = tree(tmp_path / "plain", {"a.txt": b"aaaa"})
    assert [n for n, _ in staging.files(str(src))] == ["a.txt"]   # never listed...
    assert staging.size(str(src)) == 4                   # ...never counted...
    assert staging.digest(str(src)) == staging.digest(str(plain))  # ...never in the digest
    assert staging.problems(str(src)) == ["leak points outside the fixtures folder"]


@needs_symlink
def test_symlink_inside_the_folder_is_kept(tmp_path):
    src = tree(tmp_path / "fx", {"real/big.bin": b"z" * 1000})
    link(src / "real" / "big.bin", src / "copy.bin", False)
    assert [n for n, _ in staging.files(str(src))] == ["copy.bin", "real/big.bin"]
    assert staging.problems(str(src)) == []
    assert staging.size(str(src)) == 2000                # the target's size, twice over
    dest = tmp_path / "agents" / "u-1" / "fixtures"
    staging.copy(str(src), str(dest))
    assert sorted(os.listdir(str(dest))) == ["copy.bin", "real"]
    assert not os.path.islink(str(dest / "copy.bin"))
    assert (dest / "copy.bin").read_bytes() == b"z" * 1000


@needs_symlink
def test_copy_never_stages_an_outside_target(tmp_path):
    src = tree(tmp_path / "fx", {"a.txt": b"aaaa"})
    secret = tree(tmp_path / ".ssh", {"id_rsa": b"secret"})
    link(secret / "id_rsa", src / "leak", False)
    dest = tmp_path / "agents" / "u-1" / "fixtures"
    staging.copy(str(src), str(dest))                    # no validation in front of it
    assert sorted(os.listdir(str(dest))) == ["a.txt"]    # the outside target stays put
    assert (secret / "id_rsa").read_bytes() == b"secret"


@needs_symlink
def test_copy_replaces_a_stale_dest_link_into_the_folder(tmp_path):
    # a leftover dest that is a link into the fixtures folder: the link goes, the folder
    # it pointed at stays a fixture (a stale realpath of it must not drop it)...
    src = tree(tmp_path / "fx", {"a.txt": b"aaaa", "d/inside.bin": b"z"})
    dest = tmp_path / "agents" / "u-1" / "fixtures"
    dest.parent.mkdir(parents=True)
    link(src / "d", dest, True)
    staging.copy(str(src), str(dest))
    assert not os.path.islink(str(dest))                      # dest became a real copy
    assert sorted(os.listdir(str(dest))) == ["a.txt", "d"]
    assert (dest / "d" / "inside.bin").read_bytes() == b"z"   # live folder still staged
    staging.copy(str(src), str(dest))                         # and the re-stage holds


@needs_symlink
def test_copy_into_its_own_subfolder_through_a_linked_parent(tmp_path):
    # dest reached through a LINKED parent inside src: its real location is the linked
    # folder, so the copy must not walk into (and into into) its own destination
    src = tree(tmp_path / "fx", {"a.txt": b"aaaa", "real/u/f.txt": b"f"})
    link(src / "real", src / "via", True)                     # linked parent of dest
    dest = tmp_path / "fx" / "via" / "u" / "fixtures"         # == src/real/u/fixtures
    try:
        staging.copy(str(src), str(dest))
        staging.copy(str(src), str(dest))                     # the re-stage
    except RecursionError:
        pytest.fail("copy recursed into its own destination")
    real = tmp_path / "fx" / "real" / "u" / "fixtures"        # where dest truly lives
    assert (real / "a.txt").read_bytes() == b"aaaa"
    assert not (real / "real" / "u" / "fixtures").exists()    # never its own copy


def test_copy_into_its_own_subfolder_does_not_recurse(tmp_path):
    # the `fixtures: .` shape: the folder to copy holds the run folder, and the copy goes
    # to agents/<unit>/fixtures inside it -- the destination is inside its own source
    src = tree(tmp_path, {"a.txt": b"aaaa", "config.json": b"{}"})
    unit = tmp_path / "run" / "agents" / "u-1"
    unit.mkdir(parents=True)
    dest = unit / "fixtures"
    staging.copy(str(src), str(dest))
    staging.copy(str(src), str(dest))                    # the re-stage sees the first copy
    assert (dest / "a.txt").read_bytes() == b"aaaa"
    assert not (dest / "run" / "agents" / "u-1" / "fixtures").exists()   # never its own copy
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "config.json", "run"]


def test_digest_moves_with_any_name_or_content_change(tmp_path):
    src = tree(tmp_path / "fx", {"a.txt": b"one", "sub/b.txt": b"two"})
    first = staging.digest(str(src))
    assert staging.digest(str(src)) == first        # stable
    (src / "a.txt").write_bytes(b"ONE")
    edited = staging.digest(str(src))
    assert edited != first
    (src / "sub" / "b.txt").rename(src / "sub" / "c.txt")
    assert staging.digest(str(src)) not in (first, edited)


def test_copy_replaces_what_was_staged_before(tmp_path):
    src = tree(tmp_path / "fx", {"a.txt": b"new"})
    dest = tree(tmp_path / "agents" / "u-1" / "fixtures", {"a.txt": b"old", "stale.txt": b"x"})
    os.chmod(str(dest / "a.txt"), 0o444)            # a read-only leftover must not block it
    staging.copy(str(src), str(dest))
    assert sorted(os.listdir(str(dest))) == ["a.txt"]
    assert (dest / "a.txt").read_bytes() == b"new"
    os.chmod(str(src / "a.txt"), 0o444)             # nor a read-only source, twice over
    staging.copy(str(src), str(dest))
    staging.copy(str(src), str(dest))
    assert (dest / "a.txt").read_bytes() == b"new"
