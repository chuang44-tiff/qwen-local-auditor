"""A fixtures folder staged into a unit's own working directory (wf.fan_out / wf.agent stage=).

A browser unit can upload only a file inside its own working directory: Playwright MCP
accepts an upload inside its --output-dir or its cwd, and qwen-agent makes the output dir
fresh for every call. So a workflow that names a fixtures folder has it copied into
agents/<unit>/fixtures/ when the unit starts (Unit.setup: once per unit, so its retries and
its repair round find the same copy in the same folder).

One walk, files(), decides what a folder holds, and the size check, the digest that joins
the cache key, the file list a prompt names and the copy all use it, so they cannot
disagree: a regular file is in; a symlinked FILE is in, counted, hashed and copied as its
target, when its target stays inside the folder, and refused -- never listed, never
counted, never copied -- when it points outside it, so fixtures/leak -> ~/.ssh/id_rsa
cannot smuggle a file out of the folder and into a unit, its digest or its prompt; a
symlinked DIRECTORY (or a Windows junction) is skipped, never followed, so a link loop
cannot hang the walk; anything else (a dangling link, a fifo, a socket) is skipped. A
relative name carrying a control character is refused too -- it could inject a line into
the prompt that names the folder's files. problems() reports both by name for a
workflow's validation to refuse the folder over the first of them. Names are posix
relative paths ("docs/a.csv") on every platform; the copy is made with plain copyfile
(no permission bits), and a re-stage clears read-only leftovers, so neither a read-only
source nor an earlier copy makes the next unit's re-stage fail on Windows.

`skip` -- files/size/digest take it, and copy() passes its own destination -- names one
folder whose subtree no walk enters: a destination inside the source (a fixtures folder
that holds the run folder it is copied into) would otherwise be listed as fixtures and
copied into itself without end; copy() drops that destination from the tree it walks and
the tree it copies, both by realpath.
"""
import hashlib
import os
import shutil
import stat
import sys

MAX_BYTES = 200 * 1024 * 1024       # a fixtures folder larger than this is refused (exit 2)
FOLDER = "fixtures"                 # the staged copy: agents/<unit>/fixtures/


def _is_link(path):
    """A symlink, or a Windows junction (Python 3.12+ can tell; older ones cannot)."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _inside(root, path):
    """True when the already-resolved `path` is the already-resolved `root` or sits
    under it -- so a link whose target leaves the folder is outside it. Both sides go
    through os.path.normcase first: on a case-insensitive filesystem the same folder
    reaches here under two spellings (a fixture written `..\\Fixtures` in a suite file),
    and one spelling is not an exit from the folder."""
    root, path = os.path.normcase(root), os.path.normcase(path)
    return path == root or path.startswith(root + os.sep)


def _walk(src, skip=None):
    """Yield (dirpath, filenames) per directory of `src`. os.walk lists a linked
    directory among dirnames; pruning it here means it is never descended, whatever
    followlinks would do with a junction -- and the same pruning drops `skip`, so a
    folder inside the source is never walked once. `skip` is a folder that may well exist
    (the run folder, whose digest must not count itself), so it is compared under
    os.path.normcase like every other containment test here: one folder spelled two ways
    is still the folder to keep out of the walk."""
    skip = os.path.normcase(os.path.realpath(skip)) if skip is not None else None

    def real_dir(path):
        return not _is_link(path) and os.path.normcase(os.path.realpath(path)) != skip

    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if real_dir(os.path.join(dirpath, d)))
        yield dirpath, filenames


def files(src, skip=None):
    """[(name, path)] of every file the folder `src` stages, sorted by name: `name` is the
    posix path relative to `src`, `path` the native path to read it from. A file whose
    realpath falls outside realpath(src) -- a link pointing out of the folder -- is not
    one of them; `skip` keeps one folder's subtree out of the walk. problems() names what
    was refused."""
    root = os.path.realpath(src)
    out = []
    for dirpath, filenames in _walk(src, skip):
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            if os.path.isfile(path) and _inside(root, os.path.realpath(path)):
                out.append((os.path.relpath(path, src).replace(os.sep, "/"), path))
    return sorted(out)


def problems(src, skip=None):
    """Sorted, one line per entry of `src` that may not be staged: "<name> points outside
    the fixtures folder" for a file whose realpath leaves realpath(src), and
    "<repr(name)> has a control character in its name" for a relative name that could
    inject a line into a tester prompt. files() stages neither; a workflow's validation
    refuses the folder, naming the first of these."""
    root = os.path.realpath(src)
    out = []
    for dirpath, filenames in _walk(src, skip):
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            name = os.path.relpath(path, src).replace(os.sep, "/")
            if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
                out.append("%s has a control character in its name" % repr(name))
            elif os.path.isfile(path) and not _inside(root, os.path.realpath(path)):
                out.append("%s points outside the fixtures folder" % name)
    return sorted(out)


def size(src, skip=None):
    """Total bytes of files(src, skip); a linked file that stays inside the folder counts
    at its target's size, and one that leaves it is not counted at all."""
    return sum(os.path.getsize(path) for _, path in files(src, skip=skip))


