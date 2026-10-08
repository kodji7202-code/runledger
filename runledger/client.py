"""Push a Claude Code session receipt to a RunLedger team server.

The receipt is built the same way as `runledger receipt` (parser, pricing and
risk rules from cli.build), then POSTed as JSON to /api/runs with the developer
name and project folder. Standard library only (urllib).
"""
from __future__ import annotations

import getpass
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Union
from urllib.parse import urlsplit

from . import __version__
from .cli import build
from .pricing import cost_of
from .receipt import render

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class PushError(RuntimeError):
    """The receipt could not be built or delivered. The message is safe to print."""


def default_user() -> str:
    """git config user.email, else the OS user name."""
    try:
        out = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return getpass.getuser()
    except Exception:  # no USER/USERNAME in the environment
        return "unknown"


def _folder_name(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    parts = [p for p in re.split(r"[\\/]+", cwd.strip()) if p]
    return parts[-1] if parts else None


def build_payload(
    session_path: Union[str, Path],
    user: Optional[str] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """The JSON body for POST /api/runs: the receipt plus user, project, html."""
    path = Path(session_path)
    if not path.is_file():
        raise PushError(f"Session file not found: {path}")
    run, score, level, risks, _note = build(str(path))
    payload = json.loads(render(run, score, level, risks, "json"))
    # Exact cost per model, including messages that issued no tool call
    # (step costs alone would miss those).
    payload["models"] = {m: {"tokens": u.total, "cost_usd": cost_of(u, m)} for m, u in run.models.items()}
    payload["user"] = (user or default_user()).strip() or "unknown"
    payload["project"] = project or _folder_name(run.cwd) or "unknown"
    payload["html"] = render(run, score, level, risks, "html")
    return payload


def _opener_for(url: str) -> urllib.request.OpenerDirector:
    if (urlsplit(url).hostname or "") in LOOPBACK_HOSTS:
        # A local server never needs a proxy, even when HTTP_PROXY is set.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _server_message(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read().decode("utf-8"))["error"]["message"])[:300]
    except Exception:
        return str(exc.reason) if exc.reason else "no details"


def push(
    server_url: str,
    api_key: str,
    session_path: Union[str, Path],
    user: Optional[str] = None,
    project: Optional[str] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Build the receipt for a session and upload it. Returns the server's reply
    ({"id", "url", "risk_score", "risk_level"})."""
    if not server_url:
        raise PushError("No server URL. Pass --server or set RUNLEDGER_SERVER.")
    if not api_key:
        raise PushError("No API key. Pass --key or set RUNLEDGER_API_KEY.")
    payload = build_payload(session_path, user=user, project=project)
    endpoint = server_url.rstrip("/") + "/api/runs"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"runledger/{__version__}",
        },
    )
    try:
        with _opener_for(endpoint).open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # before URLError: HTTPError is a subclass
        raise PushError(f"Server refused the run (HTTP {exc.code}): {_server_message(exc)}") from None
    except urllib.error.URLError as exc:
        raise PushError(f"Could not reach {server_url}: {exc.reason}") from None
    except (ValueError, OSError) as exc:  # unreadable reply, or a read timeout
        raise PushError(f"Unexpected reply from {server_url}: {exc}") from None
