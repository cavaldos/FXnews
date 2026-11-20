"""Request guards for a local-first API.

The API has no login: it is meant to be reachable only from the local machine. Two browser-based attacks
still work against such a server, and both are closed here:

* Cross-site request forgery - any web page the user opens can fire a "simple" cross-origin POST (no body, no
  custom header => no CORS preflight) at http://127.0.0.1:8000, e.g. /api/news/N/analyze (LLM spend) or
  /api/ingest/run. CORS only stops the page from *reading* the answer, not the request from running.
  ``CrossSiteGuardMiddleware`` rejects state-changing requests whose Origin is not allowed.
* DNS rebinding - a hostile name resolving to 127.0.0.1 makes the API same-origin for the attacker's page.
  ``TrustedHostMiddleware`` rejects Host headers that are not on the allow-list.

Non-browser clients (curl, scripts, the Vite dev proxy) are unaffected: they send no Origin header.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


class CrossSiteGuardMiddleware:
    """Pure ASGI middleware (so it never buffers the SSE stream). Answers 403 to an unsafe-method request when

    * it carries an ``Origin`` that is not in ``allowed_origins`` (``"null"`` included), unless that Origin is
      the server's own origin (its authority equals the Host header - e.g. a UI served by this app), or
    * it carries no ``Origin`` but the browser marked it ``Sec-Fetch-Site: cross-site``.

    An Origin listed in ``allowed_origins`` always passes, even if the browser calls it cross-site: the operator
    allowed it on purpose. GET/HEAD/OPTIONS are never blocked (read-only; CORS already hides the response).
    """

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed = {normalize_origin(o) for o in allowed_origins}
        self.allow_any = "*" in self.allowed

    def rejection_reason(self, headers: Headers) -> str | None:
        origin = headers.get("origin")
        if origin is not None:
            norm = normalize_origin(origin)
            if self.allow_any or norm in self.allowed:
                return None
            host = headers.get("host", "").strip().lower()
            if norm != "null" and host and urlsplit(norm).netloc == host:
                return None  # same-origin request (Host itself was vetted by TrustedHostMiddleware)
            return f"origin {origin!r} is not allowed"
        if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
            return "cross-site request without an allowed Origin"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        reason = self.rejection_reason(Headers(scope=scope))
        if reason is None:
            await self.app(scope, receive, send)
            return
        log.warning("blocked %s %s: %s", scope["method"], scope.get("path"), reason)
        body = json.dumps({"detail": f"Cross-site request blocked: {reason}"}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def install_security(app: FastAPI, *, allowed_origins: list[str], allowed_hosts: list[str]) -> None:
    """Add CSRF guard (innermost), CORS, then the Host allow-list (outermost) to ``app``."""
    app.add_middleware(CrossSiteGuardMiddleware, allowed_origins=allowed_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
