"""MCP risk by verb, and the SQL arguments of execute-class tools.

Pure functions, no I/O: the live guard and the receipts share them, so a call is scored the
same way whether it is decided in the moment or read back from a transcript. The verb
vocabulary follows the tool name in both shapes agents report:

    mcp__<server>__<tool>     Claude Code and Codex
    <server>__<tool>          Factory Droid connector tools (the adapters canonicalise these)

The SQL body is read to classify the call and is then dropped: it never reaches a reason, a
log row or an approval request, and an enormous argument is capped before any pattern runs.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

MCP_READ_VERBS = frozenset(
    "get list read search describe show fetch find query view check diff validate count lookup "
    "inspect status health stat preview export download watch tail".split())
MCP_DESTRUCTIVE_VERBS = frozenset(
    "delete remove destroy drop purge wipe truncate terminate kill revoke uninstall erase "
    "reset unpublish deprovision decommission nuke force".split())
MCP_WRITE_VERBS = frozenset(
    "create update add set put patch push merge deploy publish send post invite follow "
    "unfollow like unlike retweet mute unmute pause unpause restore apply write edit insert "
    "upsert rename move copy upload cancel disconnect transfer grant run execute start stop "
    "restart enable disable install import commit approve reject close reopen".split())

LABEL_DESTRUCTIVE = "Called a destructive MCP tool"
LABEL_STATE_CHANGING = "Called a state-changing MCP tool"
LABEL_EXTERNAL = "Called an external MCP tool"
SQL_LABELS = {
    "drop": "Ran SQL that drops or truncates data",
    "truncate": "Ran SQL that truncates a table",
    "delete_no_where": "Ran SQL that deletes rows without a WHERE clause",
    "update_no_where": "Ran SQL that updates rows without a WHERE clause",
    "drop_column": "Ran SQL that drops a column",
    "write": "Ran SQL that changes data",
    "unreadable": "Ran SQL the guard could not read",
}
SQL_HIGH_CLASSES = frozenset({"drop", "truncate", "delete_no_where", "update_no_where", "drop_column"})

SQL_MAX_CHARS = 20000
SQL_ARGUMENT_KEYS = ("query", "sql", "statement")
_TOOL_SQL_TOKENS = frozenset({"sql", "query", "statement", "execute"})
_MAX_SHOWN_NAME = 120

_SQL_HIGH_RULES: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("drop", re.compile(r"\bdrop\s+(?:table|database|schema|view|index|role|user)\b", re.I)),
    ("truncate", re.compile(r"\btruncate\b", re.I)),
    ("delete_no_where", re.compile(r"\bdelete\s+from\s+\S+(?![^;]*\bwhere\b)", re.I)),
    ("update_no_where", re.compile(r"\bupdate\s+\S+\s+set\b(?![^;]*\bwhere\b)", re.I)),
    ("drop_column", re.compile(r"\balter\s+table\s+\S+\s+drop\b", re.I)),
)
_SQL_CHANGE_RE = re.compile(r"\b(?:insert|update|delete|alter|create|grant|revoke|merge|replace)\b", re.I)
_SQL_FIRST_WORD_RE = re.compile(r"\s*([A-Za-z_]+)")
_SQL_FIRST_WORDS = frozenset(
    "select insert update delete drop truncate alter create grant revoke merge replace with explain "
    "show describe desc use set values pragma vacuum analyze reindex copy do lock notify refresh "
    "begin commit rollback savepoint call prepare deallocate execute discard cluster checkpoint "
    "table comment".split())


def _chunks(leaf: str) -> List[str]:
    out: List[str] = []
    buf: List[str] = []
    for ch in leaf:
        if ch.isalnum():
            buf.append(ch)
        elif buf:
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return out


def mcp_tokens(leaf: str) -> List[str]:
    """The words of a tool leaf name, lower case: `deleteBranch`, `delete-branch` and
    `delete_branch` all give ["delete", "branch"]. Separators are anything that is not a
    letter or a digit, and a lower-to-upper (or digit-to-upper) change starts a new word."""
    tokens: List[str] = []
    for chunk in _chunks(leaf):
        start = 0
        for i in range(1, len(chunk)):
            prev, cur = chunk[i - 1], chunk[i]
            if cur.isupper() and (prev.islower() or prev.isdigit()):
                tokens.append(chunk[start:i].lower())
                start = i
            elif cur.isupper() and prev.isupper() and i + 1 < len(chunk) and chunk[i + 1].islower():
                tokens.append(chunk[start:i].lower())
                start = i
        word = chunk[start:].lower()
        if word:
            tokens.append(word)
    return [t for t in tokens if t]


def mcp_parts(tool: str) -> Optional[Tuple[str, str]]:
    """`(server, tool)` of an MCP call name, or None when the name is not one.

    `mcp__<server>__<tool>` (Claude Code, Codex) and the bare `<server>__<tool>` names of
    Factory Droid connector tools are both accepted. A name that carries no second separator
    keeps an empty server, so the reason names the tool alone."""
    name = tool.strip() if isinstance(tool, str) else ""
    if not name:
        return None
    prefixed = name.startswith("mcp__")
    if prefixed:
        name = name[len("mcp__"):]
    server, sep, leaf = name.partition("__")
    if not sep and not prefixed:
        return None
    if not prefixed and (not server or not leaf):
        return None
    return server, leaf


def sql_argument(tool_input: Any) -> Optional[str]:
    """The SQL text of a call: the first non-empty string under `query`, `sql` or `statement`,
    stripped and capped at SQL_MAX_CHARS. None when there is no readable string."""
    if not isinstance(tool_input, dict):
        return None
    for key in SQL_ARGUMENT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:SQL_MAX_CHARS]
    return None


def blank_sql_literals(sql: str) -> str:
    """The statement with string literals and comments replaced by spaces, so a `DROP TABLE x`
    inside a string or a comment cannot be read as a statement."""
    out: List[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 2 if (sql[i] == "\\" and i + 1 < n) else 1
            out.append(" ")
        elif ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def sql_statement_class(sql: str) -> str:
    """The class of one SQL statement: a `SQL_LABELS` key for a destructive or data-changing
    statement, else "other" (read-only or unrecognised)."""
    text = blank_sql_literals(sql)
    for name, rx in _SQL_HIGH_RULES:
        if rx.search(text):
            return name
    return "write" if _SQL_CHANGE_RE.search(text) else "other"


def looks_like_sql(sql: str) -> bool:
    """Whether the argument opens like a statement ("hello" and "release notes" do not)."""
    match = _SQL_FIRST_WORD_RE.match(blank_sql_literals(sql))
    return bool(match) and match.group(1).lower() in _SQL_FIRST_WORDS


def _verb_sets(extra: Optional[Dict[str, Any]]) -> Tuple[Set[str], Set[str], Set[str]]:
    """The three vocabularies, extended by `extra["mcp_verbs"]` (the user's own words)."""
    verbs = extra.get("mcp_verbs") if isinstance(extra, dict) else None
    sets = [set(MCP_READ_VERBS), set(MCP_DESTRUCTIVE_VERBS), set(MCP_WRITE_VERBS)]
    if isinstance(verbs, dict):
        for slot, key in zip(sets, ("read", "destructive", "write")):
            words = verbs.get(key)
            if isinstance(words, list):
                slot.update(w.strip().lower() for w in words if isinstance(w, str) and w.strip())
    return sets[0], sets[1], sets[2]


def _shown(server: str, leaf: str) -> str:
    """The name a reason carries: `server: tool`, or just the tool when there is no server."""
    shown = leaf[:_MAX_SHOWN_NAME]
    return f"{server}: {shown}" if server else shown


def classify_mcp(tool: str, tool_input: Any = None,
                 extra: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """`(severity, reason)` for one MCP call.

    1. A leaf whose first word is a read verb never escalates (`get_delete_policy`).
    2. Otherwise a destructive word anywhere in the leaf is high, a write word is medium, and
       anything else stays low with the wording receipts have always used.
    3. An execute-class call - the leaf carries `sql`, `query`, `statement` or `execute`, or a
       `query`/`sql`/`statement` argument opens like a statement - is decided by the SQL
       instead: dropping, truncating, a write without a WHERE clause or a dropped column is
       high, any other data-changing statement is medium, read-only SQL keeps the low wording,
       and a call with no statement the guard can read is medium. SQL wins over the read-verb
       rule, but a destructive word in the tool's own name still keeps it high.

    `extra` is the risk policy; `extra["mcp_verbs"]` extends the three vocabularies.
    """
    parts = mcp_parts(tool) or ("", tool if isinstance(tool, str) else "")
    server, leaf = parts
    read, destructive, write = _verb_sets(extra)
    tokens = mcp_tokens(leaf)
    name = _shown(server, leaf)

    executable = any(t in _TOOL_SQL_TOKENS for t in tokens)
    sql = sql_argument(tool_input)
    stated = sql is not None and looks_like_sql(sql)
    from_arguments = not executable and isinstance(tool_input, dict) and stated \
        and any(k in tool_input for k in SQL_ARGUMENT_KEYS)
    if executable or from_arguments:
        kind = sql_statement_class(sql) if sql is not None else "unreadable"
        if kind in SQL_LABELS:
            severity = "high" if kind in SQL_HIGH_CLASSES else "medium"
            return severity, f"{SQL_LABELS[kind]} ({name})"
        if executable and not stated:
            return "medium", f"{SQL_LABELS['unreadable']} ({name})"
        if any(t in destructive for t in tokens):
            return "high", f"{LABEL_DESTRUCTIVE} ({name})"
        return "low", f"{LABEL_EXTERNAL} ({_shown('', leaf)})"

    if tokens and tokens[0] in read:
        return "low", f"{LABEL_EXTERNAL} ({_shown('', leaf)})"
    if any(t in destructive for t in tokens):
        return "high", f"{LABEL_DESTRUCTIVE} ({name})"
    if any(t in write for t in tokens):
        return "medium", f"{LABEL_STATE_CHANGING} ({name})"
    return "low", f"{LABEL_EXTERNAL} ({_shown('', leaf)})"
