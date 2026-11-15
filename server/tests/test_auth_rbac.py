"""Named API-key authentication, role enforcement and audit tests."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest
from rid_server.alerts import Alert
from rid_server.app import create_app
from rid_server.auth import AuthConfigError, AuthStore, Role, hash_api_key
from rid_server.bus import ObservationBus
from rid_server.config import Settings
from rid_server.mqtt_subscriber import MqttSubscriber
from rid_server.repository import list_alert_events, upsert_alert
from sqlalchemy.ext.asyncio import async_sessionmaker


def _write_key_file(path, entries: list[dict]) -> None:
    path.write_text(
        json.dumps({"version": 1, "keys": entries}),
        encoding="utf-8",
    )


def _entry(
    key_id: str,
    subject: str,
    display_name: str,
    role: str,
    token: str,
    *,
    enabled: bool = True,
) -> dict:
    return {
        "key_id": key_id,
        "subject": subject,
        "display_name": display_name,
        "roles": [role],
        "api_key_sha256": hash_api_key(token),
        "enabled": enabled,
    }


def _settings(path) -> Settings:
    return Settings(
        auth_users_file=str(path),
        auth_reload_interval_s=1,
        log_level="WARNING",
    )


def _app(settings: Settings):
    app = create_app()
    app.state.settings = settings
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(settings)
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_alert(db_engine) -> str:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    alert = Alert(
        alert_id="auth-test-alert",
        rule="altitude",
        severity="warn",
        drone_id="drone-auth",
        message="test alert",
        detail={},
        at=datetime.now(timezone.utc).isoformat(),
    )
    async with factory() as session:
        row = await upsert_alert(session, alert)
    return row.alert_id


def test_auth_store_roles_and_capabilities(tmp_path):
    viewer_token = "viewer-secret"
    operator_token = "operator-secret"
    admin_token = "admin-secret"
    path = tmp_path / "keys.json"
    _write_key_file(
        path,
        [
            _entry("viewer-1", "viewer", "Viewer", "viewer", viewer_token),
            _entry(
                "operator-1",
                "operator",
                "Operator",
                "operator",
                operator_token,
            ),
            _entry("admin-1", "admin", "Admin", "admin", admin_token),
        ],
    )
    store = AuthStore(_settings(path))

    viewer = store.principal_for_token(viewer_token)
    operator = store.principal_for_token(operator_token)
    admin = store.principal_for_token(admin_token)

    assert viewer is not None and viewer.has_role(Role.VIEWER)
    assert viewer is not None and not viewer.has_role(Role.OPERATOR)
    assert operator is not None and operator.has_role(Role.OPERATOR)
    assert operator is not None and not operator.has_role(Role.ADMIN)
    assert admin is not None and admin.has_role(Role.ADMIN)
    assert admin is not None and "audit:read" in admin.capabilities
    assert store.principal_for_token("wrong") is None
    assert store.required is True
    assert store.principal_count == 3


def test_auth_file_reload_keeps_last_good_rotation(tmp_path):
    first_key = "first-secret"
    second_key = "second-secret"
    path = tmp_path / "keys.json"
    _write_key_file(
        path,
        [_entry("first", "first", "First", "viewer", first_key)],
    )
    store = AuthStore(_settings(path))
    assert store.principal_for_token(first_key) is not None

    path.write_text("{not-json", encoding="utf-8")
    store.reload_if_changed(force=True)
    assert store.principal_for_token(first_key) is not None

    _write_key_file(
        path,
        [_entry("second", "second", "Second", "operator", second_key)],
    )
    store.reload_if_changed(force=True)
    assert store.principal_for_token(first_key) is None
    second = store.principal_for_token(second_key)
    assert second is not None and second.has_role(Role.OPERATOR)


def test_auth_file_rejects_plaintext_and_unknown_roles(tmp_path):
    path = tmp_path / "keys.json"
    _write_key_file(
        path,
        [
            {
                "key_id": "bad",
                "subject": "bad",
                "display_name": "Bad",
                "roles": ["viewer"],
                "api_key": "do-not-store-this",
            }
        ],
    )
    with pytest.raises(AuthConfigError, match="plaintext"):
        AuthStore(_settings(path))

    _write_key_file(
        path,
        [
            _entry("bad-role", "bad", "Bad", "root", "secret"),
        ],
    )
    with pytest.raises(AuthConfigError, match="unknown role"):
        AuthStore(_settings(path))


async def test_http_rbac_and_authentication(db_engine, tmp_path):
    alert_id = await _seed_alert(db_engine)
    viewer_token = "viewer-token"
    operator_token = "operator-token"
    admin_token = "admin-token"
    path = tmp_path / "keys.json"
    _write_key_file(
        path,
        [
            _entry("v", "viewer", "Viewer", "viewer", viewer_token),
            _entry("o", "operator", "Alice Operator", "operator", operator_token),
            _entry("a", "admin", "Admin", "admin", admin_token),
        ],
    )
    app = _app(_settings(path))

    async with _client(app) as client:
        unauth = await client.get("/stations")
        health = await client.get("/health")
        me = await client.get("/auth/me", headers=_auth(viewer_token))
        viewer_get = await client.get("/stations", headers=_auth(viewer_token))
        viewer_ack = await client.post(
            f"/alerts/{alert_id}/ack",
            json={"note": "not allowed"},
            headers=_auth(viewer_token),
        )
        operator_ack = await client.post(
            f"/alerts/{alert_id}/ack",
            json={"acknowledged_by": "spoofed", "note": "handled"},
            headers=_auth(operator_token),
        )
        operator_audit = await client.get(
            "/audit/events",
            headers=_auth(operator_token),
        )
        admin_audit = await client.get(
            "/audit/events",
            headers=_auth(admin_token),
        )
        admin_zone = await client.put(
            "/zones/zone-rbac",
            json={
                "zone_id": "zone-rbac",
                "name": "Test zone",
                "polygon": [[0, 0], [1, 0], [1, 1]],
                "severity": "warn",
            },
            headers=_auth(admin_token),
        )
        audit_after_zone = await client.get(
            "/audit/events",
            headers=_auth(admin_token),
        )

    assert unauth.status_code == 401
    assert health.status_code == 200
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["roles"] == ["viewer"]
    assert me.json()["capabilities"] == ["data:read"]
    assert viewer_get.status_code == 200
    assert viewer_ack.status_code == 403
    assert operator_ack.status_code == 200
    assert operator_ack.json()["acknowledged_by"] == "Alice Operator"
    assert operator_audit.status_code == 403
    assert admin_audit.status_code == 200
    assert any(
        item["action"] == "alert.acknowledge"
        and item["actor_subject"] == "operator"
        and item["actor_name"] == "Alice Operator"
        for item in admin_audit.json()["items"]
    )
    assert admin_zone.status_code == 200
    assert audit_after_zone.status_code == 200
    assert any(
        item["action"] == "zone.upsert"
        and item["resource_id"] == "zone-rbac"
        for item in audit_after_zone.json()["items"]
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        events = await list_alert_events(session, alert_id)
    ack_event = next(event for event in events if event.action == "acknowledged")
    assert ack_event.actor_subject == "operator"
    assert ack_event.request_id is not None
    assert ack_event.actor == "Alice Operator"


async def test_open_mode_keeps_legacy_actor_contract(db_engine):
    alert_id = await _seed_alert(db_engine)
    app = _app(Settings(log_level="WARNING"))
    async with _client(app) as client:
        missing = await client.post(
            f"/alerts/{alert_id}/ack",
            json={"note": "no actor"},
        )
        accepted = await client.post(
            f"/alerts/{alert_id}/ack",
            json={"acknowledged_by": "legacy-operator"},
        )
    assert missing.status_code == 400
    assert accepted.status_code == 200
    assert accepted.json()["acknowledged_by"] == "legacy-operator"


async def test_audit_cursor_and_filters(db_engine, tmp_path):
    admin_token = "admin-token"
    path = tmp_path / "keys.json"
    _write_key_file(
        path,
        [_entry("a", "admin", "Admin", "admin", admin_token)],
    )
    app = _app(_settings(path))
    async with _client(app) as client:
        for index in range(3):
            response = await client.put(
                f"/zones/audit-zone-{index}",
                json={
                    "zone_id": f"audit-zone-{index}",
                    "name": f"Zone {index}",
                    "polygon": [[0, 0], [1, 0], [1, 1]],
                    "severity": "warn",
                },
                headers=_auth(admin_token),
            )
            assert response.status_code == 200
        first = await client.get(
            "/audit/events",
            params={"limit": 1},
            headers=_auth(admin_token),
        )
        cursor = first.json()["next_cursor"]
        second = await client.get(
            "/audit/events",
            params={"limit": 10, "cursor": cursor},
            headers=_auth(admin_token),
        )
        filtered = await client.get(
            "/audit/events",
            params={"resource_id": "audit-zone-1"},
            headers=_auth(admin_token),
        )
        bad_cursor = await client.get(
            "/audit/events",
            params={"cursor": "not-an-audit-cursor"},
            headers=_auth(admin_token),
        )
    assert first.status_code == 200
    assert len(first.json()["items"]) == 1
    assert len(second.json()["items"]) == 2
    assert second.json()["items"][0]["event_id"] != first.json()["items"][0][
        "event_id"
    ]
    assert filtered.status_code == 200
    assert len(filtered.json()["items"]) == 1
    assert filtered.json()["items"][0]["resource_id"] == "audit-zone-1"
    assert bad_cursor.status_code == 400


def test_legacy_token_is_an_admin_credential():
    store = AuthStore(Settings(api_token="legacy-admin"))
    principal = store.principal_for_token("legacy-admin")
    assert principal is not None
    assert principal.has_role(Role.ADMIN)
    assert principal.key_id == "legacy-api-token"


def test_hashes_are_sha256_of_the_api_key():
    assert hash_api_key("abc") == hashlib.sha256(b"abc").hexdigest()
