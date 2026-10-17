"""Files a PowerShell command writes or deletes, found without running it."""
from __future__ import annotations

import ntpath
import posixpath
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from .paths import is_abs, is_windows_style, join
from .shell_syntax import ansi_c_end, strip_heredocs
from .shell_writes import ShellWrite

Write = ShellWrite

_PS_CMDLET_WORD_RE = re.compile(
    r"(?:"
    r"(?:Set|Add|Out|New|Copy|Move|Remove|Rename|Export|Tee)-[A-Z]\w+"
    r"|(?:Set-Content|Add-Content|Out-File|Tee-Object|Export-Csv"
    r"|New-Item|Copy-Item|Move-Item|Rename-Item|Remove-Item"
    r"|Set-Location|Push-Location|Invoke-WebRequest|Invoke-RestMethod)\b"
    r")",
    re.IGNORECASE,
)
_DOTNET_CALL_RE = re.compile(r"\[(?:System\.)?IO\.File\]::", re.IGNORECASE)
# Cheap pre-filter: the careful check below only has to run when some text looks
# like a cmdlet at all.
_PS_SNIFF_HINT_RE = re.compile(
    r"(?<![\w-])(?:" + _PS_CMDLET_WORD_RE.pattern + r")|\[(?:System\.)?IO\.File\]::",
    re.IGNORECASE,
)
_SHELL_BREAK_RE = re.compile(r"[;\n|&(){}]")
_SHELL_PREFIX_WORDS = frozenset({
    "!", "builtin", "case", "command", "do", "done", "elif", "else", "env", "esac", "exec",
    "fi", "for", "if", "nohup", "noglob", "sudo", "then", "time", "until", "while", "xargs",
})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

_HERE_DQ_RE = re.compile(r'@"[ \t]*\r?\n.*?\r?\n"@', re.DOTALL)
_HERE_SQ_RE = re.compile(r"@'[ \t]*\r?\n.*?\r?\n'@", re.DOTALL)
_BLOCK_COMMENT_RE = re.compile(r"<#.*?#>", re.DOTALL)

_DOTNET_WRITE_RE = re.compile(
    r"\[(?:System\.)?IO\.File\]::(?:WriteAllText|WriteAllLines|AppendAllText|AppendAllLines|WriteAllBytes)"
    r"\s*\(\s*(?:(['\"])([^'\"]*)\1|([^,)\s]+))",
    re.IGNORECASE,
)

_REDIR_OP_RE = re.compile(r"^(?:[1-6*]?>>|[1-6*]?>(?!&))$")
_REDIR_MERGE_RE = re.compile(r"^[1-6*]?>>&?[0-9]?$|^[1-6*]?✨$")
_STREAM_MERGE_PREFIX_RE = re.compile(r"^(?:[1-6*]?>>|[1-6*]?>)(?:&[1-6]?)?(.*)$")

_DEVICES = {
    "/dev/null",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/fd/1",
    "/dev/fd/2",
    "nul",
    "con",
    "prn",
    "aux",
    "$null",
}

_CONTENT_CMDS = {"set-content", "sc", "add-content", "ac", "out-file", "tee-object", "tee", "export-csv", "epcsv"}
_NEW_CMDS = {"new-item", "ni"}
_COPY_CMDS = {"copy-item", "cp", "copy", "cpi"}
_MOVE_CMDS = {"move-item", "mv", "move", "mi"}
_RENAME_CMDS = {"rename-item", "ren", "rni"}
_DELETE_CMDS = {"remove-item", "ri", "rm", "del", "rd", "rmdir", "erase"}
_CD_CMDS = {"set-location", "cd", "sl", "chdir", "push-location", "pushd"}
_WEB_CMDS = {"invoke-webrequest", "iwr", "invoke-restmethod", "irm"}

_PATH_PARAMS = {"-path", "-literalpath", "-filepath", "-lp", "-pspath"}
_DEST_PARAMS = {"-destination", "-dest"}
_NEWNAME_PARAMS = {"-newname"}
_NAME_PARAMS = {"-name", "-n"}
_ITEMTYPE_PARAMS = {"-itemtype", "-type"}
_OUTFILE_PARAMS = {"-outfile", "-o"}

