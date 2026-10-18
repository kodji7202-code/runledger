"""Post RunLedger risk receipts as a sticky comment on a GitHub pull request.

    python -m runledger.github comment --sessions-dir .runledger/sessions --fail-on 60
    python -m runledger.github comment --session run.jsonl --dry-run

Exit codes: 0 ok; 1 the highest risk score is >= --fail-on; 2 bad arguments;
3 runtime error (no sessions, no pull request, token missing, GitHub API error).

Environment: GITHUB_REPOSITORY and GITHUB_EVENT_PATH give the default repository
and pull request. GITHUB_API_URL overrides the API base (GitHub Enterprise Server,
tests). GITHUB_STEP_SUMMARY and GITHUB_OUTPUT receive the Markdown and the outputs
risk_score and risk_level. The token is read from the variable named by
--token-env (default GITHUB_TOKEN); it is never printed.
"""
from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import __version__
from . import _http
from . import labels
from .adapters import label as agent_label
from .cli import build
from .guard_receipt import guard_counts, guard_warning_count
from .parser import Run
from .pricing import friendly_model, partial_note
from .quality import _test_runs
from .risk import Risk
from .safe import make_safe
from .summarize import file_changes

MARKER = "<!-- runledger-receipt -->"
GITHUB_COMMENT_LIMIT = 65_536  # GitHub rejects a longer comment body outright
MAX_COMMENT_CHARS = 60_000  # our own limit, safely below GITHUB_COMMENT_LIMIT
DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_PAGES = 100
HTTP_TIMEOUT = 30
MAX_LIST_ITEMS = 25  # risk reasons and files listed per run
ERROR_BODY_BYTES = 4096  # of an error body, only this much is read before the 300-char detail

EXIT_OK = 0
EXIT_THRESHOLD = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

_BADGES = {"Low": "🟢", "Medium": "🟡", "High": "🔴"}
_REPO_RE = re.compile(r"^([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)$")
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?\s*(?:,|$)')
_NOTICE = "> _Shortened to fit GitHub's comment size limit; some content is left out._"
_DETAILS_CLOSE = "\n</details>"
_FORK_MESSAGE = "resource not accessible by integration"
_ZWSP = "\u200b"
# Markdown punctuation that could build a link, an image, emphasis or a table cell.
_ESCAPE_RE = re.compile(r"([\\`*_~\[\]()!|])")
_BACKTICKS_RE = re.compile(r"`+")
_ISSUE_REF_RE = re.compile(r"#(?=\d)")
_LINE_START_RE = re.compile(r"^(?:#|>|-|\+|\d+\.)")
# GitHub links a bare URL on its own: the scheme and the `www.` prefix are the triggers,
# and escaping the surrounding punctuation does not stop them.
_BARE_URL_RE = re.compile(r"(?i)https?://|www\.")


@dataclass
class Scored:
    """One session and its risk assessment, as returned by cli.build().

    `guard_warnings` and `test_runs` are read from the session before safe.make_safe runs:
    masking blanks the step results and shortens the cwd the guard log is found through."""
    run: Run
    score: int
    level: str
    risks: List[Risk]
    guard_warnings: int = 0
    test_runs: Optional[List[Any]] = None


