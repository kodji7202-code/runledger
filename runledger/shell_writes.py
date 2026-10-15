"""Files a shell command writes, found without running it.

Agents often edit files through the shell instead of their edit tools:
`cat > f <<EOF`, `echo x >> f`, `tee`, `cp`, `mv`, `sed -i`, `touch`, or a
Python heredoc that calls open(path, "w"). This is a best-effort static
reading: targets that need expansion ($VAR, globs, ~) are skipped, and
line counts are known only for heredoc bodies written with cat or tee.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import List, Optional, Tuple

_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2")
_WRITE_OPS = (">", ">>", ">|", "&>", "&>>", "<>")
_UNRESOLVED = re.compile(r"[$`*?\[{]|^~")
_WRAPPERS = {"sudo", "env", "command", "nohup", "time", "exec", "builtin"}
_PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")
_PY_ASSIGN = re.compile(r"^[ \t]*(\w+)[ \t]*=[ \t]*(['\"])([^'\"\n]+)\2[ \t]*(?:#.*)?$", re.M)
_PY_TARGET = r"(?:(['\"])([^'\"\n]+)\1|(\w+))"
_PY_OPEN = re.compile(r"\bopen\(\s*" + _PY_TARGET + r"\s*,\s*(?:mode\s*=\s*)?(['\"])([^'\"]*)\4")
_PY_PATH = re.compile(r"\bPath\(\s*" + _PY_TARGET + r"\s*\)\s*\.\s*write_(?:text|bytes)\(")


@dataclass
class ShellWrite:
    path: str
    added: int = 0
    deleted: bool = False


def _heredocs(command: str) -> Tuple[str, List[str]]:
    """Remove heredoc bodies from the command text. Bodies are returned in the
    order their `<<` operators appear, so the tokenizer can claim them in turn."""
    lines = command.split("\n")
    kept: List[str] = []
    bodies: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for m in _HEREDOC.finditer(line):
            strip_tabs, delim = m.group(1) == "-", m.group(3)
            body: List[str] = []
            while i < len(lines):
                candidate = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if candidate == delim:
                    break
                body.append(lines[i - 1])
            bodies.append("\n".join(body))
    return "\n".join(kept), bodies


def _tokens(text: str) -> Optional[List[str]]:
    lex = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _is_operator(token: str) -> bool:
    return bool(token) and all(c in "();<>|&\n" for c in token)


def _commands(tokens: List[str], bodies: List[str]):
    """Yield (words, redirect_targets, heredoc_body) for each simple command."""
    words: List[str] = []
    targets: List[str] = []
    body: Optional[str] = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not _is_operator(tok):
            words.append(tok)
            i += 1
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok in _WRITE_OPS or (tok == ">&" and nxt is not None and not nxt.isdigit() and nxt != "-"):
            if words and words[-1].isdigit():
                words.pop()  # the fd number in `2> file`
            if nxt is not None and not _is_operator(nxt):
                targets.append(nxt)
            i += 2
        elif tok in ("<<", "<<-"):
            body = bodies.pop(0) if bodies else ""
            i += 2
        elif "<" in tok or tok == ">&":
            if tok == ">&" and words and words[-1].isdigit():
                words.pop()
            i += 2
        else:
            yield words, targets, body
            words, targets, body = [], [], None
            i += 1
    yield words, targets, body


def _positional(args: List[str], with_value: Tuple[str, ...] = ()) -> List[str]:
    out: List[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in with_value:
            skip = True
        elif a == "--":
            continue
        elif not a.startswith("-") or a == "-":
            out.append(a)
    return out


def _python_writes(source: str) -> List[str]:
    assigns = [(m.start(), m.group(1), m.group(3)) for m in _PY_ASSIGN.finditer(source)]

    def resolve(m: "re.Match[str]") -> Optional[str]:
        if m.group(2):
            return m.group(2)
        value = None
        for pos, name, literal in assigns:
            if pos > m.start():
                break
            if name == m.group(3):
                value = literal
        return value

    found: List[Tuple[int, str]] = []
    for m in _PY_OPEN.finditer(source):
        if re.search(r"[wax+]", m.group(5)):
            path = resolve(m)
            if path:
                found.append((m.start(), path))
    for m in _PY_PATH.finditer(source):
        path = resolve(m)
        if path:
            found.append((m.start(), path))
    return [p for _, p in sorted(found)]


def _sed_files(args: List[str]) -> List[str]:
    if not any(a == "-i" or a.startswith(("-i", "--in-place")) or
               (a.startswith("-") and not a.startswith("--") and "i" in a[1:]) for a in args):
        return []
    has_script = any(a in ("-e", "-f") or a.startswith(("--expression", "--file")) for a in args)
    files = _positional(args, ("-e", "-f", "-l"))
    return files if has_script else files[1:]


def _perl_files(args: List[str]) -> List[str]:
    if not any(a.startswith("-") and not a.startswith("--") and "i" in a[1:] for a in args):
        return []
    has_script = any(a == "-e" or a == "-E" for a in args)
    files = _positional(args, ("-e", "-E"))
    return files if has_script else files[1:]


def _command_writes(words: List[str], targets: List[str], body: Optional[str]) -> List[ShellWrite]:
    while words and (words[0] in _WRAPPERS or re.match(r"^\w+=", words[0])):
        words = words[1:]
    name = posixpath.basename(words[0]) if words else ""
    args = words[1:]
    body_lines = len(body.splitlines()) if body is not None and name in ("cat", "tee") else 0
    out = [ShellWrite(t, body_lines) for t in targets]
    if name == "tee":
        out += [ShellWrite(f, body_lines) for f in _positional(args)]
    elif name == "touch":
        out += [ShellWrite(f) for f in _positional(args, ("-d", "-r", "-t"))]
    elif name in ("cp", "install", "mv"):
        paths = _positional(args, ("-t", "-S", "-m", "-o", "-g", "--suffix"))
        if len(paths) >= 2:
            dest, sources = paths[-1], paths[:-1]
            for src in sources:
                target = posixpath.join(dest, posixpath.basename(src)) if dest.endswith("/") or len(sources) > 1 else dest
                out.append(ShellWrite(target))
                if name == "mv":
                    out.append(ShellWrite(src, deleted=True))
    elif name == "sed":
        out += [ShellWrite(f) for f in _sed_files(args)]
    elif name == "perl":
        out += [ShellWrite(f) for f in _perl_files(args)]
    elif _PYTHON.match(name):
        sources = [body] if body else []
        if "-c" in args and args.index("-c") + 1 < len(args):
            sources.append(args[args.index("-c") + 1])
        for src in sources:
            out += [ShellWrite(p) for p in _python_writes(src)]
    return out


def shell_writes(command: str) -> List[ShellWrite]:
    """Files the command writes or moves away. Paths stay as the command names
    them, joined to any directory the command `cd`s into first."""
    text, bodies = _heredocs(command)
    tokens = _tokens(text)
    if tokens is None:
        return []
    out: List[ShellWrite] = []
    cwd: Optional[str] = None
    lost = False  # after an unresolvable cd, relative paths point nowhere known
    for words, targets, body in _commands(tokens, bodies):
        stripped = [w for w in words if not re.match(r"^\w+=", w)]
        if stripped and stripped[0] in ("cd", "pushd"):
            dest = _positional(stripped[1:])
            if len(dest) == 1 and dest[0] != "-" and not _UNRESOLVED.search(dest[0]):
                cwd = posixpath.normpath(posixpath.join(cwd, dest[0]) if cwd else dest[0])
                lost = lost and not cwd.startswith("/")
            else:
                lost = True
            continue
        for w in _command_writes(words, targets, body):
            if not w.path or w.path == "-" or _UNRESOLVED.search(w.path):
                continue
            if lost and not w.path.startswith("/"):
                continue
            path = posixpath.normpath(posixpath.join(cwd, w.path) if cwd else w.path)
            if path == "/dev" or path.startswith("/dev/"):
                continue
            out.append(ShellWrite(path, w.added, w.deleted))
    return out