# Parameters that consume a following value argument (not a switch flag).
_VALUE_PARAMS = {
    "-value",
    "-encoding",
    "-filter",
    "-include",
    "-exclude",
    "-delimiter",
    "-width",
    "-variable",
    "-stream",
    "-credential",
    "-Session",
    "-session",
    "-headers",
    "-body",
    "-method",
    "-contenttype",
    "-useragent",
    "-timeoutsec",
    "-maximumredirection",
}


def _shell_code(text: str) -> str:
    """`text` with quoted strings, ANSI-C strings and comments blanked, so that a cmdlet
    name inside data does not look like a command."""
    chars = list(text)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "$" and text.startswith("$'", i):
            close = ansi_c_end(text, i)
            end = n if close == -1 else close
        elif ch == "'":
            close = text.find("'", i + 1)
            end = n if close == -1 else close + 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "\n":
                    break
                j += 1
            end = j + 1 if j < n and text[j] == '"' else j
        elif ch == "#" and (text.rfind("\n", 0, i) + 1 == i or text[i - 1] in " \t\r;&|()<>"):
            close = text.find("\n", i)
            end = n if close == -1 else close
        else:
            i += 1
            continue
        for k in range(i, end):
            if chars[k] != "\n":
                chars[k] = " "
        i = end
    return "".join(chars)


def _powershell_sniff(code: str) -> bool:
    """True when a cmdlet or a .NET file call is the command word of a statement in `code`."""
    if _DOTNET_CALL_RE.search(code):
        return True
    for segment in _SHELL_BREAK_RE.split(code):
        words = segment.split()
        i = 0
        while i < len(words) and (words[i].lower() in _SHELL_PREFIX_WORDS or _ASSIGNMENT_RE.match(words[i])):
            i += 1
        if i < len(words) and _PS_CMDLET_WORD_RE.match(words[i]):
            return True
    return False


def shell_kind(step_or_tool: Any, command: Optional[str] = None) -> str:
    """Return `'powershell'` or `'posix'` for a `Step` or `(tool, command)` pair."""
    if hasattr(step_or_tool, "tool"):
        tool = str(getattr(step_or_tool, "tool") or "")
        inp = getattr(step_or_tool, "input", None) or {}
        cmd = str(inp.get("command") or "") if command is None else str(command)
    elif command is None:
        tool = "Bash"
        cmd = str(step_or_tool or "")
    else:
        tool = str(step_or_tool or "")
        cmd = str(command or "")
    if tool == "PowerShell":
        return "powershell"
    if tool == "Bash":
        if not _PS_SNIFF_HINT_RE.search(cmd):
            return "posix"
        stripped, _ = strip_heredocs(cmd)
        if _powershell_sniff(_shell_code(stripped)):
            return "powershell"
    return "posix"


