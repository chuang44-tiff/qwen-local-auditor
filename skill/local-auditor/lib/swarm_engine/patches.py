"""Paths a git patch touches: git's C-quoted paths unquoted, both sides of a rename."""
import re

# the per-file lines: the only path sources that survive a path containing spaces, for
# git quotes a `diff --git` header path only for bytes it must escape, never for a space
_FILE_LINE = re.compile(r"^(--- |\+\+\+ |rename from |rename to |copy from |copy to )(.*)$")
_SIDES = {"--- ": "a", "+++ ": "b"}
_C_NAMED = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}


def _unquote(tok):
    """A git-quoted path token from a `diff --git` line ("a/x y" with \\303\\274-style
    octal escapes): the C-style body becomes bytes and decodes as utf-8/surrogateescape,
    so a path holding non-UTF-8 bytes comes back exactly as git sees it."""
    if not (len(tok) > 1 and tok.startswith('"') and tok.endswith('"')):
        return tok
    body, out, i = tok[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            n = body[i + 1:i + 4]
            if re.fullmatch(r"[0-7]{3}", n) and int(n, 8) < 256:   # git emits <= \377
                out.append(int(n, 8))
                i += 4
                continue
            if body[i + 1] in _C_NAMED:
                out.append(_C_NAMED[body[i + 1]])
                i += 2
                continue
            if body[i + 1] in '"\\':
                out.extend(body[i + 1].encode("utf-8"))
                i += 2
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "surrogateescape")


def _diff_tokens(rest):
    """The two path tokens of a `diff --git` line: bare words, or C-quoted runs that may
    hold spaces (\" and \\\\ inside do not end the quote)."""
    toks, i = [], 0
    while len(toks) < 2 and i < len(rest):
        if rest[i] == " ":
            i += 1
        elif rest[i] == '"':
            j = i + 1
            while j < len(rest) and rest[j] != '"':
                j += 2 if rest[j] == "\\" else 1
            toks.append(rest[i:min(j + 1, len(rest))])
            i = j + 1
        else:
            j = rest.find(" ", i)
            j = len(rest) if j == -1 else j
            toks.append(rest[i:j])
            i = j
    return toks


def _file_path(line):
    """The path a per-file metadata line names, without its a/ or b/ side prefix: ---
    and +++ carry one, rename/copy from-to name the path bare, and /dev/null is not a
    path. A trailing tab-separated timestamp (a plain `diff` stamps one) comes off
    before the C-style unquoting, which needs the quotes whole."""
    m = _FILE_LINE.match(line)
    if not m:
        return None
    p = _unquote(m.group(2).split("\t", 1)[0])
    if not p or p == "/dev/null":
        return None
    side = _SIDES.get(m.group(1))
    if side and p.startswith(side + "/"):
        p = p[len(side) + 1:]
    return p


def _strip_side(p, side):
    """One half of a `diff --git` header path: its a/ or b/ prefix comes off."""
    pre = side + "/"
    return p[len(pre):] if p.startswith(pre) else p


def _header_paths(rest):
    """Both paths of a `diff --git` header's tail, for a section without any ---/+++
    lines to trust (a binary-only change): a quoted pair splits as in _diff_tokens, an
    unquoted one on its one " b/" separator -- and when the path itself contains " b/"
    so the split is ambiguous, only the identical-path form `a/X b/X` names the halves."""
    if rest.startswith('"'):
        return [p for p in (_strip_side(_unquote(t), s)
                            for t, s in zip(_diff_tokens(rest), "ab")) if p]
    if rest.count(" b/") == 1:
        old, _, new = rest.partition(" b/")
        return [p for p in (_strip_side(old, "a"), _strip_side(new, "b")) if p]
    m = re.fullmatch(r"a/(.*) b/\1", rest)
    return [m.group(1)] if m else []


def patch_paths(patch):
    """Every path a git patch touches (both sides of a rename): git's C-quoted paths
    unquoted, each without its a/ or b/ prefix, de-duplicated in order of appearance.
    The paths come from the reliable per-file lines --- / +++ and rename/copy from-to;
    a section with none of them (a binary-only change) falls back to its `diff --git`
    header."""
    out = []

    def add(p):
        if p and p not in out:
            out.append(p)

    header, in_meta, got = None, False, False
    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            if header is not None and not got:
                for p in _header_paths(header):
                    add(p)
            header, in_meta, got = line[len("diff --git "):], True, False
        elif line.startswith("@@"):
            in_meta = False
        elif header is not None and in_meta:
            p = _file_path(line)
            if p is not None:
                got = True
                add(p)
    if header is not None and not got:
        for p in _header_paths(header):
            add(p)
    return out
