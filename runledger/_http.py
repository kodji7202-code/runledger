"""Reading an HTTPError body without leaking the connection.

`urllib` raises `HTTPError` with the response still open, so every reader of the error
body must close it: an unclosed response is a `ResourceWarning` (a hard failure under
`-W error::ResourceWarning`) and keeps the socket alive in a long-running process.

Standard library only.
"""
from __future__ import annotations

import urllib.error
from typing import Optional


def read_error(exc: urllib.error.HTTPError, limit: Optional[int] = None) -> str:
    """The error body as text, and the response is always closed.

    The body is optional detail: a body that cannot be read yields an empty string, and a
    body that cannot be closed must not hide the error the caller is already reporting."""
    try:
        raw = exc.read(limit) if limit is not None else exc.read()
    except Exception:
        raw = b""
    finally:
        try:
            exc.close()
        except Exception:
            pass
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
