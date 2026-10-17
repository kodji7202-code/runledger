"""Shared secret detection, redaction, secret-path matching, and safe user regex validation.

Leaf module: imports nothing from `runledger` (and never imports stdlib `secrets`, which
this file's name would shadow if run directly). Used by `risk.py`, `guard.py`, and `llm.py`.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Iterable, List, Optional, Pattern, Sequence, Tuple, Union

try:
    import re._parser as _re_parser  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Python < 3.11 fallback
    import sre_parse as _re_parser  # type: ignore[no-redef]

MAX_USER_REGEX_LEN = 200
MAX_EXTRA_SECRET_PATHS = 20
MAX_PATH_MATCH_LEN = 1024
_MAX_BOUNDED_REPEAT = 32
_MAX_UNBOUNDED_REPEATS = 2

SAFE_PREFIXES: Tuple[str, ...] = (
    "$",
    "http://",
    "https://",
    "example",
    "sample",
    "dummy",
    "placeholder",
    "your_",
    "your-",
    "<your",
    "<insert",
    "changeme",
    "todo",
    "xxx",
    "***",
    "[redacted]",
    "os.environ",
    "process.env",
    "env[",
    "env.get",
    "getenv",
)

_SAFE_EXACT = frozenset({
    "true", "false", "none", "null", "undefined", "yes", "no", "on", "off",
    "changeme", "redacted", "[redacted]", "password", "secret", "token",
})

_ALL_CAPS_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_AWS_KEY_EXACT_RE = re.compile(r"^(?:AKIA|ASIA)[0-9A-Z]{16}$")


def _is_benign_value(val: str) -> bool:
    """True when `val` is a placeholder, env reference, boolean/null literal, pure number, or path."""
    v = val.strip().strip("\"'")
    if not v or v.isdigit() or v.startswith(("$", "{", "<", "%", "[")) or v.endswith((">", "...")):
        return True
    low = v.lower()
    if low in _SAFE_EXACT or low.startswith(SAFE_PREFIXES):
        return True
    if v.startswith(("/", "./", "../", "~/")) or (len(v) > 2 and v[1:3] in (":\\", ":/")):
        return True
    if _ALL_CAPS_ENV_RE.match(v) and not _AWS_KEY_EXACT_RE.match(v):
        return True
    return False


# ---------------------------------------------------------------- secret content rules

# Ordered (compiled_regex, human_readable_kind) for token/key shapes that are self-identifying.
_TOKEN_RULES: Tuple[Tuple[Pattern[str], str], ...] = (
    (re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    (re.compile(r"(?<![A-Za-z0-9])sk-(?!ant-)(?:proj-|live-|test-|svcacct-)?[A-Za-z0-9_-]{20,}"), "OpenAI API key"),
    (re.compile(r"(?<![A-Za-z0-9])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), "Stripe API key"),
    (re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"), "AWS access key"),
    (re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"), "GitHub token"),
    (re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{20,}"), "GitLab token"),
    (re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"), "Google API key"),
    (re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{36}"), "npm token"),
    (re.compile(r"(?<![A-Za-z0-9])pypi-[A-Za-z0-9_-]{32,}"), "PyPI token"),
    (re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"), "HuggingFace token"),
    (re.compile(r"(?<![A-Za-z0-9])rl_[A-Za-z0-9_-]{30,}"), "RunLedger API key"),
    (re.compile(r"(?i)(?<![A-Za-z0-9])AccountKey\s*=\s*[A-Za-z0-9+/=]{40,}"), "Azure storage key"),
    (re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT token"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "private key"),
)

_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_PEM_HEADER_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

_URL_CRED_RE = re.compile(
    r"(\b[A-Za-z][A-Za-z0-9+.-]{0,20}://[^/\s:@\"']{1,128}:)(?!\$\{?)([^@\s/\"']{4,256})(@)"
)
_AUTH_HEADER_RE = re.compile(
    r"(\bAuthorization\s*:\s*(?:Bearer|Basic|Token)\s+)([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|client_secret|password|secret)=)"
    r"([^&#\s\"'<>]{6,})",
    re.IGNORECASE,
)
_FLAG_SECRET_RE = re.compile(
    r"(--(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret|client-secret)"
    r"(?:=|\s+))([\"']?)([^\s\"'=<>]{6,})\2",
    re.IGNORECASE,
)
_SECRET_KEYWORD = (
    r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"secret[_-]?access[_-]?key|private[_-]?key|secret[_-]?key|secret[_-]?token|access[_-]?key|"
    r"db[_-]?password|passw(?:or)?d|pwd|secret|token|credentials?)"
)
_GENERIC_QUOTED_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + _SECRET_KEYWORD + r"(?:[_-][A-Za-z0-9]{1,16})?)"
    r"([\"']?\s*[:=]\s*[\"'])([^\"'\s]{12,})([\"'])",
    re.IGNORECASE,
)
_GENERIC_UNQUOTED_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + _SECRET_KEYWORD + r"(?:[_-][A-Za-z0-9]{1,16})?)"
    r"(\s*=\s*)([^\s\"';,&|<>(){}\[\]]{12,})",
    re.IGNORECASE,
)
# Redaction covers a name that merely contains the keyword (`MYTOKEN=`, `TOKEN2=`,
# `MY_ACCESS_TOKEN2=`), as v0.6.0 did. The prefix and suffix are bounded so a long blob
# without a keyword cannot cause backtracking; detection (the _GENERIC_* patterns) stays
# word-anchored and keeps its longer value threshold.
_REDACT_NAME = r"[A-Za-z0-9_.-]{0,40}" + _SECRET_KEYWORD + r"[A-Za-z0-9_.-]{0,40}"
_REDACT_QUOTED_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + _REDACT_NAME + r")"
    r"([\"']?\s*[:=]\s*[\"'])([^\"'\s]{8,})([\"'])",
    re.IGNORECASE,
)
_REDACT_UNQUOTED_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + _REDACT_NAME + r")"
    r"(\s*=\s*)([^\s\"';,&|<>(){}\[\]]{6,})",
    re.IGNORECASE,
)

SECRET_CONTENT_PATTERNS: Tuple[Tuple[Pattern[str], str], ...] = _TOKEN_RULES + (
    (_URL_CRED_RE, "URL password"),
    (_AUTH_HEADER_RE, "authorization token"),
    (_QUERY_SECRET_RE, "access token"),
    (_FLAG_SECRET_RE, "CLI secret"),
    (_GENERIC_QUOTED_RE, "hardcoded secret"),
    (_GENERIC_UNQUOTED_RE, "hardcoded secret"),
)


def _has_token_rule_match(val: str) -> bool:
    return any(rx.search(val) for rx, _ in _TOKEN_RULES)


def _generic_kind_label(raw_name: str) -> str:
    low = re.sub(r"[_-]+", " ", raw_name.lower()).strip()
    for canonical in ("api key", "access token", "auth token", "client secret", "private key",
                      "secret key", "secret token", "db password", "password", "secret", "token"):
        if low == canonical or low.endswith(" " + canonical):
            return canonical
    if low in ("passwd", "pwd") or low.endswith((" passwd", " pwd")):
        return "password"
    return low


def find_secrets(text: Any) -> List[str]:
    """Return deduplicated secret kind labels detected in `text`."""
    if not isinstance(text, str) or not text:
        return []
    kinds: List[str] = [kind for rx, kind in _TOKEN_RULES if rx.search(text)]
    for m in _URL_CRED_RE.finditer(text):
        if not _is_benign_value(m.group(2)):
            kinds.append("URL password")
    for m in _AUTH_HEADER_RE.finditer(text):
        val = m.group(2)
        if not _is_benign_value(val) and not _has_token_rule_match(val):
            kinds.append("authorization token")
    for m in _QUERY_SECRET_RE.finditer(text):
        val = m.group(2)
        if not _is_benign_value(val) and not _has_token_rule_match(val):
            kinds.append("access token")
    for m in _FLAG_SECRET_RE.finditer(text):
        val = m.group(3)
        if not _is_benign_value(val) and not _has_token_rule_match(val):
            kinds.append("CLI secret")
    for m in _GENERIC_QUOTED_RE.finditer(text):
        val = m.group(3)
        if not _is_benign_value(val) and not _has_token_rule_match(val):
            kinds.append(_generic_kind_label(m.group(1)))
    for m in _GENERIC_UNQUOTED_RE.finditer(text):
        val = m.group(3)
        if not _is_benign_value(val) and not _has_token_rule_match(val):
            kinds.append(_generic_kind_label(m.group(1)))
    return list(dict.fromkeys(kinds))


def _mask_if_secret(value: str) -> str:
    return value if _is_benign_value(value) else "[REDACTED]"


def _looks_like_unquoted_secret(value: str) -> bool:
    """An unquoted value worth masking: not benign, and either contains a digit
    or is at least 12 characters long (so `token=refresh` stays readable)."""
    if _is_benign_value(value):
        return False
    return any(c.isdigit() for c in value) or len(value) >= 12


def _mask_unquoted_if_secret(value: str) -> str:
    return "[REDACTED]" if _looks_like_unquoted_secret(value) else value


def redact_text(text: Any) -> str:
    """Replace secret values in `text` with `[REDACTED]`, keeping surrounding structure."""
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    s = _PEM_BLOCK_RE.sub("[REDACTED PRIVATE KEY — [REDACTED]]", text)
    s = _PEM_HEADER_RE.sub("[REDACTED]", s)
    for rx, _ in _TOKEN_RULES[:-1]:  # all token rules except _PEM_HEADER_RE (already handled)
        s = rx.sub("[REDACTED]", s)
    s = _URL_CRED_RE.sub(lambda m: m.group(1) + _mask_if_secret(m.group(2)) + m.group(3), s)
    s = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + _mask_if_secret(m.group(2)), s)
    s = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + _mask_if_secret(m.group(2)), s)
    s = _FLAG_SECRET_RE.sub(
        lambda m: m.group(1) + m.group(2) + _mask_if_secret(m.group(3)) + m.group(2), s
    )
    s = _REDACT_QUOTED_RE.sub(
        lambda m: m.group(1) + m.group(2) + _mask_if_secret(m.group(3)) + m.group(4), s
    )
    s = _REDACT_UNQUOTED_RE.sub(
        lambda m: m.group(1) + m.group(2) + _mask_unquoted_if_secret(m.group(3)), s
    )
    return s


# ---------------------------------------------------------------- secret path rules & safe regex validator

_PUBLIC_OR_TEMPLATE_PATH_RE = re.compile(
    r"(?:"
    r"\.pub$"
    r"|(?:^|/)\.env\.(?:example|sample|template|dist|schema|defaults|test|ci)$"
    r"|\.tfvars\.(?:example|sample|template)$"
    r")",
    re.IGNORECASE,
)

SECRET_PATH_PATTERNS: Tuple[Pattern[str], ...] = (
    re.compile(r"(?:^|/)\.env(?:\.[A-Za-z0-9_-]+)?$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.envrc$", re.IGNORECASE),
    re.compile(r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)(?:_[A-Za-z0-9_-]+)?$", re.IGNORECASE),
    re.compile(r"\.(?:pem|key|p8|p12|pfx|jks|keystore|kdbx|keychain)$", re.IGNORECASE),
    re.compile(r"\.tfstate(?:\.backup)?$", re.IGNORECASE),
    re.compile(r"\.tfvars$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.(?:npmrc|pypirc|netrc|pgpass|htpasswd|vault-token|dockercfg|git-credentials)$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.git/config$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.aws/(?:credentials|config)$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.ssh/", re.IGNORECASE),
    re.compile(r"(?:^|/)\.kube/config$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.docker/config\.json$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.config/gh/hosts\.yml$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.config/gcloud/(?:credentials\.db|application_default_credentials\.json|legacy_credentials(?:/|$))", re.IGNORECASE),
    re.compile(r"(?:^|/)\.azure/(?:accessTokens|azureProfile)\.json$", re.IGNORECASE),
    re.compile(
        r"(?:^|/)(?:credentials(?:\.json)?|client_secret[^/]*\.json|service[-_]account[^/]*\.json|"
        r"secrets?\.(?:ya?ml|json|toml)|master\.key)$",
        re.IGNORECASE,
    ),
)


def _walk_regex_ast(
    parsed: Any,
    in_repeat: bool = False,
    in_unbounded: bool = False,
) -> Tuple[bool, int]:
    """Inspect a parsed `re._parser` / `sre_parse` AST for catastrophic backtracking shapes.
    Returns `(is_safe, unbounded_repeat_count)`."""
    unbounded_count = 0
    for op, av in parsed:
        op_name = getattr(op, "name", str(op)).upper()
        if op_name in ("GROUPREF", "GROUPREF_EXISTS"):
            return False, unbounded_count
        if op_name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            min_rep, max_rep, subpattern = av
            max_is_inf = getattr(max_rep, "name", "").upper() == "MAXREPEAT" or int(max_rep) > 65534
            is_unbounded = max_is_inf or int(max_rep) > _MAX_BOUNDED_REPEAT
            is_repeat = is_unbounded or int(max_rep) > 1
            if in_unbounded and is_repeat:
                return False, unbounded_count
            if in_repeat and is_unbounded:
                return False, unbounded_count
            if is_unbounded:
                unbounded_count += 1
            ok, sub_unbounded = _walk_regex_ast(
                subpattern,
                in_repeat=in_repeat or is_repeat,
                in_unbounded=in_unbounded or is_unbounded,
            )
            if not ok:
                return False, unbounded_count
            unbounded_count += sub_unbounded
        elif op_name == "BRANCH":
            _, branches = av
            if in_repeat and len(branches) > 1:
                return False, unbounded_count
            for branch in branches:
                ok, sub_unbounded = _walk_regex_ast(branch, in_repeat=in_repeat, in_unbounded=in_unbounded)
                if not ok:
                    return False, unbounded_count
                unbounded_count += sub_unbounded
        elif op_name == "SUBPATTERN":
            subpattern = av[-1]
            ok, sub_unbounded = _walk_regex_ast(subpattern, in_repeat=in_repeat, in_unbounded=in_unbounded)
            if not ok:
                return False, unbounded_count
            unbounded_count += sub_unbounded
        elif op_name == "ATOMIC_GROUP":
            ok, sub_unbounded = _walk_regex_ast(av, in_repeat=in_repeat, in_unbounded=in_unbounded)
            if not ok:
                return False, unbounded_count
            unbounded_count += sub_unbounded
        elif op_name in ("ASSERT", "ASSERT_NOT"):
            _, subpattern = av
            ok, sub_unbounded = _walk_regex_ast(subpattern, in_repeat=in_repeat, in_unbounded=in_unbounded)
            if not ok:
                return False, unbounded_count
            unbounded_count += sub_unbounded
        if unbounded_count > _MAX_UNBOUNDED_REPEATS:
            return False, unbounded_count
    return True, unbounded_count


def safe_user_regex(pattern: Any) -> Optional[Pattern[str]]:
    """Compile a user-supplied regex (or `glob:...` pattern) only if it is syntactically valid,
    at most 200 chars, and free of ReDoS-prone nested repeats/alternations/backreferences."""
    if not isinstance(pattern, str):
        return None
    raw = pattern.strip()
    if not raw or len(raw) > MAX_USER_REGEX_LEN:
        return None
    if raw.startswith("glob:"):
        glob_expr = raw[5:].strip()
        if not glob_expr:
            return None
        try:
            return re.compile(fnmatch.translate(glob_expr), re.I)
        except re.error:
            return None
    try:
        parsed = _re_parser.parse(raw)
        ok, unbounded = _walk_regex_ast(parsed)
        if not ok or unbounded > _MAX_UNBOUNDED_REPEATS:
            return None
        return re.compile(raw, re.I)
    except Exception:
        return None


def compile_extra_secret_paths(
    patterns: Any,
) -> Tuple[List[Pattern[str]], List[str]]:
    """Compile up to `MAX_EXTRA_SECRET_PATHS` safe user regexes.
    Returns `(compiled_patterns, rejected_raw_patterns)`."""
    compiled: List[Pattern[str]] = []
    rejected: List[str] = []
    if not isinstance(patterns, list):
        return compiled, rejected
    for item in patterns:
        if not isinstance(item, str) or not item.strip():
            continue
        if len(compiled) >= MAX_EXTRA_SECRET_PATHS:
            rejected.append(item)
            continue
        rx = safe_user_regex(item)
        if rx is None:
            rejected.append(item)
        else:
            compiled.append(rx)
    return compiled, rejected


def is_secret_path(
    path: Any,
    extra: Optional[Iterable[Union[str, Pattern[str]]]] = None,
) -> bool:
    """True when `path` matches a built-in secret file pattern or a safe `extra` pattern.
    Public keys (`*.pub`) and `.env.example`/template files return False."""
    if not isinstance(path, str) or not path:
        return False
    norm = path.replace("\\", "/")
    if _PUBLIC_OR_TEMPLATE_PATH_RE.search(norm):
        return False
    if any(rx.search(norm) for rx in SECRET_PATH_PATTERNS):
        return True
    if not extra:
        return False
    truncated = norm[:MAX_PATH_MATCH_LEN]
    count = 0
    for item in extra:
        if count >= MAX_EXTRA_SECRET_PATHS:
            break
        rx = item if isinstance(item, Pattern) else safe_user_regex(item)
        if rx is None:
            continue
        count += 1
        if rx.search(truncated):
            return True
    return False
