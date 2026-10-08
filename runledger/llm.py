"""A small Anthropic Messages API client on the standard library.

Used by the optional Claude features: `--ai` step summaries (summarize.py) and
`--review` risk review (review.py). Nothing here runs unless a caller asks for it.

- Every text sent is passed through redact() first, using the same secret patterns
  as the risk rules, so a key pasted into a command does not leave the machine.
- Transient failures (429, 500, 502, 503, 529 and network errors) are retried up to
  MAX_RETRIES times with exponential backoff and jitter. A retry-after header
  replaces the computed delay (capped at MAX_RETRY_AFTER_S).
- LLMError never contains the API key. `status` is the HTTP status, or None when no
  response arrived.

ANTHROPIC_BASE_URL replaces https://api.anthropic.com (tests and proxies). Loopback
hosts bypass HTTP proxies.
"""
from __future__ import annotations

import http.client
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import guard as _guard  # the secret patterns shared with the risk rules
from .parser import Usage, _usage

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
BASE_URL_ENV = "ANTHROPIC_BASE_URL"
API_KEY_ENV = "ANTHROPIC_API_KEY"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 529})
MAX_RETRIES = 3              # retries after the first attempt, so at most 4 requests
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
MAX_RETRY_AFTER_S = 60.0     # a longer retry-after is cut to this
ERROR_TEXT_CHARS = 300
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class LLMError(RuntimeError):
    """A Claude API call failed. `status` is the HTTP status, or None if no response came back."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- redaction

def redact(text: Any, emails: bool = False) -> str:
    """Mask secret values (API keys, tokens, passwords, private keys) with the risk rules'
    patterns. With emails=True, e-mail addresses are masked too. Only the match is replaced."""
    s = _guard.redact(text)
    if emails:
        s = _EMAIL_RE.sub("[EMAIL]", s)
    return s


def _redact_messages(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            content = redact(content)
        elif isinstance(content, list):
            content = [dict(block, text=redact(block["text"]))
                       if isinstance(block, dict) and isinstance(block.get("text"), str) else block
                       for block in content]
        out.append(dict(message, content=content))
    return out


def _system_blocks(system: Any, cache: bool) -> Optional[List[Dict[str, Any]]]:
    if system is None:
        return None
    if isinstance(system, str):
        block: Dict[str, Any] = {"type": "text", "text": redact(system)}
        if cache:
            block["cache_control"] = {"type": "ephemeral"}   # prompt caching for the static prompt
        return [block]
    return list(system)


# ---------------------------------------------------------------- transport

def _api_key(api_key: Optional[str]) -> str:
    key = api_key or os.environ.get(API_KEY_ENV)
    if not key:
        raise LLMError(f"No API key: set {API_KEY_ENV}.")
    return key


def _base_url() -> str:
    return (os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).strip().rstrip("/")


def _opener_for(url: str) -> urllib.request.OpenerDirector:
    if (urllib.parse.urlsplit(url).hostname or "") in _LOOPBACK:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _scrub(text: str, api_key: str) -> str:
    """Remove the key itself and any secret pattern from text that goes into an error."""
    if api_key:
        text = text.replace(api_key, "[key]")
    text = redact(text)
    return " ".join(text.split())[:ERROR_TEXT_CHARS]


def _error_text(exc: urllib.error.HTTPError, api_key: str) -> str:
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # the body is optional detail
        raw = ""
    message = raw
    try:
        parsed = json.loads(raw)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict) and error.get("message"):
            message = str(error["message"])
    except ValueError:
        pass
    return _scrub(message or str(exc.reason or "no details"), api_key)


def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    value = exc.headers.get("retry-after") if exc.headers is not None else None
    try:
        seconds = float(value)  # HTTP-date values are ignored: the backoff applies instead
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _delay(attempt: int, retry_after: Optional[float]) -> float:
    if retry_after is not None:
        return min(retry_after, MAX_RETRY_AFTER_S)
    ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** attempt))
    return ceiling * random.uniform(0.5, 1.0)


def _post(body: Dict[str, Any], api_key: str, timeout: float) -> Dict[str, Any]:
    url = _base_url() + "/v1/messages"
    data = json.dumps(body).encode("utf-8")
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    for attempt in range(MAX_RETRIES + 1):
        retry_after: Optional[float] = None
        try:
            request = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with _opener_for(url).open(request, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:  # before URLError: HTTPError is a subclass
            status: Optional[int] = exc.code
            problem = f"Claude API error {exc.code}: {_error_text(exc, api_key)}"
            if exc.code not in RETRY_STATUSES:
                raise LLMError(problem, status=exc.code) from None
            retry_after = _retry_after(exc)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:  # refused, reset, timeout, bad reply
            status = None
            reason = getattr(exc, "reason", None) or exc
            problem = f"network error reaching Claude API: {_scrub(str(reason), api_key)}"
        else:
            break
        if attempt == MAX_RETRIES:
            raise LLMError(f"{problem} (gave up after {attempt + 1} attempts)", status=status) from None
        time.sleep(_delay(attempt, retry_after))
    try:
        reply = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise LLMError("Claude API returned a reply that is not JSON.", status=200) from None
    if not isinstance(reply, dict):
        raise LLMError("Claude API returned an unexpected reply.", status=200)
    return reply


# ---------------------------------------------------------------- public API

def call(messages: Sequence[Dict[str, Any]], model: str, system: Any = None, max_tokens: int = 1024,
         tools: Optional[List[Dict[str, Any]]] = None, tool_choice: Optional[Dict[str, Any]] = None,
         api_key: Optional[str] = None, timeout: float = 60, cache: bool = True) -> Dict[str, Any]:
    """POST one Messages API request and return the parsed reply (it includes "usage").

    `system` may be a string (sent as one block with cache_control "ephemeral" unless
    cache=False) or a list of content blocks. Message text is redacted before sending."""
    body: Dict[str, Any] = {"model": model, "max_tokens": int(max_tokens),
                            "messages": _redact_messages(list(messages))}
    blocks = _system_blocks(system, cache)
    if blocks:
        body["system"] = blocks
    if tools:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return _post(body, _api_key(api_key), timeout)


def structured_response(prompt: str, schema: Dict[str, Any], model: str, *, system: Any = None,
                        max_tokens: int = 1024, api_key: Optional[str] = None, timeout: float = 60,
                        tool_name: str = "record_result",
                        tool_description: str = "Record the structured result.",
                        ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Force one tool call whose input must follow `schema`. Returns (tool input, full reply)."""
    tools = [{"name": tool_name, "description": tool_description, "input_schema": schema}]
    reply = call([{"role": "user", "content": prompt}], model, system=system, max_tokens=max_tokens,
                 tools=tools, tool_choice={"type": "tool", "name": tool_name},
                 api_key=api_key, timeout=timeout)
    if reply.get("stop_reason") == "max_tokens":
        raise LLMError("Claude's answer was cut off at max_tokens before the structured result finished.")
    for block in reply.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == tool_name:
            data = block.get("input")
            if isinstance(data, dict):
                return data, reply
            break
    raise LLMError(f"Claude did not return the structured result '{tool_name}'.")


def structured(prompt: str, schema: Dict[str, Any], model: str, **kwargs: Any) -> Dict[str, Any]:
    """The parsed input of the forced tool call (see structured_response for the keyword arguments)."""
    data, _reply = structured_response(prompt, schema, model, **kwargs)
    return data


def text_of(reply: Dict[str, Any]) -> str:
    """All text blocks of a reply, joined."""
    return "".join(b.get("text", "") for b in reply.get("content") or []
                   if isinstance(b, dict) and b.get("type") == "text")


def usage(reply: Dict[str, Any]) -> Usage:
    """Token counts of a reply, ready for pricing.cost_of()."""
    return _usage(reply.get("usage"))
