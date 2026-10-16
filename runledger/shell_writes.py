"""Files a shell command writes, found without running it.

Agents often edit files through the shell instead of their edit tools:
`cat > f <<EOF`, `echo x >> f`, `tee`, `cp`, `mv`, `sed -i`, `touch`, or a
Python heredoc that calls open(path, "w"). This is a best-effort static
reading: targets that need expansion ($VAR, globs, ~) are skipped, and
line counts are known only for heredoc bodies written with cat or tee.
"""
from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Tuple

from .paths import is_abs, is_windows_style, join

_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2")
_WRITE_OPS = (">", ">>", ">|", "&>", "&>>", "<>")
_UNRESOLVED = re.compile(r"[$`*?\[{]|^~")
_WORD_BREAKS = " \t\r\n();<>|&"
_WIN_WORD = re.compile(r"[A-Za-z]:\\[^\s();<>|&'\"]*")
_WRAPPERS = {"sudo", "env", "command", "nohup", "time", "exec", "builtin"}
_PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")
_PY_ASSIGN = re.compile(r"^[ \t]*(\w+)[ \t]*=[ \t]*(['\"])([^'\"\n]+)\2[ \t]*(?:#.*)?$", re.M)
_PY_TARGET = r"(?:(['\"])([^'\"\n]+)\1|(\w+))"
_PY_OPEN = re.compile(r"\bopen\(\s*" + _PY_TARGET + r"\s*,\s*(?:mode\s*=\s*)?(['\"])([^'\"]*)\4")
_PY_PATH = re.compile(r"\bPath\(\s*" + _PY_TARGET + r"\s*\)\s*\.\s*write_(?:text|bytes)\(")


