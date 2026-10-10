"""This repository is public: no personal machine details may be committed.

The patterns are deliberately generic (private address ranges, home-directory
paths, e-mail addresses) so the test itself names nothing it is guarding.
"""
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

PATTERNS = {
    "private IPv4 address": re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b"),   # + CGNAT/Tailscale
    "home directory path": re.compile(
        r"(?:/home/|/Users/|\b[A-Za-z]:[\\/]+Users[\\/]+)[A-Za-z0-9._-]+"),
    "e-mail address": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b|users\.noreply\.github\.com\b|anthropic\.com\b(?<=noreply@anthropic\.com))"
        r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
}


def _tracked_files():
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True,
                             text=True, check=True).stdout.splitlines()
        return [ROOT / p for p in out if p]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def test_no_personal_machine_details_are_committed():
    hits = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, rx in PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append("%s:%d: %s (%s)" % (path.relative_to(ROOT), line, label, m.group(0)))
    assert not hits, "\n".join(hits)


# Words that narrate how the code was reviewed rather than what it does. Planning notes
# under docs/superpowers are working files, not shipped code, and are not checked.
PROCESS_NARRATION = re.compile(
    r"Review focus:|\bfix round\b|\bper the spec\b|\bMust-fix\b|\bShould-fix\b|\bTask \d+:")


CLI_SCRIPTS_GLOB = "skill/local-auditor/*.sh"


def test_cli_scripts_are_executable_in_git():
    """The commands are run as programs, so the executable bit belongs in the index: a
    clone that checks them out 644 cannot run them. The on-disk bit of this particular
    checkout proves nothing about a clone, so the index (mode 100755) is what is checked."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", CLI_SCRIPTS_GLOB],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    modes = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        modes[path] = meta.partition(" ")[0]
    assert modes, "git knows no CLI scripts under %s" % CLI_SCRIPTS_GLOB
    not_exec = sorted(p for p, m in modes.items() if m != "100755")
    assert not not_exec, ("committed without the executable bit: %s"
                          "  fix: chmod +x PATH && git update-index --chmod=+x PATH"
                          % ", ".join(not_exec))


def test_shipped_files_do_not_narrate_their_review():
    hits = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT)
        if rel.parts[:2] == ("docs", "superpowers") or rel == pathlib.Path(__file__).relative_to(ROOT):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in PROCESS_NARRATION.finditer(text):
            hits.append("%s:%d: %s" % (rel, text.count("\n", 0, m.start()) + 1, m.group(0)))
    assert not hits, "\n".join(hits)
