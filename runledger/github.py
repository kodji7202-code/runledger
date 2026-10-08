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
from .adapters import label as agent_label
from .cli import build
from .parser import Run
from .pricing import friendly_model
from .risk import Risk
from .summarize import file_changes

MARKER = "<!-- runledger-receipt -->"
MAX_COMMENT_CHARS = 60_000  # GitHub rejects comments above 65,536 characters
DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_PAGES = 100
HTTP_TIMEOUT = 30
MAX_LIST_ITEMS = 25  # risk reasons and files listed per run

EXIT_OK = 0
EXIT_THRESHOLD = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

_BADGES = {"Low": "🟢", "Medium": "🟡", "High": "🔴"}
_REPO_RE = re.compile(r"^([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)$")
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?\s*(?:,|$)')
_NOTICE = "> _Shortened to fit GitHub's comment size limit; some content is left out._"
_DETAILS_CLOSE = "\n</details>"


@dataclass
class Scored:
    """One session and its risk assessment, as returned by cli.build()."""
    run: Run
    score: int
    level: str
    risks: List[Risk]


class GitHubError(RuntimeError):
    """A GitHub API call failed. The message never contains the token."""


# ---------------------------------------------------------------- Markdown

def _text(value: Any, limit: Optional[int] = None) -> str:
    """User-derived text made safe inside a comment: one line, no raw HTML,
    no accidental @mentions and no broken table cells."""
    s = " ".join(str(value).split())
    if limit is not None and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return html.escape(s, quote=False).replace("@", "@​").replace("|", "\\|")


def _code(value: Any) -> str:
    s = " ".join(str(value).split())
    return "`" + s.replace("`", "'") + "`"


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
        return _money(run.cost)
    if run.reported_cost is not None:
        return f"{_money(run.reported_cost)} reported"
    return "n/a"


def _run_details(item: Scored) -> List[str]:
    run = item.run
    changes = list(file_changes(run).values())
    lines = [
        "<details>",
        f"<summary>Details for <code>{_text(run.session_id[:8])}</code>: "
        f"{len(item.risks)} risk reason(s), {len(changes)} file(s) changed</summary>",
        "",
        "**Risk reasons**",
    ]
    if item.risks:
        for r in item.risks[:MAX_LIST_ITEMS]:
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


def render_comment(items: List[Scored], limit: int = MAX_COMMENT_CHARS) -> str:
    """The sticky PR comment: header with the worst risk, one table row per run,
    and a collapsible block per run with its risk reasons and files changed."""
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
    footer = ("---\n"
              f"Generated by [RunLedger](https://runledger.site) {__version__}. "
              "Titles are each session's first request. Risk scores are rule-based; "
              "costs are estimates at Claude API list prices.")
    return _fit(body, footer, limit)


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
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise GitHubError(f"GitHub returned HTTP {exc.code} for {method} {url}: "
                              f"{self._redact(detail)}") from None
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

    def find_marked_comment(self) -> Optional[Dict[str, Any]]:
        for page in self._comment_pages():
            for comment in page:
                if isinstance(comment, dict) and MARKER in str(comment.get("body") or ""):
                    return comment
        return None

    def upsert_comment(self, body: str) -> Tuple[str, Dict[str, Any]]:
        """PATCH the existing marked comment, or POST a new one.
        Returns ("updated" | "created", the comment JSON)."""
        found = self.find_marked_comment()
        if found is not None:
            comment_id = found.get("id")
            if not isinstance(comment_id, int):
                raise GitHubError("marked comment has no id")
            data, _ = self._send("PATCH", f"{self.api}/repos/{self.repo}/issues/comments/{comment_id}",
                                 {"body": body})
            return "updated", data or {}
        data, _ = self._send("POST", f"{self.api}/repos/{self.repo}/issues/{self.pr}/comments",
                             {"body": body})
        return "created", data or {}


# ---------------------------------------------------------------- CLI

def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


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


def _pr_number(event_path: Optional[str]) -> Optional[int]:
    if not event_path:
        return None
    try:
        with open(event_path, encoding="utf-8-sig") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        return None
    pull = event.get("pull_request") if isinstance(event, dict) else None
    number = pull.get("number") if isinstance(pull, dict) else None
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return number
    return None


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


def _post(args: argparse.Namespace, markdown: str, top: Scored) -> bool:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        _error("no repository: pass --repo owner/name or set GITHUB_REPOSITORY")
        return False
    match = _REPO_RE.match(repo)
    if not match or match.group(2) in (".", ".."):
        _error("repository must be written as owner/name")
        return False
    pr = args.pr or _pr_number(os.environ.get("GITHUB_EVENT_PATH"))
    if not pr:
        _error("no pull request number: pass --pr N or run on a pull_request event")
        return False
    token = os.environ.get(args.token_env, "")
    if not token:
        _error(f"no token: set {args.token_env} (the token needs pull-requests: write)")
        return False
    api = os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL
    try:
        action, comment = GitHubClient(api, token, repo, pr).upsert_comment(markdown)
    except GitHubError as exc:
        _error(str(exc))
        return False
    url = comment.get("html_url")
    link = f" {url}" if isinstance(url, str) and url else ""
    print(f"RunLedger receipt {action} on {repo}#{pr}: risk {top.score}/100 ({top.level}).{link}")
    return True


def cmd_comment(args: argparse.Namespace) -> int:
    if not args.session and not args.sessions_dir:
        _error("give at least one --session PATH or a --sessions-dir DIR")
        return EXIT_USAGE

    paths = _session_paths(args.session or [], args.sessions_dir)
    if not paths:
        where = f" in {args.sessions_dir}" if args.sessions_dir else ""
        _error(f"no session .jsonl files found{where}")
        return EXIT_ERROR

    items: List[Scored] = []
    for path in paths:
        try:
            run, score, level, risks, _note = build(str(path))
        except (OSError, ValueError) as exc:
            _error(f"cannot read session {path}: {exc}")
            return EXIT_ERROR
        items.append(Scored(run, score, level, risks))

    markdown = render_comment(items)
    top = max(items, key=lambda s: s.score)
    _write_step_outputs(markdown, top)

    posted = True
    if args.dry_run:
        _emit(markdown)
        print("Dry run: nothing was posted.", file=sys.stderr)
    else:
        posted = _post(args, markdown, top)

    if args.fail_on is not None and top.score >= args.fail_on:
        print(f"risk {top.score}/100 ({top.level}) is at or above --fail-on {args.fail_on}",
              file=sys.stderr)
        return EXIT_THRESHOLD
    return EXIT_OK if posted else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m runledger.github",
                                description="Post RunLedger risk receipts to GitHub pull requests.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("comment", help="post or update the receipt comment on a pull request")
    c.add_argument("--session", action="append", metavar="PATH",
                   help="session .jsonl file (repeatable)")
    c.add_argument("--sessions-dir", metavar="DIR",
                   help="score every .jsonl and .runledger.json session file in this folder")
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