def digest(src, skip=None):
    """sha256 over every staged file's name and content: a renamed, added, removed or
    edited fixture changes it, and so the cache key of every unit that staged it."""
    h = hashlib.sha256()
    for name, path in files(src, skip=skip):
        one = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                one.update(chunk)
        h.update(name.encode("utf-8", "surrogateescape") + b"\0"
                 + one.hexdigest().encode("ascii") + b"\n")
    return h.hexdigest()


def _remove(dest):
    """rmtree that also clears a read-only file: Windows refuses to delete one, and a
    leftover copy would otherwise fail every later re-stage."""
    def writable(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(dest, onexc=writable)
    else:                                       # 3.10/3.11: onexc does not exist yet
        shutil.rmtree(dest, onerror=writable)


def copy(src, dest):
    """Replace `dest` with a copy of what files(src, skip=dest) lists: the walk is the
    second line of defence, so an outside-pointing link is never copied even when copy()
    runs with no validation in front of it, and `dest`'s own subtree is never listed.
    copytree with symlinks=False copies a linked file as its content; the ignore callable
    drops what files() skips (a linked directory, a dangling link, a fifo), and `dest`
    itself by realpath, compared under os.path.normcase like every other containment test
    here -- with `dest` inside `src` (a fixtures folder holding the run folder) the walk
    must never reach into its own destination."""
    if _is_link(dest):                          # never rmtree through a link
        try:
            os.unlink(dest)
        except OSError:                         # Windows: a directory link is an rmdir
            os.rmdir(dest)
    elif os.path.lexists(dest):
        _remove(dest)
    # only now is dest gone: what the walk lists and the copy skips is decided against
    # the folder as it stands -- a stale realpath of a dest that was a link into src
    # would drop the live folder it pointed at, and a previous copy at dest would be
    # listed as fixtures
    keep = {name for name, _ in files(src, skip=dest)}
    real_dest = os.path.normcase(os.path.realpath(dest))

    def ignore(folder, names):
        rel = os.path.relpath(folder, src)
        prefix = "" if rel == os.curdir else rel.replace(os.sep, "/") + "/"
        dropped = []
        for n in names:
            path = os.path.join(folder, n)
            if os.path.normcase(os.path.realpath(path)) == real_dest:
                dropped.append(n)                       # dest itself: src holds dest
                continue
            if prefix + n in keep:
                continue
            if os.path.isdir(path) and not _is_link(path):
                continue                        # a real subfolder: copytree walks into it
            dropped.append(n)
        return dropped

    shutil.copytree(src, dest, symlinks=False, ignore=ignore, copy_function=shutil.copyfile)