class GitHubError(RuntimeError):
    """A GitHub API call failed. The message never contains the token.

    `status` is the HTTP status when GitHub answered, `detail` the redacted error body:
    the caller needs both to tell "a fork cannot comment" from any other refusal."""

    def __init__(self, message: str, status: Optional[int] = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


def _fork_forbidden(exc: GitHubError) -> bool:
    """GitHub's answer when a `pull_request` token from a fork may not write."""
    return exc.status == 403 and _FORK_MESSAGE in (exc.detail or "").lower()


# ---------------------------------------------------------------- Markdown

def _no_autolink(match: "re.Match[str]") -> str:
    """A zero-width space inside the trigger: GitHub no longer links the URL, and the
    reader still sees the whole address."""
    token = match.group(0)
    cut = token.index(":") + 1 if ":" in token else len(token) - 1
    return token[:cut] + _ZWSP + token[cut:]


def _text(value: Any, limit: Optional[int] = None) -> str:
    """User-derived text made safe inside a comment: one line, no raw HTML, no live link,
    image or @mention, no Markdown block that swallows the rest of the comment and no
    broken table cell. HTML tags stay escaped (`<b>` never becomes a tag)."""
    s = " ".join(str(value).split())
    if limit is not None and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    s = html.escape(s, quote=False)
    s = _ESCAPE_RE.sub(r"\\\1", s)
    s = _ISSUE_REF_RE.sub(r"\\#", s)
    s = _LINE_START_RE.sub(lambda m: "\\" + m.group(0), s)
    s = _BARE_URL_RE.sub(_no_autolink, s)
    return s.replace("@", "@" + _ZWSP)


def _code(value: Any) -> str:
    """One code span. A backtick is kept: the fence grows past the longest run of
    backticks and is padded, instead of replacing the character."""
    s = " ".join(str(value).split())
    longest = max((len(m.group(0)) for m in _BACKTICKS_RE.finditer(s)), default=0)
    fence = "`" * (longest + 1)
    if fence == "`":
        return f"`{s}`"
    return f"{fence} {s} {fence}"


def _money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:.4f}" if value < 0.01 else f"${value:.3f}" if value < 1 else f"${value:.2f}"


def _badge(level: str) -> str:
    return _BADGES.get(level, "⚪")


def _title(run: Run) -> str:
    first = run.prompts[0] if run.prompts else ""
    return _text(first, 80) or f"Session {_text(run.session_id[:8])}"


def _models(run: Run) -> str:
    names: List[str] = []
    for model, _usage in sorted(run.models.items(), key=lambda kv: -kv[1].total):
        name = friendly_model(model)
        if name not in names:
            names.append(name)
    return _text(", ".join(names)) or "n/a"


def _agent_model(run: Run) -> str:
    return f"{_text(agent_label(run.agent))} · {_models(run)}"


def _cost_cell(run: Run) -> str:
    """The estimate when there is one; otherwise the figure the agent reported."""
    if run.cost is not None:
        note = partial_note(run)
        return f"≥ {_money(run.cost)} ({note})" if note else _money(run.cost)
    if run.reported_cost is not None:
        return f"{_money(run.reported_cost)} reported"
    return "n/a"


def _guard_line(item: Scored) -> Optional[str]:
    """The Guard's decisions for this run, when there are any. `warned` counts what the
    Guard flagged without blocking: monitor-mode interventions and configuration warnings."""
    run = item.run
    counts = guard_counts(run.guard)
    warnings = item.guard_warnings
    monitor = counts["monitor_blocked"] + counts["monitor_asked"]
    if not run.guard and not warnings:
        return None
    parts = [f'{counts["blocked"]} blocked']
    if counts["asked"]:
        parts.append(f'{counts["asked"]} asked ({counts["approved"]} approved, '
                     f'{counts["rejected"]} rejected)')
    warned = monitor + warnings
    detail = []
    if monitor:
        detail.append(f"{monitor} monitor mode")
    if warnings:
        detail.append(f"{warnings} log warning{'s' if warnings != 1 else ''}")
    parts.append(f"{warned} warned" + (f" ({', '.join(detail)})" if detail else ""))
    return "- **Guard:** " + " · ".join(parts)


def _tests_line(item: Scored) -> str:
    """One line per run saying whether tests ran and how the final run ended. The final
    invocation decides the verdict: an unreadable one is unknown, and any readable earlier
    run is named as earlier instead of being passed off as the last run."""
    runs = item.test_runs if item.test_runs is not None else _test_runs(item.run)
    if not runs:
        return "- **Tests:** no tests run (this session has no test command)."
    count = f"{len(runs)} test command(s) ran"
    last = runs[-1]
    if last.outcome is None:
        line = (f"- **Tests:** {count} · last run unreadable ({last.description}) · "
                "verdict: unknown.")
        earlier = [t for t in runs[:-1] if t.outcome is not None]
        if earlier:
            ended = "passed" if earlier[-1].outcome == "pass" else "failed"
            line += f" An earlier readable run {ended} ({earlier[-1].description})."
        return line
    verdict = "passed" if last.outcome == "pass" else "failed"
    return f"- **Tests:** {count} · last run {last.description} · verdict: {verdict}."


