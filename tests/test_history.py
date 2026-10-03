import json
import os
import pathlib

from lib.builders import history

DATA = pathlib.Path(__file__).parent / "data"


def test_transcript_dir_encodes_the_project_path(monkeypatch, tmp_path):
    monkeypatch.delenv("QWEN_TRANSCRIPT_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    d = history.transcript_dir("/work/my.proj")
    assert os.path.dirname(d) == os.path.join(str(tmp_path), "projects")
    assert d.endswith("-work-my-proj")          # on Windows the drive letter adds a prefix


def test_events_for_a_file():
    ev = history.events_for("src/net.py", [str(DATA / "transcript-sample.jsonl")])
    kinds = [e.split("] ", 1)[1].split(":", 1)[0] for e in ev]
    assert kinds == ["USER", "EDIT Edit", "TEST RUN", "REASON"]
    assert all(e.startswith("[session:aaaa1111 2026-10-01T") for e in ev)
    assert "timeout at attempt 4" in ev[2]


def test_basename_matches_whole_names_only(tmp_path):
    # For src/net.py: a path prefix and punctuation around the name still name it,
    # but a longer name ("internet.py") must not drag this file's evidence along.
    def row(kind, text):
        return json.dumps({"type": kind, "sessionId": "bbbb2222-0",
                           "timestamp": "2026-10-01T09:00:00Z",
                           "message": {"role": kind, "content": text}})
    lines = [row("user", t) for t in
             ["fix net.py today", "see `net.py` here", "src/net.py is broken",
              "net.py: line 3 regressed", "internet.py is a different file",
              "net.py.bak is a backup", "net.py2 is another", "I edited net.py."]]
    lines.append(row("assistant", "internet.py needed retries because the upstream flakes"))
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ev = history.events_for("src/net.py", [str(p)])
    assert len(ev) == 5 and all("] USER: " in e for e in ev)
    assert not any(("internet" in e) or (".bak" in e) or ("net.py2" in e) for e in ev)


def test_sessions_refuse_links_outside(tmp_path):
    d = tmp_path / "proj"; d.mkdir()
    (d / "ok.jsonl").write_text("{}\n")
    outside = tmp_path / "secret.jsonl"; outside.write_text("{}\n")
    try:
        os.symlink(outside, d / "link.jsonl")
    except (OSError, NotImplementedError):
        return                                   # no symlinks on this platform
    assert [os.path.basename(p) for p in history.sessions(str(d))] == ["ok.jsonl"]


def test_build_withholds_without_events(monkeypatch, tmp_path):
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(DATA))
    items = history.enumerate_items({"repo": str(tmp_path), "files": "src/net.py,docs/none.md"})
    built = [history.build(i) for i in items]
    assert built[0].withheld is None and "TEST RUN" in built[0].context
    assert built[1].withheld and "no transcript" in built[1].withheld


def test_corrupt_lines_crlf_and_non_ascii(tmp_path):
    p = tmp_path / "s.jsonl"
    good = ('{"type":"user","sessionId":"cccc3333-0","timestamp":"2026-10-01T09:00:00Z",'
            '"message":{"role":"user","content":"fix caf\\u00e9.py\\r\\nplease"}}')
    p.write_bytes((good + '\r\n{"type":"assistant","sess\r\nnot json\r\n[1,2]\r\n').encode("utf-8"))
    ev = history.events_for("src/café.py", [str(p)])
    assert len(ev) == 1 and ev[0].startswith("[session:cccc3333 ")
    assert "\r" not in ev[0] and "\n" not in ev[0]
