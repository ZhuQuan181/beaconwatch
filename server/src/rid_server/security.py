"""Named API-key authentication, RBAC dependencies and HTTP hardening."""
from __future__ import annotations

import re
import uuid
from typing import Annotated, Any

import structlog
from fastapi import Depends, HTTPException, Request, status
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import AuthStore, Principal, Role
from .config import Settings, get_settings
from .metrics import RequestMetrics

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _settings_for_app(app: Any) -> Settings:
    return getattr(app.state, "settings", None) or get_settings()


def _auth_store_for_app(app: Any) -> AuthStore:
    settings = _settings_for_app(app)
    store = getattr(app.state, "auth", None)
    if not isinstance(store, AuthStore) or store.settings is not settings:
        store = AuthStore(settings)
        app.state.auth = store
    return store


def _unauthorized(request: Request) -> HTTPException:
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics.auth_failures += 1
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(request: Request, required: Role) -> HTTPException:
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics.authz_failures += 1
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"{required.value} role required",
    )


def get_current_principal(request: Request) -> Principal:
    """Resolve the caller or return an explicit open-mode principal."""

    store = _auth_store_for_app(request.app)
    request.state.auth_store = store
    principal = store.resolve_http(request.headers)
    if principal is None:
        raise _unauthorized(request)
    request.state.principal = principal
    return principal


def require_viewer(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """Require read access (or open mode)."""

    return principal


def require_operator(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """Require alert disposition privileges."""

    if not principal.has_role(Role.OPERATOR):
        raise _forbidden(request, Role.OPERATOR)
    return principal


def require_admin(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """Require configuration and audit privileges."""

    if not principal.has_role(Role.ADMIN):
        raise _forbidden(request, Role.ADMIN)
    return principal


def authorize_websocket(
    websocket: Any,
) -> tuple[Principal | None, str | None]:
    """Check a WebSocket handshake and return (principal, subprotocol).

    Browsers cannot attach an Authorization header to a WebSocket.  The
    dashboard therefore offers the token as a ``bearer.<token>``
    subprotocol alongside the versioned ``rid.v1`` subprotocol.  A real
    reverse proxy can still inject an Authorization header for non-browser
    clients.
    """
    store = _auth_store_for_app(websocket.app)
    principal, accept_protocol = store.resolve_websocket(websocket)
    if principal is not None and principal.has_role(Role.VIEWER):
        return principal, accept_protocol
    metrics = getattr(websocket.app.state, "metrics", None)
    if metrics is not None:
        metrics.auth_failures += 1
    return None, accept_protocol


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
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

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