def _run_details(item: Scored) -> List[str]:
    run = item.run
    changes = list(file_changes(run).values())
    lines = [
        "<details>",
        f"<summary>Details for <code>{_text(run.session_id[:8])}</code>: "
        f"{len(item.risks)} risk reason(s), {len(changes)} file(s) changed</summary>",
        "",
    ]
    guard = _guard_line(item)
    if guard:
        lines.append(guard)
    lines += [
        _tests_line(item),
        "",
        "**Risk reasons**",
    ]
    if item.risks:
        for r in labels.show_risks(item.risks[:MAX_LIST_ITEMS], labels.step_label_map(item.run)):
            lines.append(f"- **{r.severity.upper()}** · step {r.step}: {_text(r.reason)}")
        if len(item.risks) > MAX_LIST_ITEMS:
            lines.append(f"- … and {len(item.risks) - MAX_LIST_ITEMS} more")
    else:
        lines.append("- None. No risky actions detected by the rules.")
    lines += ["", "**Files changed**"]
    if changes:
        for c in changes[:MAX_LIST_ITEMS]:
            tag = " (deleted)" if c.deleted else " (new)" if c.created else ""
            lines.append(f"- {_code(c.path)}{tag} +{c.added} −{c.removed}")
        if len(changes) > MAX_LIST_ITEMS:
            lines.append(f"- … and {len(changes) - MAX_LIST_ITEMS} more")
    else:
        lines.append("- None.")
    lines += ["", "</details>", ""]
    return lines


def _fit(body: str, footer: str, limit: int) -> str:
    """body + footer within `limit` characters. When it does not fit, cut at a
    line boundary, close an open <details> block and add a notice."""
    if len(body) + 1 + len(footer) + 1 <= limit:
        return body + "\n" + footer + "\n"
    # room for: closing tag, blank line, notice, blank line, footer, final newline
    reserve = len(_DETAILS_CLOSE) + 2 + len(_NOTICE) + 2 + len(footer) + 1
    cut = body[: max(0, limit - reserve)]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else ""
    close = _DETAILS_CLOSE if cut.count("<details>") > cut.count("</details>") else ""
    return f"{cut}{close}\n\n{_NOTICE}\n\n{footer}\n"


def _footer(items: List[Scored]) -> str:
    """The statement every comment ends with. Safe mode is on for everything the CLI
    posts, so the comment says what it masked instead of leaving that to the reader."""
    if all(item.run.safe for item in items):
        safe = "Safe mode: paths are shortened and e-mails are masked."
    else:
        safe = "Safe mode: off. Paths and e-mails are shown as captured."
    return ("---\n"
            f"{safe}\n"
            f"Generated by [RunLedger](https://runledger.site) {__version__}. "
            "Titles are each session's first request. Risk scores are rule-based; "
            "costs are estimates at Claude API list prices.")


