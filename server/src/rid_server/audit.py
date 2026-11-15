"""Request-scoped audit context shared by mutation routes and repositories."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request

from .auth import Principal


@dataclass(frozen=True)
class AuditContext:
    """Identity and request metadata persisted with one mutation."""

    request_id: str | None
    actor_subject: str | None
    actor_name: str | None
    actor_roles: tuple[str, ...]
    authenticated: bool
    method: str
    path: str
    action: str
    resource_type: str
    resource_id: str
    detail: dict[str, Any]


def build_audit_context(
    request: Request,
    principal: Principal,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    detail: dict[str, Any] | None = None,
) -> AuditContext:
    """Capture the authenticated actor and stable request id."""

    request_id = getattr(request.state, "request_id", None)
    return AuditContext(
        request_id=request_id if isinstance(request_id, str) else None,
        actor_subject=principal.subject if principal.authenticated else None,
        actor_name=principal.actor_name,
        actor_roles=tuple(principal.role_names),
        authenticated=principal.authenticated,
        method=request.method,
        path=request.url.path,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail or {},
    )
