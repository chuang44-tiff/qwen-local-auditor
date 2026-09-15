"""This repository is public: no personal machine details may be committed.

The patterns are deliberately generic (private address ranges, home-directory
paths, e-mail addresses) so the test itself names nothing it is guarding.
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

PATTERNS = {
    "private IPv4 address": re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
    "home directory path": re.compile(
        r"(?:/home/|/Users/|\b[A-Za-z]:[\\/]+Users[\\/]+)[A-Za-z0-9._-]+"),
    "e-mail address": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b|users\.noreply\.github\.com\b)"
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