def render_comment(items: List[Scored], limit: int = MAX_COMMENT_CHARS, skipped: int = 0) -> str:
    """The sticky PR comment: header with the worst risk, one table row per run, and a
    collapsible block per run with its Guard decisions, test verdict, risk reasons and
    files changed. `skipped` sessions could not be read and are named in the comment."""
    if not items:
        raise ValueError("no sessions to render")
    top = max(items, key=lambda s: s.score)
    steps = sum(len(s.run.steps) for s in items)
    files = sum(len(file_changes(s.run)) for s in items)
    costs = [s.run.cost for s in items if s.run.cost is not None]
    total = _money(sum(costs)) if costs else "n/a"
    if costs and len(costs) < len(items):
        total += " (partial)"
    runs = f"{len(items)} run{'s' if len(items) != 1 else ''}"

    lines = [
        MARKER,
        f"## RunLedger receipt · {_badge(top.level)} {top.level} risk · {top.score}/100",
        "",
        f"**{runs}** · {steps} steps · {files} files changed · estimated cost {total}",
        "",
    ]
    if skipped:
        lines += [f"> {skipped} session(s) could not be read and were skipped; "
                  "see the job log for the file names.", ""]
    lines += [
        "| Run | Agent · Model | Steps | Cost | Risk |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for s in items:
        lines.append(
            f"| {_title(s.run)} | {_agent_model(s.run)} | {len(s.run.steps)} | "
            f"{_cost_cell(s.run)} | {_badge(s.level)} {s.score}/100 {s.level} |")
    lines.append("")
    for s in items:
        lines += _run_details(s)
    body = "\n".join(lines)
    return _fit(body, _footer(items), limit)


# ---------------------------------------------------------------- GitHub API

def _next_link(link: str) -> Optional[str]:
    m = _LINK_NEXT_RE.search(link or "")
    return m.group(1) if m else None


class GitHubClient:
    def __init__(self, api_url: str, token: str, repo: str, pr: int, timeout: int = HTTP_TIMEOUT):
        self.api = api_url.rstrip("/")
        self.repo = repo
        self.pr = pr
        self.timeout = timeout
        self._token = token
        self._login: Optional[str] = None      # GET /user, asked once; None when it is refused
        self.notice: Optional[str] = None      # the caller prints this when it is set

    def _redact(self, text: Any) -> str:
        s = str(text)
        return s.replace(self._token, "***") if self._token else s

    def _send(self, method: str, url: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[Any, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": f"runledger/{__version__}",
            "X-GitHub-Api-Version": API_VERSION,
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                link = resp.headers.get("Link", "") or ""
        except urllib.error.HTTPError as exc:
            detail = self._redact(_http.read_error(exc, ERROR_BODY_BYTES)[:300])  # closes it too
            raise GitHubError(f"GitHub returned HTTP {exc.code} for {method} {url}: "
                              f"{detail}", status=exc.code, detail=detail) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise GitHubError(f"cannot reach GitHub at {self.api}: {self._redact(exc)}") from None
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            raise GitHubError(f"unexpected non-JSON response for {method} {url}") from None
        return body, link

    def _comment_pages(self) -> Iterator[List[Any]]:
        """Pages of issue comments on the pull request. Follows the Link header
        (rel="next"); without one, requests the next page while pages are full."""
        base = f"{self.api}/repos/{self.repo}/issues/{self.pr}/comments?per_page={PER_PAGE}"
        url: Optional[str] = base
        page_no = 1
        for _ in range(MAX_PAGES):
            if url is None:
                return
            items, link = self._send("GET", url)
            if not isinstance(items, list):
                raise GitHubError("unexpected response when listing pull request comments")
            yield items
            nxt = _next_link(link)
            if nxt is not None:
                if not nxt.startswith(self.api + "/"):
                    raise GitHubError("refusing to follow a pagination link outside the API host")
                url = nxt
            elif len(items) == PER_PAGE:
                page_no += 1
                url = f"{base}&page={page_no}"
            else:
                return
        raise GitHubError(f"more than {MAX_PAGES} pages of comments; giving up")

    def viewer_login(self) -> Optional[str]:
        """The login the token acts as, from `GET /user`. A `GITHUB_TOKEN` cannot call
        `/user`, so the answer is None and only bot comments may be adopted."""
        if self._login is None:
            try:
                data, _ = self._send("GET", f"{self.api}/user")
            except GitHubError:
                return None
            login = data.get("login") if isinstance(data, dict) else None
            if isinstance(login, str) and login:
                self._login = login
        return self._login

    def find_marked_comment(self) -> Optional[Dict[str, Any]]:
        """The sticky comment with our marker, restricted to comments this token may rewrite:
        its own (matched by login) or a bot's. A comment somebody else planted is ignored."""
        login = self.viewer_login()
        for page in self._comment_pages():
            for comment in page:
                if not isinstance(comment, dict) or MARKER not in str(comment.get("body") or ""):
                    continue
                user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
                if login is not None:
                    if user.get("login") == login:
                        return comment
                elif user.get("type") == "Bot":
                    return comment
        return None

    def upsert_comment(self, body: str) -> Tuple[str, Dict[str, Any]]:
        """PATCH the existing marked comment, or POST a new one. A refused PATCH (403 or
        404, e.g. a comment written by another app) falls back to a fresh comment.
        Returns ("updated" | "created", the comment JSON); `self.notice` explains a fallback."""
        found = self.find_marked_comment()
        if found is not None:
            comment_id = found.get("id")
            if not isinstance(comment_id, int):
                raise GitHubError("marked comment has no id")
            try:
                data, _ = self._send("PATCH",
                                     f"{self.api}/repos/{self.repo}/issues/comments/{comment_id}",
                                     {"body": body})
                return "updated", data or {}
            except GitHubError as exc:
                if _fork_forbidden(exc) or exc.status not in (403, 404):
                    raise
                self.notice = (f"cannot update comment {comment_id} (HTTP {exc.status}); "
                               "creating a new comment")
        data, _ = self._send("POST", f"{self.api}/repos/{self.repo}/issues/{self.pr}/comments",
                             {"body": body})
        return "created", data or {}


# ---------------------------------------------------------------- CLI

def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _warn_file(path: Path, message: str) -> None:
    """A GitHub annotation: the workflow log links it to the file it names."""
    print(f"::warning file={path}::{message}", file=sys.stderr)


def _notice(message: str) -> None:
    print(f"notice: {message}", file=sys.stderr)


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _emit(markdown: str) -> None:
    """Write Markdown to stdout. Falls back to raw UTF-8 bytes when the console
    or a redirect uses a code page that cannot show the badge emoji."""
    out = sys.stdout
    try:
        out.write(markdown)
    except UnicodeEncodeError:
        out.flush()
        out.buffer.write(markdown.encode("utf-8"))
        out.buffer.flush()
    out.flush()


def _score_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}")
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return value


def _pr_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a pull request number, got {text!r}")
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def _session_paths(sessions: List[str], sessions_dir: Optional[str]) -> List[Path]:
    candidates = [Path(s) for s in sessions]
    if sessions_dir:
        folder = Path(sessions_dir)
        candidates += sorted(folder.glob("*.jsonl")) + sorted(folder.glob("*.runledger.json"))
    unique: List[Path] = []
    seen = set()
    for path in candidates:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _event() -> Dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        return {}
    return event if isinstance(event, dict) else {}


def _pr_number(event: Dict[str, Any]) -> Optional[int]:
    pull = event.get("pull_request")
    for candidate in ((pull or {}).get("number"), event.get("number")):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            return candidate
    return None


def _event_fork(event: Dict[str, Any]) -> bool:
    """True for a `pull_request` event whose head repository is a fork. Such a run gets a
    read-only token, so no comment can be posted and no request is worth trying."""
    if os.environ.get("GITHUB_EVENT_NAME", "") == "pull_request_target":
        return False  # that event runs with the base repository's write token
    pull = event.get("pull_request")
    head = pull.get("head") if isinstance(pull, dict) else None
    repo = head.get("repo") if isinstance(head, dict) else None
    return isinstance(repo, dict) and repo.get("fork") is True


def _write_step_outputs(markdown: str, top: Scored) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(markdown)
        except OSError as exc:
            _warn(f"cannot write GITHUB_STEP_SUMMARY: {exc}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        try:
            with open(output, "a", encoding="utf-8") as fh:
                fh.write(f"risk_score={top.score}\nrisk_level={top.level}\n")
        except OSError as exc:
            _warn(f"cannot write GITHUB_OUTPUT: {exc}")


def _fork_notice() -> None:
    """A fork pull request gets a read-only token. The receipt still reaches the job page."""
    _notice("cannot post a comment from a fork pull request (read-only token); "
            "the receipt was written to the step summary instead")
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        _warn("GITHUB_STEP_SUMMARY is not set, so the receipt is only in this log")


def _post(args: argparse.Namespace, markdown: str, top: Scored) -> bool:
    """Post or update the sticky comment. A fork pull request is not an error: GitHub
    refuses the write, so the caller keeps the exit code of the risk threshold alone."""
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        _error("no repository: pass --repo owner/name or set GITHUB_REPOSITORY")
        return False
    match = _REPO_RE.match(repo)
    if not match or match.group(2) in (".", ".."):
        _error("repository must be written as owner/name")
        return False
    event = _event()
    pr = args.pr or _pr_number(event)
    if not pr:
        _error("no pull request number: pass --pr N or run on a pull_request event")
        return False
    token = os.environ.get(args.token_env, "")
    if not token:
        _error(f"no token: set {args.token_env} (the token needs pull-requests: write)")
        return False
    if _event_fork(event):
        _fork_notice()
        return True
    api = os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL
    client = GitHubClient(api, token, repo, pr)
    try:
        action, comment = client.upsert_comment(markdown)
    except GitHubError as exc:
        if _fork_forbidden(exc):
            _fork_notice()
            return True
        _error(str(exc))
        return False
    if client.notice:
        _notice(client.notice)
    url = comment.get("html_url")
    link = f" {url}" if isinstance(url, str) and url else ""
    print(f"RunLedger receipt {action} on {repo}#{pr}: risk {top.score}/100 ({top.level}).{link}")
    return True


def _write_step_summary_line(line: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        _warn(f"cannot write GITHUB_STEP_SUMMARY: {exc}")


def _no_sessions(args: argparse.Namespace) -> int:
    """An empty session folder is a misconfiguration: a failure unless --allow-empty."""
    where = f" in {args.sessions_dir}" if args.sessions_dir else ""
    if not args.allow_empty:
        _error(f"no session files were found{where}: looked for *.jsonl and "
               "*.runledger.json; pass --allow-empty to accept an empty folder")
        return EXIT_ERROR
    note = f"no session files were found{where}; the receipt was skipped (--allow-empty)"
    _notice(note)
    _write_step_summary_line(f"RunLedger: {note}.")
    return EXIT_OK


def cmd_comment(args: argparse.Namespace) -> int:
    if not args.session and not args.sessions_dir:
        _error("give at least one --session PATH or a --sessions-dir DIR")
        return EXIT_USAGE

    paths = _session_paths(args.session or [], args.sessions_dir)
    if not paths:
        return _no_sessions(args)

    items: List[Scored] = []
    failed: List[Tuple[Path, Exception]] = []
    for path in paths:
        try:
            run, score, level, risks, _note = build(str(path))
            # Read what masking takes away: the guard log lives at the real cwd, and
            # safe.make_safe blanks every step result the test verdict is read from.
            guard_warnings = guard_warning_count(run)
            test_runs = _test_runs(run)
            make_safe(run, risks)  # a PR comment is public to everyone who can read the PR
        except (OSError, ValueError, RecursionError) as exc:
            failed.append((path, exc))   # one broken file must not hide the other sessions
            continue
        items.append(Scored(run, score, level, risks, guard_warnings=guard_warnings,
                            test_runs=test_runs))
    for path, exc in failed:
        _warn_file(path, f"RunLedger skipped this session: {exc}")
    if not items:
        _error(f"no session could be read ({len(failed)} file(s) failed); nothing was posted")
        return EXIT_ERROR

    markdown = render_comment(items, skipped=len(failed))
    top = max(items, key=lambda s: s.score)
    _write_step_outputs(markdown, top)

    ok = True
    if args.dry_run:
        _emit(markdown)
        print("Dry run: nothing was posted.", file=sys.stderr)
    else:
        ok = _post(args, markdown, top)

    met = args.fail_on is not None and top.score >= args.fail_on
    if met:
        print(f"risk {top.score}/100 ({top.level}) is at or above --fail-on {args.fail_on}",
              file=sys.stderr)
    if not ok:
        return EXIT_ERROR      # a comment that never went up is an error, whatever the score
    return EXIT_THRESHOLD if met else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m runledger.github",
                                description="Post RunLedger risk receipts to GitHub pull requests.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("comment", help="post or update the receipt comment on a pull request")
    c.add_argument("--session", action="append", metavar="PATH",
                   help="session file: .jsonl (Claude Code, Codex, Droid) or .runledger.json "
                        "(repeatable)")
    c.add_argument("--sessions-dir", metavar="DIR",
                   help="score every .jsonl and .runledger.json session file in this folder "
                        "(a Droid session needs its .settings.json beside it for usage)")
    c.add_argument("--allow-empty", action="store_true",
                   help="accept a session folder with no session files: print a notice on "
                        "stderr and exit 0 instead of failing")
    c.add_argument("--fail-on", type=_score_arg, metavar="SCORE",
                   help="exit 1 if the highest risk score is >= SCORE (0-100)")
    c.add_argument("--repo", metavar="owner/name",
                   help="repository (default: $GITHUB_REPOSITORY)")
    c.add_argument("--pr", type=_pr_arg, metavar="N",
                   help="pull request number (default: from $GITHUB_EVENT_PATH)")
    c.add_argument("--token-env", default="GITHUB_TOKEN", metavar="NAME",
                   help="environment variable holding the token (default GITHUB_TOKEN)")
    c.add_argument("--dry-run", action="store_true",
                   help="print the Markdown only; no network access")
    c.set_defaults(func=cmd_comment)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
