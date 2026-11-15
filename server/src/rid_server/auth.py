"""Named API-key authentication and role-based access control.

The legacy ``RID_API_TOKEN`` remains supported as an administrator
credential.  Production deployments can instead mount a JSON key file
whose entries identify individual operators and expose only SHA-256
digests of high-entropy API keys.  The file is re-read when its content
changes so credentials can be rotated without restarting the API.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .config import Settings
from .logging_setup import get_logger

log = get_logger(__name__)

_SUBJECT_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_CREDENTIAL_LENGTH = 512
_MAX_CONFIG_BYTES = 1024 * 1024
_WS_PROTOCOL = "rid.v1"


class Role(StrEnum):
    """Ordered operational roles.

    A higher role includes every capability of the roles below it.
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


_ROLE_RANK = {
    Role.VIEWER: 1,
    Role.OPERATOR: 2,
    Role.ADMIN: 3,
}

_CAPABILITIES = {
    Role.VIEWER: ("data:read",),
    Role.OPERATOR: ("data:read", "alerts:write"),
    Role.ADMIN: (
        "data:read",
        "alerts:write",
        "zones:write",
        "audit:read",
    ),
}


class AuthConfigError(ValueError):
    """Authentication configuration cannot be used safely."""


@dataclass(frozen=True)
class Principal:
    """An authenticated (or explicitly anonymous) API caller."""

    subject: str
    display_name: str
    roles: frozenset[Role]
    authenticated: bool
    key_id: str | None = None

    def has_role(self, required: Role) -> bool:
        rank = _ROLE_RANK[required]
        return any(_ROLE_RANK[role] >= rank for role in self.roles)

    @property
    def role_names(self) -> list[str]:
        return [role.value for role in sorted(self.roles, key=_ROLE_RANK.get)]

    @property
    def capabilities(self) -> list[str]:
        highest = max(
            (_ROLE_RANK[role] for role in self.roles),
            default=0,
        )
        for role, rank in _ROLE_RANK.items():
            if rank == highest:
                return list(_CAPABILITIES[role])
        return []

    @property
    def actor_name(self) -> str:
        return self.display_name.strip() or self.subject

    @classmethod
    def anonymous(cls) -> Principal:
        """Open-mode principal used only when authentication is disabled."""
        return cls(
            subject="anonymous",
            display_name="Open mode",
            roles=frozenset(Role),
            authenticated=False,
            key_id=None,
        )


@dataclass(frozen=True)
class _ApiKeyRecord:
    key_id: str
    subject: str
    display_name: str
    roles: frozenset[Role]
    digest: str
    enabled: bool

    def principal(self) -> Principal:
        return Principal(
            subject=self.subject,
            display_name=self.display_name,
            roles=self.roles,
            authenticated=True,
            key_id=self.key_id,
        )


def hash_api_key(api_key: str) -> str:
    """Return the lowercase SHA-256 digest stored in the key file.

    API keys must be generated from at least 32 bytes of randomness, so
    a fast digest is sufficient for verification.  Passwords or
    user-chosen tokens must not use this scheme.
    """

    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _parse_roles(value: Any, *, key_id: str) -> frozenset[Role]:
    if not isinstance(value, list) or not value:
        raise AuthConfigError(
            f"key {key_id!r} must declare at least one role"
        )
    roles: set[Role] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise AuthConfigError(f"key {key_id!r} has a non-string role")
        try:
            roles.add(Role(raw.strip().lower()))
        except ValueError as exc:
            raise AuthConfigError(
                f"key {key_id!r} has unknown role {raw!r}"
            ) from exc
    return frozenset(roles)