_DB_FILE = re.compile(r"\.(?:db|sqlite3?|db3)$", re.I)
_WRITE_SQL = re.compile(
    r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update\s+[\w\"'`.]+\s+set|delete\s+from|replace\s+into"
    r"|alter\s+table|create\s+(?:unique\s+|temp(?:orary)?\s+|virtual\s+)?(?:table|index|view|trigger)"
    r"|drop\s+(?:table|index|view|trigger)|vacuum)\b"
    r"|^\s*\.(?:read|import|restore)\b", re.I | re.M)
_PY_CONNECT = re.compile(r"\bsqlite3\.connect\(\s*" + _PY_TARGET)
_SQLITE_VALUE_OPTS = ("-cmd", "-init", "-separator", "-newline", "-nullvalue", "-mmap", "-vfs",
                      "-lookaside", "-pagecache", "-heap", "-maxsize")
_RUNNERS = {"npx", "pnpx", "bunx", "bundle", "poetry", "pipenv", "uv"}
_MIGRATE_SCRIPT = re.compile(r"^migrat(?:e|ions?)(?:[_-]\w+)?(?:\.(?:py|js|mjs|cjs|ts|sh|rb))?$", re.I)
# Every receipt reads each command several times (file changes, risk, quality, each renderer),
# so parses are cached. Longer commands are parsed each time to keep the cache's memory bounded.
_CACHE_MAX_CHARS = 64_000
_CACHE_ENTRIES = 4096


@dataclass(frozen=True)
class ShellWrite:
    path: str
    added: int = 0
    deleted: bool = False
    source: Optional[str] = None  # what cp, mv or install copied to `path`


@dataclass
class DbWrite:
    kind: str                     # "write": changes a database file; "migrate": runs a migration tool
    command: str                  # the simple command, as written
    target: Optional[str] = None  # the database file, when the command names one


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


def _keep_windows_paths(text: str) -> str:
    """Double the backslashes of unquoted words that start with a drive ('C:\\x'), so POSIX
    tokenizing keeps them: on Windows they are separators, not escapes."""
    if ":\\" not in text:
        return text
    out: List[str] = []
    quote: Optional[str] = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < n:
                out.append(text[i:i + 2])
                i += 2
                continue
        elif c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        elif i == 0 or text[i - 1] in _WORD_BREAKS:
            m = _WIN_WORD.match(text, i)
            if m:
                out.append(m.group().replace("\\", "\\\\"))
                i = m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _tokens(text: str) -> Optional[List[str]]:
    lex = shlex.shlex(_keep_windows_paths(text), posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _is_operator(token: str) -> bool:
    return bool(token) and all(c in "();<>|&\n" for c in token)


def _commands(tokens: List[str], bodies: List[str]):
    """Yield (words, redirect_targets, heredoc_body, input_files) for each simple command."""
    words: List[str] = []
    targets: List[str] = []
    inputs: List[str] = []
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
            if tok == "<" and nxt is not None and not _is_operator(nxt):
                inputs.append(nxt)
            i += 2
        else:
            yield words, targets, body, inputs
            words, targets, inputs, body = [], [], [], None
            i += 1
    yield words, targets, body, inputs


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
                into = dest.endswith(("/", "\\") if is_windows_style(dest) else "/") or len(sources) > 1
                name_of = ntpath.basename if is_windows_style(src) else posixpath.basename
                target = join(dest, name_of(src)) if into else dest
                out.append(ShellWrite(target, source=src))
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


_Parsed = Tuple[Tuple[Optional[str], bool, Tuple[str, ...], Tuple[str, ...], Optional[str], Tuple[str, ...]], ...]


def _parse(command: str) -> _Parsed:
    """(cwd, lost, words, targets, body, inputs) for each simple command that is not a cd.
    `cwd` is the directory the command `cd`s into first; `lost` is true after an
    unresolvable cd, when relative paths point nowhere known."""
    text, bodies = _heredocs(command)
    tokens = _tokens(text)
    if tokens is None:
        return ()
    cwd: Optional[str] = None
    lost = False
    out = []
    for words, targets, body, inputs in _commands(tokens, bodies):
        stripped = [w for w in words if not re.match(r"^\w+=", w)]
        if stripped and stripped[0] in ("cd", "pushd"):
            dest = _positional(stripped[1:])
            if len(dest) == 1 and dest[0] != "-" and not _UNRESOLVED.search(dest[0]):
                cwd = join(cwd, dest[0])
                lost = lost and not is_abs(cwd)
            else:
                lost = True
            continue
        out.append((cwd, lost, tuple(words), tuple(targets), body, tuple(inputs)))
    return tuple(out)


_parse_cached = lru_cache(maxsize=_CACHE_ENTRIES)(_parse)


def _resolver(cwd: Optional[str], lost: bool):
    def resolve(path: Optional[str]) -> Optional[str]:
        if not path or path == "-" or _UNRESOLVED.search(path):
            return None
        if lost and not is_abs(path):
            return None
        return join(cwd, path)
    return resolve


def _walk(command: str):
    """Yield (resolve, words, targets, body, inputs) for each simple command that is not a cd.
    `resolve(path)` joins a path to the directory the command `cd`s into first, or
    returns None when the path cannot be known."""
    parsed = _parse_cached(command) if len(command) <= _CACHE_MAX_CHARS else _parse(command)
    for cwd, lost, words, targets, body, inputs in parsed:
        yield _resolver(cwd, lost), list(words), list(targets), body, list(inputs)


def _shell_writes(command: str) -> Tuple[ShellWrite, ...]:
    out: List[ShellWrite] = []
    for resolve, words, targets, body, _ in _walk(command):
        for w in _command_writes(words, targets, body):
            path = resolve(w.path)
            if path is None or path == "/dev" or path.startswith("/dev/"):
                continue
            out.append(ShellWrite(path, w.added, w.deleted, resolve(w.source) if w.source else None))
    return tuple(out)


_shell_writes_cached = lru_cache(maxsize=_CACHE_ENTRIES)(_shell_writes)


def shell_writes(command: str) -> List[ShellWrite]:
    """Files the command writes or moves away. Paths stay as the command names
    them, joined to any directory the command `cd`s into first."""
    if len(command) > _CACHE_MAX_CHARS:
        return list(_shell_writes(command))
    return list(_shell_writes_cached(command))


def _python_db_writes(source: str) -> List[str]:
    """Database files a Python snippet opens with sqlite3 when it also runs SQL that changes data."""
    if not (_WRITE_SQL.search(source) or ".executescript(" in source):
        return []
    assigns = [(m.start(), m.group(1), m.group(3)) for m in _PY_ASSIGN.finditer(source)]
    found = []
    for m in _PY_CONNECT.finditer(source):
        path = m.group(2)
        if not path:
            for pos, name, literal in assigns:
                if pos > m.start():
                    break
                if name == m.group(3):
                    path = literal
        if path and _DB_FILE.search(path):
            found.append(path)
    return found


def _is_migration_command(words: List[str]) -> bool:
    words = [w.lower() for w in words]
    while words and words[0] in _RUNNERS | {"run", "exec"}:
        words = words[1:]
    if not words:
        return False
    name, args = posixpath.basename(words[0]), words[1:]
    joined = " ".join(args)
    if _PYTHON.match(name) or name in ("node", "bash", "sh", "ruby", "deno", "bun", "ts-node", "tsx"):
        if args[:1] == ["-m"]:
            return _is_migration_command(args[1:])
        script = next((a for a in args if not a.startswith("-")), "")
        base = posixpath.basename(script)
        if base == "manage.py":
            return "migrate" in args
        return bool(_MIGRATE_SCRIPT.match(base))
    if _MIGRATE_SCRIPT.match(name) and words[0] != name:  # ./migrate.sh, scripts/migrate
        return True
    checks = {
        "alembic": r"\b(upgrade|downgrade)\b",
        "flask": r"\bdb\s+(upgrade|downgrade|migrate)\b",
        "rails": r"\bdb:(migrate|rollback|schema:load|setup|reset)\b",
        "rake": r"\bdb:(migrate|rollback|schema:load|setup|reset)\b",
        "prisma": r"\bmigrate\s+(dev|deploy|reset)\b|\bdb\s+push\b",
        "knex": r"\bmigrate:(latest|up|down|rollback)\b",
        "sequelize": r"\bdb:migrate\b",
        "sequelize-cli": r"\bdb:migrate\b",
        "flyway": r"\bmigrate\b",
        "goose": r"\b(up|down|redo|reset)\b",
        "dbmate": r"\b(up|migrate|rollback|down)\b",
        "migrate": r"\b(up|down|goto|force)\b",
        "diesel": r"\bmigration\s+run\b",
        "sqlx": r"\bmigrate\s+run\b",
    }
    if name in checks:
        return bool(re.search(checks[name], joined))
    if name in ("npm", "pnpm", "yarn", "bun"):
        script = [a for a in args if a not in ("run", "run-script") and not a.startswith("-")]
        return bool(script) and bool(re.match(r"^(db:)?migrat", script[0]))
    return False


def database_writes(command: str) -> List[DbWrite]:
    """Commands that change a local database file (sqlite3 with write SQL or an SQL
    script on stdin, Python sqlite3 code that runs DDL or DML) or run a migration tool.
    Read-only queries are not reported."""
    out: List[DbWrite] = []
    for resolve, words, _targets, body, inputs in _walk(command):
        while words and (words[0] in _WRAPPERS or re.match(r"^\w+=", words[0])):
            words = words[1:]
        if not words:
            continue
        text = " ".join(words)
        name = posixpath.basename(words[0])
        if name == "sqlite3":
            args = _positional(words[1:], _SQLITE_VALUE_OPTS)
            if not args or not _DB_FILE.search(args[0]):
                continue
            cmds = [words[i + 1] for i, w in enumerate(words[:-1]) if w == "-cmd"]
            sql = "\n".join(args[1:] + cmds + ([body] if body else []))
            if inputs or _WRITE_SQL.search(sql):
                out.append(DbWrite("write", text, resolve(args[0]) or args[0]))
        elif _PYTHON.match(name):
            sources = [body] if body else []
            if "-c" in words and words.index("-c") + 1 < len(words):
                sources.append(words[words.index("-c") + 1])
            for src in sources:
                for path in _python_db_writes(src):
                    out.append(DbWrite("write", words[0], resolve(path) or path))
            if not sources and _is_migration_command(words):
                out.append(DbWrite("migrate", text))
        elif _is_migration_command(words):
            out.append(DbWrite("migrate", text))
    return out


def _rm_targets(words: List[str]) -> List[str]:
    """Files an rm, unlink or `git rm` removes from the working tree."""
    while words and (words[0] in _WRAPPERS or re.match(r"^\w+=", words[0])):
        words = words[1:]
    name = posixpath.basename(words[0]) if words else ""
    if name in ("rm", "unlink"):
        return _positional(words[1:])
    if name == "git":
        args, base = words[1:], ""
        while args and args[0].startswith("-"):  # git -C dir / -c k=v take a value
            if args[0] == "-C" and len(args) > 1:
                base = posixpath.join(base, args[1])
            args = args[2:] if args[0] in ("-C", "-c") else args[1:]
        if args and args[0] == "rm" and "--cached" not in args:
            return [posixpath.join(base, t) if base else t for t in _positional(args[1:])]
    return []


def shell_deletes(command: str) -> List[str]:
    """Paths the command deletes with rm, unlink or `git rm` (not `git rm --cached`),
    joined to any directory the command `cd`s into first. Globs and variables are skipped."""
    out: List[str] = []
    for resolve, words, _, _, _ in _walk(command):
        for target in _rm_targets(words):
            path = resolve(target)
            if path is not None and not path.startswith("/dev/"):
                out.append(path)
    return out