def _strip_ps_herestrings(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _HERE_DQ_RE.sub('""', text)
    text = _HERE_SQ_RE.sub("''", text)
    return text


def _clean_ps_path(raw: Optional[str], cwd: Optional[str] = None) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    if not s or s == "-" or s.startswith("-"):
        return None
    if "$" in s or "*" in s or "?" in s or "`" in s:
        return None
    if s.lower() in _DEVICES or re.match(r"^(?:com|lpt)[1-9]$", s, re.I):
        return None
    if s.endswith(("/", "\\")):
        return None
    while s.startswith(("./", ".\\")):
        s = s[2:]
    if s in ("", ".", ".."):
        return None
    if cwd:
        s = join(cwd, s)
    if not is_abs(s):
        s = posixpath.normpath(s.replace("\\", "/"))
        if s in ("", "."):
            return None
    return s


def _join_dir_and_leaf(folder: str, leaf: str) -> str:
    if is_windows_style(folder):
        return ntpath.normpath(folder.rstrip("\\/") + "\\" + leaf)
    return posixpath.normpath(folder.rstrip("\\/") + "/" + leaf)


def _split_statements(text: str) -> List[str]:
    """Split PowerShell text into simple statements on unquoted `;`, `|`, and newlines."""
    parts: List[str] = []
    cur: List[str] = []
    quote: Optional[str] = None
    parens = 0
    i, n = 0, len(text)
    line_start = True

    while i < n:
        c = text[i]
        if quote == "'":
            cur.append(c)
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if quote == '"':
            cur.append(c)
            if c == "`" and i + 1 < n:
                cur.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        # Unquoted
        if c == "`" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2
                continue
            if text[i + 1] == "\r" and i + 2 < n and text[i + 2] == "\n":
                i += 3
                continue
            cur.append(c)
            cur.append(text[i + 1])
            i += 2
            line_start = False
            continue
        if c in ("'", '"'):
            quote = c
            cur.append(c)
            i += 1
            line_start = False
            continue
        if c == "#" and (line_start or (i > 0 and text[i - 1] in " \t\r\n;|(")):
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue
        if c == "(":
            parens += 1
            cur.append(c)
            i += 1
            line_start = False
            continue
        if c == ")" and parens > 0:
            parens -= 1
            cur.append(c)
            i += 1
            line_start = False
            continue
        if parens == 0 and c in ";|\r\n":
            stmt = "".join(cur).strip()
            if stmt:
                parts.append(stmt)
            cur = []
            line_start = True
            i += 1
            continue
        if c not in " \t":
            line_start = False
        cur.append(c)
        i += 1

    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _tokenize_stmt(stmt: str) -> Tuple[List[str], List[str]]:
    """Return `(words, redirect_targets)` for a single PowerShell statement."""
    raw_tokens: List[str] = []
    cur: List[str] = []
    quote: Optional[str] = None
    parens = 0
    i, n = 0, len(stmt)

    def flush() -> None:
        if cur:
            raw_tokens.append("".join(cur))
            cur.clear()

    while i < n:
        c = stmt[i]
        if quote == "'":
            if c == "'":
                if i + 1 < n and stmt[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                quote = None
                i += 1
                continue
            cur.append(c)
            i += 1
            continue
        if quote == '"':
            if c == "`" and i + 1 < n:
                cur.append(c)
                cur.append(stmt[i + 1])
                i += 2
                continue
            if c == '"':
                if i + 1 < n and stmt[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                quote = None
                i += 1
                continue
            cur.append(c)
            i += 1
            continue

        if c in " \t\r\n" and parens == 0:
            flush()
            i += 1
            continue
        if c in ("'", '"') and parens == 0:
            quote = c
            i += 1
            continue
        if c == "(":
            parens += 1
            cur.append(c)
            i += 1
            continue
        if c == ")" and parens > 0:
            parens -= 1
            cur.append(c)
            i += 1
            continue
        if parens == 0 and (
            stmt.startswith(">>", i)
            or c == ">"
            or (c in "123456*" and i + 1 < n and stmt[i + 1] == ">")
        ):
            flush()
            op_start = i
            if c in "123456*":
                i += 1
            if stmt.startswith(">>", i):
                i += 2
            else:
                i += 1
            if i < n and stmt[i] == "&":
                i += 1
                if i < n and stmt[i].isdigit():
                    i += 1
                raw_tokens.append(stmt[op_start:i])
                continue
            raw_tokens.append(stmt[op_start:i])
            continue
        cur.append(c)
        i += 1

    flush()

    words: List[str] = []
    redirs: List[str] = []
    idx = 0
    while idx < len(raw_tokens):
        tok = raw_tokens[idx]
        if "&" in tok and ">" in tok:
            idx += 1
            continue
        if _REDIR_OP_RE.match(tok):
            if idx + 1 < len(raw_tokens):
                redirs.append(raw_tokens[idx + 1])
                idx += 2
                continue
            idx += 1
            continue
        # Expand `-Param:Value` (when Value does not start with backslash like `-Path C:\...`)
        if tok.startswith("-") and ":" in tok:
            p_name, p_val = tok.split(":", 1)
            if p_name.lower() in (
                _PATH_PARAMS | _DEST_PARAMS | _NEWNAME_PARAMS | _NAME_PARAMS | _ITEMTYPE_PARAMS | _OUTFILE_PARAMS | _VALUE_PARAMS
            ):
                words.append(p_name)
                if p_val:
                    words.append(p_val)
                idx += 1
                continue
        words.append(tok)
        idx += 1

    return words, redirs


def _parse_args(
    args: List[str],
) -> Tuple[Dict[str, List[str]], List[str]]:
    """Split cmdlet arguments into named parameters (lowercased) and positional arguments."""
    named: Dict[str, List[str]] = {}
    positional: List[str] = []
    i = 0
    n = len(args)
    all_valued = (
        _PATH_PARAMS
        | _DEST_PARAMS
        | _NEWNAME_PARAMS
        | _NAME_PARAMS
        | _ITEMTYPE_PARAMS
        | _OUTFILE_PARAMS
        | _VALUE_PARAMS
    )
    while i < n:
        a = args[i]
        low = a.lower()
        if low in ("-whatif", "--"):
            if low == "-whatif":
                named.setdefault("-whatif", []).append("true")
            i += 1
            continue
        if low in all_valued:
            if i + 1 < n:
                # Comma-separated path lists: `Remove-Item a.txt,b.txt`
                val = args[i + 1]
                named.setdefault(low, []).append(val)
                i += 2
                continue
            i += 1
            continue
        if a.startswith("-"):
            # Unknown switch or parameter: if it's a known switch, consume just the flag
            i += 1
            continue
        positional.append(a)
        i += 1
    return named, positional


def _first_param(named: Dict[str, List[str]], keys: set[str]) -> List[str]:
    out: List[str] = []
    for k in keys:
        if k in named:
            out.extend(named[k])
    return out


@lru_cache(maxsize=4096)
def _ps_writes_cached(command: str) -> Tuple[ShellWrite, ...]:
    if not command or not command.strip():
        return ()
    text = _strip_ps_herestrings(command)
    text, _ = strip_heredocs(text)

    out: List[ShellWrite] = []
    cwd: Optional[str] = None

    for stmt in _split_statements(text):
        for m in _DOTNET_WRITE_RE.finditer(stmt):
            quoted_path = m.group(2)
            if quoted_path is not None:
                cleaned = _clean_ps_path(quoted_path, cwd)
                if cleaned:
                    out.append(ShellWrite(cleaned, added=1))

        words, redirs = _tokenize_stmt(stmt)
        for r_target in redirs:
            cleaned = _clean_ps_path(r_target, cwd)
            if cleaned:
                out.append(ShellWrite(cleaned, added=1))

        if not words:
            continue
        if words[0] == "&" and len(words) > 1:
            words = words[1:]
        cmd = words[0].lower()
        args = words[1:]
        named, pos = _parse_args(args)
        if "-whatif" in named:
            continue

        if cmd in _CD_CMDS:
            targets = _first_param(named, _PATH_PARAMS) or pos[:1]
            if targets:
                raw_cd = targets[0].strip("'\"")
                if raw_cd and not any(c in raw_cd for c in "$*?`"):
                    cwd = join(cwd, raw_cd) if cwd else raw_cd
                else:
                    cwd = None
            continue

        if cmd in _CONTENT_CMDS:
            targets = _first_param(named, _PATH_PARAMS) or pos[:1]
            vals = _first_param(named, {"-value"})
            if not vals and not _first_param(named, _PATH_PARAMS) and len(pos) > 1:
                vals = pos[1:]
            elif not vals and _first_param(named, _PATH_PARAMS) and pos:
                vals = pos
            added = 1
            if vals:
                v_str = "\n".join(vals).replace("\\n", "\n").replace("`n", "\n")
                added = max(1, len(v_str.splitlines()))
            for t in targets:
                cleaned = _clean_ps_path(t, cwd)
                if cleaned:
                    out.append(ShellWrite(cleaned, added=added))
            continue

        if cmd in _NEW_CMDS:
            item_types = [v.lower() for v in _first_param(named, _ITEMTYPE_PARAMS)]
            if any(t in ("directory", "folder", "container") for t in item_types):
                continue
            vals = _first_param(named, {"-value"})
            added = max(1, len("\n".join(vals).replace("\\n", "\n").replace("`n", "\n").splitlines())) if vals else 0
            paths_arg = _first_param(named, _PATH_PARAMS)
            names_arg = _first_param(named, _NAME_PARAMS)
            if paths_arg and names_arg:
                raw_target = _join_dir_and_leaf(paths_arg[0], names_arg[0])
                cleaned = _clean_ps_path(raw_target, cwd)
                if cleaned:
                    out.append(ShellWrite(cleaned, added=added))
            else:
                targets = paths_arg or names_arg or pos[:1]
                for t in targets:
                    cleaned = _clean_ps_path(t, cwd)
                    if cleaned:
                        out.append(ShellWrite(cleaned, added=added))
            continue

        if cmd in _COPY_CMDS or cmd in _MOVE_CMDS:
            is_move = cmd in _MOVE_CMDS
            srcs = _first_param(named, _PATH_PARAMS)
            dsts = _first_param(named, _DEST_PARAMS)
            rem = list(pos)
            if not srcs and rem:
                srcs = [rem.pop(0)]
            if not dsts and rem:
                dsts = [rem.pop(0)]
            if not srcs or not dsts:
                continue
            raw_src, raw_dst = srcs[0], dsts[0]
            clean_src = _clean_ps_path(raw_src, cwd)
            if not clean_src:
                # Source has wildcards/variables or is unresolvable -> skip
                continue
            raw_dst_unquoted = raw_dst.strip().strip("'\"")
            if raw_dst_unquoted.endswith(("/", "\\")):
                folder = _clean_ps_path(raw_dst_unquoted.rstrip("/\\"), cwd)
                if not folder:
                    continue
                leaf = ntpath.basename(clean_src.replace("/", "\\"))
                clean_dst: Optional[str] = _join_dir_and_leaf(folder, leaf)
            else:
                clean_dst = _clean_ps_path(raw_dst, cwd)
            if not clean_dst:
                continue
            if is_move:
                out.append(ShellWrite(clean_src, deleted=True))
            out.append(ShellWrite(clean_dst, source=clean_src))
            continue

        if cmd in _RENAME_CMDS:
            srcs = _first_param(named, _PATH_PARAMS)
            new_names = _first_param(named, _NEWNAME_PARAMS)
            rem = list(pos)
            if not srcs and rem:
                srcs = [rem.pop(0)]
            if not new_names and rem:
                new_names = [rem.pop(0)]
            if not new_names:
                continue
            clean_src = _clean_ps_path(srcs[0], cwd) if srcs else None
            if not clean_src:
                continue
            raw_new = new_names[0].strip().strip("'\"")
            if "/" not in raw_new and "\\" not in raw_new:
                parent = (
                    ntpath.dirname(clean_src)
                    if is_windows_style(clean_src)
                    else posixpath.dirname(clean_src)
                )
                target_str = _join_dir_and_leaf(parent, raw_new) if parent else raw_new
                clean_new = _clean_ps_path(target_str, None)
            else:
                clean_new = _clean_ps_path(raw_new, cwd)
            if clean_new:
                out.append(ShellWrite(clean_src, deleted=True))
                out.append(ShellWrite(clean_new, source=clean_src))
            continue

        if cmd in _DELETE_CMDS:
            targets = _first_param(named, _PATH_PARAMS) or pos
            for t in targets:
                for part in t.split(","):
                    cleaned = _clean_ps_path(part, cwd)
                    if cleaned:
                        out.append(ShellWrite(cleaned, deleted=True))
            continue

        if cmd in _WEB_CMDS:
            for t in _first_param(named, _OUTFILE_PARAMS):
                cleaned = _clean_ps_path(t, cwd)
                if cleaned:
                    out.append(ShellWrite(cleaned))

    merged: Dict[str, ShellWrite] = {}
    for w in out:
        prev = merged.get(w.path)
        if prev is None:
            merged[w.path] = w
        elif w.deleted:
            merged[w.path] = ShellWrite(w.path, deleted=True)
        else:
            merged[w.path] = ShellWrite(
                w.path, prev.added + w.added, deleted=False, source=w.source or prev.source
            )
    return tuple(merged.values())


def ps_writes(command: str) -> List[ShellWrite]:
    """Return files written or deleted by a PowerShell command string."""
    if len(command) > 64_000:
        return list(_ps_writes_cached.__wrapped__(command))
    return list(_ps_writes_cached(command))


def ps_deletes(command: str) -> List[str]:
    """Return file paths deleted by a PowerShell command string."""
    return [w.path for w in ps_writes(command) if w.deleted]
