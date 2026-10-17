"""Quote- and comment-aware POSIX shell heredoc scanner."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Tuple

_DELIM_RE = re.compile(
    r"""(?P<dash>-?)[ \t]*(?:\\'([^']*?)\\'|\\"([^"]*?)\\"|'([^']*)'|"([^"]*)"|\\?([A-Za-z_][\w.-]*))"""
)
_ROOT_TOKEN_RE = re.compile(r"""[\n'"\\#$<)]""")
_DQ_TOKEN_RE = re.compile(r"""[\n"\\$]""")


@dataclass(frozen=True)
class Heredoc:
    op_start: int
    op_end: int
    body_start: int
    body_end: int
    end: int
    delimiter: str
    strip_tabs: bool
    body: str


def find_heredocs(text: str) -> List[Heredoc]:
    """Return every unquoted POSIX heredoc in `text` with its operator and body spans.

    Ignores `<<` inside `'...'`, plain `"..."`, `#` comments, `$((...))` arithmetic,
    and `<<<` here-strings, while recognizing `<<` inside `"$(...)"` command substitutions.
    """
    if "<<" not in text:
        return []
    n = len(text)
    i = 0
    line_start = 0
    stack: List[str] = ["root"]
    pending: List[Tuple[int, int, str, bool]] = []
    out: List[Heredoc] = []

    def consume_pending(nl_pos: int) -> int:
        pos = nl_pos + 1 if nl_pos < n else n
        for op_start, op_end, delim, strip_tabs in pending:
            b_start = pos
            b_end = n
            h_end = n
            cur = b_start
            while cur < n:
                nl = text.find("\n", cur)
                line_end = nl if nl != -1 else n
                raw = text[cur:line_end].rstrip("\r")
                check = raw.lstrip("\t") if strip_tabs else raw
                if check == delim:
                    b_end = cur - 1 if cur > b_start and text[cur - 1] == "\n" else cur
                    if b_end > b_start and text[b_end - 1] == "\r":
                        b_end -= 1
                    h_end = nl + 1 if nl != -1 else n
                    pos = h_end
                    break
                if nl == -1:
                    pos = n
                    break
                cur = nl + 1
            else:
                b_end = n
                h_end = n
                pos = n
            body = text[b_start:b_end]
            out.append(
                Heredoc(
                    op_start=op_start,
                    op_end=op_end,
                    body_start=b_start,
                    body_end=b_end,
                    end=h_end,
                    delimiter=delim,
                    strip_tabs=strip_tabs,
                    body=body,
                )
            )
        pending.clear()
        return pos

    while i < n:
        mode = stack[-1]

        if mode == "sq":
            q = text.find("'", i)
            nl = text.find("\n", i) if pending else -1
            if nl != -1 and (q == -1 or nl < q):
                i = consume_pending(nl)
                line_start = i
                continue
            if q == -1:
                break
            stack.pop()
            i = q + 1
            continue

        if mode == "arith":
            close = text.find("))", i)
            nl = text.find("\n", i) if pending else -1
            if nl != -1 and (close == -1 or nl < close):
                i = consume_pending(nl)
                line_start = i
                continue
            if close == -1:
                break
            stack.pop()
            i = close + 2
            continue

        if mode == "dq":
            m_tok = _DQ_TOKEN_RE.search(text, i)
            if m_tok is None:
                break
            i = m_tok.start()
            ch = text[i]
            if ch == "\n":
                if pending:
                    i = consume_pending(i)
                    line_start = i
                    continue
                i += 1
                line_start = i
                continue
            if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
                i += 2
                continue
            if ch == '"':
                stack.pop()
                i += 1
                continue
            if text.startswith("$((", i):
                stack.append("arith")
                i += 3
                continue
            if text.startswith("$(", i):
                stack.append("subst")
                i += 2
                continue
            i += 1
            continue

        # mode in ("root", "subst")
        m_tok = _ROOT_TOKEN_RE.search(text, i)
        if m_tok is None:
            break
        i = m_tok.start()
        ch = text[i]

        if ch == "\n":
            if pending:
                i = consume_pending(i)
                line_start = i
                continue
            i += 1
            line_start = i
            continue

        if ch == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2
                line_start = i
                continue
            i += 2
            continue
        if ch == "'":
            stack.append("sq")
            i += 1
            continue
        if ch == '"':
            stack.append("dq")
            i += 1
            continue
        if text.startswith("$((", i):
            stack.append("arith")
            i += 3
            continue
        if text.startswith("$(", i):
            stack.append("subst")
            i += 2
            continue
        if mode == "subst" and ch == ")":
            stack.pop()
            i += 1
            continue
        if ch == "#" and (i == line_start or text[i - 1] in " \t\r\n;&|()<>"):
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue
        if ch == "<":
            if text.startswith("<<<", i):
                while i < n and text[i] == "<":
                    i += 1
                continue
            if text.startswith("<<", i):
                m = _DELIM_RE.match(text, i + 2)
                if m:
                    dash = m.group("dash") == "-"
                    delim = next(g for g in m.groups()[1:] if g is not None)
                    pending.append((i, m.end(), delim, dash))
                    i = m.end()
                    continue
        i += 1

    if pending:
        consume_pending(n)

    return out


@lru_cache(maxsize=64)
def _strip_heredocs_cached(text: str) -> Tuple[str, Tuple[str, ...]]:
    heredocs = find_heredocs(text)
    if not heredocs:
        return text, ()
    bodies = tuple(h.body for h in heredocs)
    parts: List[str] = []
    pos = 0
    for h in heredocs:
        if h.body_start > pos:
            parts.append(text[pos:h.body_start])
        pos = max(pos, h.end)
    if pos < len(text):
        parts.append(text[pos:])
    return "".join(parts), bodies


def strip_heredocs(text: str) -> Tuple[str, List[str]]:
    """Remove heredoc bodies from `text` and return `(stripped_text, bodies)`."""
    if "<<" not in text:
        return text, []
    stripped, bodies = _strip_heredocs_cached(text)
    return stripped, list(bodies)
