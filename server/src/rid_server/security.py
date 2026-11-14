"""Optional API authentication and HTTP hardening middleware."""
from __future__ import annotations

import re
import secrets
import uuid
from typing import Any

import structlog
from fastapi import HTTPException, Request, status
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings, get_settings
from .metrics import RequestMetrics

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_WS_SUBPROTOCOL = "rid.v1"


def _settings_for_app(app: Any) -> Settings:
    return getattr(app.state, "settings", None) or get_settings()


def _api_token(settings: Settings) -> str:
    secret = settings.api_token
    return secret.get_secret_value() if secret is not None else ""


def _bearer_matches(header: str | None, expected: str) -> bool:
    if not header:
        return False
    scheme, _, credentials = header.partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(
        credentials.strip(), expected
    )


def require_api_token(request: Request) -> None:
    """FastAPI dependency: open when no token is configured, else Bearer only."""
    settings = _settings_for_app(request.app)
    expected = _api_token(settings)
    if expected and not _bearer_matches(request.headers.get("authorization"), expected):
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.auth_failures += 1
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def authorize_websocket(websocket: Any) -> tuple[bool, str | None]:
    """Check a WebSocket handshake and return (allowed, subprotocol).

    Browsers cannot attach an Authorization header to a WebSocket.  The
    dashboard therefore offers the token as a ``bearer.<token>``
    subprotocol alongside the versioned ``rid.v1`` subprotocol.  A real
    reverse proxy can still inject an Authorization header for non-browser
    clients.
    """
    settings = _settings_for_app(websocket.app)
    offered = [
        item.strip()
        for item in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if item.strip()
    ]
    accept_protocol = _WS_SUBPROTOCOL if _WS_SUBPROTOCOL in offered else None
    expected = _api_token(settings)
    if not expected:
        return True, accept_protocol

    authorization = websocket.headers.get("authorization")
    if _bearer_matches(authorization, expected):
        return True, accept_protocol
    for protocol in offered:
        prefix = "bearer."
        if protocol.startswith(prefix) and secrets.compare_digest(
            protocol[len(prefix):], expected
        ):
            return True, accept_protocol
    metrics = getattr(websocket.app.state, "metrics", None)
    if metrics is not None:
        metrics.auth_failures += 1
    return False, accept_protocol


class RequestContextMiddleware:
    """Request IDs, security headers, timing and structured access context."""

    def __init__(self, app: ASGIApp, metrics: RequestMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get("x-request-id", "")
        if not _REQUEST_ID_RE.fullmatch(request_id):
            request_id = uuid.uuid4().hex

        started_at = self.metrics.begin()
        status_code = 500
        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        app = scope.get("app")
        settings = getattr(getattr(app, "state", None), "settings", None)
        forwarded_proto = headers.get("x-forwarded-proto", "").lower()
        is_https = scope.get("scheme") == "https" or forwarded_proto == "https"

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["Cache-Control"] = "no-store"
                response_headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=()"
                )
                response_headers["Cross-Origin-Resource-Policy"] = (
                    "cross-origin"
                    if getattr(settings, "cors_origin_list", [])
                    else "same-origin"
                )
                if (
                    is_https
                    and getattr(settings, "env", "dev").lower()
                    in {"prod", "production"}
                ):
                    response_headers["Strict-Transport-Security"] = (
                        "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        with structlog.contextvars.bound_contextvars(
            request_id=request_id,
            method=method,
            path=path,
        ):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                route = scope.get("route")
                route_path = getattr(route, "path", None) or "unmatched"
                self.metrics.observe(
                    method=method,
                    route=route_path,
                    status_code=status_code,
                    started_at=started_at,
                )