def _parse_key_file(raw: bytes, path: Path) -> list[_ApiKeyRecord]:
    if len(raw) > _MAX_CONFIG_BYTES:
        raise AuthConfigError(
            f"authentication file {path} exceeds 1 MiB"
        )
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuthConfigError(
            f"authentication file {path} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(document, dict):
        raise AuthConfigError("authentication file must contain an object")
    if document.get("version") != 1:
        raise AuthConfigError("authentication file version must be 1")
    entries = document.get("keys")
    if not isinstance(entries, list):
        raise AuthConfigError("authentication file must contain a keys array")

    records: list[_ApiKeyRecord] = []
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    seen_subjects: dict[str, tuple[str, frozenset[Role]]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AuthConfigError(f"key entry {index} must be an object")
        if "api_key" in entry or "token" in entry:
            raise AuthConfigError(
                f"key entry {index} contains a plaintext credential; "
                "store api_key_sha256 instead"
            )
        key_id = str(entry.get("key_id", "")).strip()
        if not key_id or len(key_id) > 64:
            raise AuthConfigError(
                f"key entry {index} needs a key_id of 1-64 characters"
            )
        if key_id in seen_ids:
            raise AuthConfigError(f"duplicate key_id {key_id!r}")
        seen_ids.add(key_id)

        subject = str(entry.get("subject", "")).strip()
        if not _SUBJECT_RE.fullmatch(subject):
            raise AuthConfigError(
                f"key {key_id!r} has an invalid subject"
            )
        display_name = str(entry.get("display_name", "")).strip()
        if not display_name or len(display_name) > 128:
            raise AuthConfigError(
                f"key {key_id!r} needs a display_name of 1-128 characters"
            )
        digest = str(entry.get("api_key_sha256", "")).strip().lower()
        if not _SHA256_RE.fullmatch(digest):
            raise AuthConfigError(
                f"key {key_id!r} api_key_sha256 must be 64 hex characters"
            )
        if digest in seen_digests:
            raise AuthConfigError("duplicate API-key digest")
        seen_digests.add(digest)
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AuthConfigError(
                f"key {key_id!r} enabled must be a boolean"
            )
        roles = _parse_roles(entry.get("roles"), key_id=key_id)
        identity = (display_name, roles)
        prior_identity = seen_subjects.get(subject)
        if prior_identity is not None and prior_identity != identity:
            raise AuthConfigError(
                f"subject {subject!r} has conflicting display names or roles"
            )
        seen_subjects[subject] = identity
        records.append(
            _ApiKeyRecord(
                key_id=key_id,
                subject=subject,
                display_name=display_name,
                roles=roles,
                digest=digest,
                enabled=enabled,
            )
        )
    return records


def _bearer_token(header: str | None) -> str:
    if not header:
        return ""
    scheme, separator, credentials = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    token = credentials.strip()
    return token if len(token) <= _MAX_CREDENTIAL_LENGTH else ""


def _header_value(headers: Mapping[str, str] | Any, name: str) -> str | None:
    if hasattr(headers, "get"):
        value = headers.get(name)
        return value if isinstance(value, str) else None
    return None


class AuthStore:
    """Loaded credentials plus their role assignments."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._path = (
            Path(settings.auth_users_file).expanduser()
            if settings.auth_users_file
            else None
        )
        self._interval = settings.auth_reload_interval_s
        self._last_check = 0.0
        self._content_digest: str | None = None
        self._file_records: list[_ApiKeyRecord] = []
        self._legacy_records = self._legacy_credentials(settings)
        self._required = bool(self._legacy_records or self._path)
        if self._path is not None:
            self._reload_file(initial=True)
        if self._required and not any(
            record.enabled for record in self._all_records
        ):
            raise AuthConfigError(
                "authentication is enabled but no usable API keys are configured"
            )

    @staticmethod
    def _legacy_credentials(settings: Settings) -> list[_ApiKeyRecord]:
        token = (
            settings.api_token.get_secret_value().strip()
            if settings.api_token is not None
            else ""
        )
        if not token:
            return []
        return [
            _ApiKeyRecord(
                key_id="legacy-api-token",
                subject="legacy-admin",
                display_name="Administrator",
                roles=frozenset({Role.ADMIN}),
                digest=hash_api_key(token),
                enabled=True,
            )
        ]

    @property
    def required(self) -> bool:
        return self._required

    @property
    def principal_count(self) -> int:
        return len(
            {
                record.subject
                for record in self._all_records
                if record.enabled
            }
        )

    @property
    def _all_records(self) -> list[_ApiKeyRecord]:
        return [*self._legacy_records, *self._file_records]

    def _reload_file(self, *, initial: bool = False) -> None:
        assert self._path is not None
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            if initial:
                raise AuthConfigError(
                    f"cannot read authentication file {self._path}: {exc}"
                ) from exc
            log.error(
                "auth.reload.failed",
                path=str(self._path),
                error=str(exc)[:200],
            )
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest == self._content_digest:
            return
        try:
            records = _parse_key_file(raw, self._path)
        except AuthConfigError as exc:
            if initial:
                raise
            # Keep the last known-good set during a partial/bad rotation.
            log.error(
                "auth.reload.invalid",
                path=str(self._path),
                error=str(exc)[:200],
            )
            return
        self._file_records = records
        self._content_digest = digest
        if not any(record.enabled for record in self._all_records):
            if initial:
                raise AuthConfigError(
                    "authentication file contains no enabled keys"
                )
            log.error("auth.reload.empty", path=str(self._path))
            return
        log.info(
            "auth.keys.loaded",
            path=str(self._path),
            keys=len(records),
            principals=len({record.subject for record in records}),
        )

    def reload_if_changed(self, *, force: bool = False) -> None:
        if self._path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_check < self._interval:
            return
        self._last_check = now
        self._reload_file()

    def _authenticate_token(self, token: str) -> Principal | None:
        if not token:
            return None
        self.reload_if_changed()
        digest = hash_api_key(token)
        matched: _ApiKeyRecord | None = None
        # Compare every digest instead of using a dict lookup.  This keeps
        # verification time independent of where a key sits in the file and
        # avoids accidentally exposing key identifiers through timing.
        for record in self._all_records:
            if secrets.compare_digest(digest, record.digest):
                matched = record
        if matched is None or not matched.enabled:
            return None
        return matched.principal()

    def resolve_http(self, headers: Mapping[str, str] | Any) -> Principal | None:
        """Resolve HTTP credentials, falling back to anonymous in open mode."""

        if not self._required:
            return Principal.anonymous()
        return self._authenticate_token(
            _bearer_token(_header_value(headers, "authorization"))
        )

    def resolve_websocket(
        self,
        websocket: Any,
    ) -> tuple[Principal | None, str | None]:
        """Resolve WebSocket credentials and the protocol to echo.

        Browsers cannot set an Authorization header on a WebSocket, so
        API keys may also be offered as ``bearer.<key>`` subprotocols.
        The secret itself is never echoed as the accepted subprotocol.
        """

        offered = [
            item.strip()
            for item in (
                _header_value(
                    websocket.headers,
                    "sec-websocket-protocol",
                )
                or ""
            ).split(",")
            if item.strip()
        ]
        accept_protocol = _WS_PROTOCOL if _WS_PROTOCOL in offered else None
        if not self._required:
            return Principal.anonymous(), accept_protocol

        header_principal = self._authenticate_token(
            _bearer_token(
                _header_value(websocket.headers, "authorization")
            )
        )
        if header_principal is not None:
            return header_principal, accept_protocol
        for protocol in offered:
            prefix = "bearer."
            if protocol.startswith(prefix):
                principal = self._authenticate_token(protocol[len(prefix):])
                if principal is not None:
                    return principal, accept_protocol
        return None, accept_protocol

    def principal_for_token(self, token: str) -> Principal | None:
        """Public helper used by credential-generation and tests."""

        return self._authenticate_token(token)
