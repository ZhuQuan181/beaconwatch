"""End-to-end chain check using real OpenDroneID golden vectors.

The identified drone starts as a complete official 802.11 Beacon frame;
the unidentified drone starts as a standards-compliant Message Pack made
from official sub-messages.  Both then traverse the real receiver /
parser / MQTT / database / HTTP path:

  official radio bytes -> WifiReceiver/PipelineManager -> MQTT -> EMQX
  -> MqttSubscriber -> Postgres/TimescaleDB -> FastAPI -> HTTP

Usage:
    RID_DB_PORT=5433 RID_MQTT_HOST=127.0.0.1 \
        python scripts/e2e_chain.py
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (
    ROOT / "edge" / "src",
    ROOT / "server" / "src",
    ROOT / "edge" / "tests",
):
    sys.path.insert(0, str(path))

import aiomqtt  # noqa: E402
from fixtures.odid_vectors import (  # noqa: E402
    LOCATION,
    SELF_ID,
    SYSTEM,
    WIFI_BEACON_FRAME,
)
from rid_edge.models import RidObservation  # noqa: E402
from rid_edge.odid_parser import message_pack  # noqa: E402
from rid_edge.pipeline import PipelineManager  # noqa: E402
from rid_edge.receivers.base import Bearer, RawFrame  # noqa: E402
from rid_edge.receivers.wifi_nan import WifiReceiver  # noqa: E402
from rid_server.alerts import AlertEngine, AlertRing  # noqa: E402
from rid_server.bus import ObservationBus  # noqa: E402
from rid_server.config import get_settings  # noqa: E402
from rid_server.mqtt_subscriber import MqttSubscriber  # noqa: E402
from rid_server.settings_state import set_subscriber_bus_hook  # noqa: E402
from scapy.layers.dot11 import RadioTap  # noqa: E402


def _official_beacon_frame() -> RawFrame:
    receiver = WifiReceiver()
    packet = RadioTap(
        bytes(RadioTap(present=0x20, dBm_AntSignal=-61)) + WIFI_BEACON_FRAME
    )
    receiver._on_pkt(packet)
    if receiver._queue.empty():
        raise RuntimeError("official beacon did not reach the receiver queue")
    return receiver._queue.get_nowait()


def _unidentified_frame(run: str) -> RawFrame:
    """Location + Self-ID + System, deliberately no Basic/Operator ID."""
    builder = message_pack.MessagePackParser()
    for msg_type, payload in (
        (message_pack.SubMessageType.LOCATION, LOCATION),
        (message_pack.SubMessageType.SELF_ID, SELF_ID),
        (message_pack.SubMessageType.SYSTEM, SYSTEM),
    ):
        builder.add(message_pack.SubMessage(msg_type, payload))

    suffix = int(run[:8], 16)
    mac = f"02:00:{(suffix >> 24) & 0xff:02x}:{(suffix >> 16) & 0xff:02x}:{(suffix >> 8) & 0xff:02x}:{suffix & 0xff:02x}"
    return RawFrame(
        bearer=Bearer.BLE,
        source_mac=mac,
        rssi_dbm=-63,
        payload=bytes(builder.build()),
        timestamp=datetime.now(timezone.utc),
    )


def _build_observations(run: str, station: str) -> tuple[RidObservation, RidObservation]:
    pipeline = PipelineManager(station_id=station)

    identified = pipeline.process(_official_beacon_frame())
    if len(identified) != 1:
        raise RuntimeError(f"expected one identified observation, got {len(identified)}")

    unidentified = pipeline.process(_unidentified_frame(run))
    if len(unidentified) != 1:
        raise RuntimeError(
            f"expected one unidentified observation, got {len(unidentified)}"
        )
    return identified[0], unidentified[0]


async def main() -> int:
    run = uuid.uuid4().hex[:8]
    station = f"E2E-{run}"
    topic_version = f"v2-{run}"

    base_settings = get_settings()
    settings = base_settings.model_copy(
        update={
            "mqtt_client_id": f"rid-e2e-{run}",
            "mqtt_obs_topic": f"rid/{topic_version}/+/obs",
            "mqtt_info_topic": f"rid/{topic_version}/+/info",
        }
    )
    topic = f"rid/{topic_version}/{station}/obs"

    identified, ghost = _build_observations(run, station)
    print(
        f"[1/6] decoded official frames: identified={identified.drone_id} "
        f"ghost={ghost.drone_id}"
    )

    received: list[str] = []
    errors: list[str] = []
    persisted = [identified, ghost]

    async def on_persisted(obs) -> None:
        received.append(obs.observation_id)

    subscriber = MqttSubscriber(settings)
    subscriber.alerts = AlertEngine(max_alt_m=None, unidentified=True)
    set_subscriber_bus_hook(subscriber, ObservationBus(), AlertRing())
    publish_persisted = subscriber.on_persisted

    async def collect_and_publish(obs) -> None:
        await on_persisted(obs)
        if publish_persisted is not None:
            await publish_persisted(obs)

    subscriber.on_persisted = collect_and_publish

    async with aiomqtt.Client(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        identifier=f"rid-e2e-pub-{run}",
    ) as publisher:
        print(f"[2/6] connected to MQTT {settings.mqtt_host}:{settings.mqtt_port}")

        async def run_subscriber() -> None:
            try:
                await subscriber.start()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"subscriber: {exc}")

        subscriber_task = asyncio.create_task(run_subscriber())
        await asyncio.sleep(1.5)

        for obs in persisted:
            await publisher.publish(topic, obs.to_mqtt_payload(), qos=1)
            print(
                f"[3/6] published {obs.observation_id[:16]}… "
                f"protocol={obs.protocol.value}"
            )

        for _ in range(40):
            await asyncio.sleep(0.25)
            if len(received) >= len(persisted):
                break

        subscriber_task.cancel()
        try:
            await subscriber_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    print(f"[4/6] subscriber persisted {len(received)}/{len(persisted)}")
    if errors:
        for error in errors:
            print(f"      FAIL: {error}")
    if len(received) < len(persisted):
        print("FAIL: messages did not reach the database")
        return 1

    import httpx
    from rid_server.app import create_app

    app = create_app()
    app.state.bus = None
    app.state.subscriber = subscriber
    transport = httpx.ASGITransport(app=app)
    headers: dict[str, str] = {}
    if base_settings.api_token is not None:
        headers["Authorization"] = (
            f"Bearer {base_settings.api_token.get_secret_value()}"
        )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers=headers,
    ) as client:
        response = await client.get("/observations", params={"limit": 50})
        response.raise_for_status()
        rows = [
            row
            for row in response.json()
            if row["observation_id"] in received and row["station_id"] == station
        ]
        ghost_ids = await client.get("/drones/unidentified")
        alert_response = await client.get("/alerts", params={"limit": 200})
        alert_response.raise_for_status()
        run_alerts = [
            alert
            for alert in alert_response.json()
            if alert["drone_id"] == ghost.drone_id
        ]
        acknowledged = None
        retried = None
        resolved = None
        reopened = None
        final_resolution = None
        alert_events = None
        if run_alerts:
            alert = run_alerts[0]
            ack_response = await client.post(
                f"/alerts/{alert['alert_id']}/ack",
                json={
                    "acknowledged_by": "e2e-operator",
                    "note": "end-to-end acknowledgement",
                },
            )
            ack_response.raise_for_status()
            acknowledged = ack_response.json()
            retry_response = await client.post(
                f"/alerts/{alert['alert_id']}/ack",
                json={"acknowledged_by": "retry-operator"},
            )
            retry_response.raise_for_status()
            retried = retry_response.json()
            resolve_response = await client.post(
                f"/alerts/{alert['alert_id']}/resolve",
                json={
                    "resolved_by": "e2e-operator",
                    "note": "end-to-end resolution",
                },
            )
            resolve_response.raise_for_status()
            resolved = resolve_response.json()
            resolve_retry_response = await client.post(
                f"/alerts/{alert['alert_id']}/resolve",
                json={"resolved_by": "retry-operator"},
            )
            resolve_retry_response.raise_for_status()
            reopen_response = await client.post(
                f"/alerts/{alert['alert_id']}/reopen",
                json={
                    "reopened_by": "e2e-operator",
                    "note": "end-to-end reopen",
                },
            )
            reopen_response.raise_for_status()
            reopened = reopen_response.json()
            final_response = await client.post(
                f"/alerts/{alert['alert_id']}/resolve",
                json={
                    "resolved_by": "e2e-operator",
                    "note": "end-to-end final resolution",
                },
            )
            final_response.raise_for_status()
            final_resolution = final_response.json()
            events_response = await client.get(
                f"/alerts/{alert['alert_id']}/events"
            )
            events_response.raise_for_status()
            alert_events = events_response.json()

    print(f"[5/6] HTTP returned {len(rows)}/{len(persisted)} of our rows")
    for row in rows:
        print(
            f"      {row['observation_id'][:16]}… drone={row['drone_id']} "
            f"op={row['operator_id']} protocol={row['protocol']} "
            f"rssi={row.get('rssi_dbm')} speed={row['position']['speed_mps']}"
        )

    ok = True
    if len(rows) != len(persisted):
        print("FAIL: HTTP did not return both rows")
        ok = False
    if any(row.get("rssi_dbm") is None for row in rows):
        print("FAIL: RSSI lost between ingest and HTTP")
        ok = False
    if any(not row["position"]["speed_mps"] for row in rows):
        print("FAIL: speed_mps lost between ingest and HTTP")
        ok = False
    if ghost.drone_id not in ghost_ids.json():
        print(f"FAIL: ghost drone {ghost.drone_id} not listed as unidentified")
        ok = False
    by_id = {row["observation_id"]: row for row in rows}
    if by_id.get(identified.observation_id, {}).get("protocol") != "wifi_beacon":
        print("FAIL: official Wi-Fi Beacon was not labelled wifi_beacon")
        ok = False
    identified_row = by_id.get(identified.observation_id, {})
    operator_location = identified_row.get("operator_location") or {}
    if operator_location.get("lat") is None:
        print("FAIL: operator location was lost between ingest and HTTP")
        ok = False
    if not run_alerts:
        print("FAIL: unidentified alert was not persisted")
        ok = False
    else:
        alert = run_alerts[0]
        if (
            acknowledged is None
            or retried is None
            or resolved is None
            or reopened is None
            or final_resolution is None
            or alert_events is None
        ):
            print("FAIL: acknowledgement requests did not complete")
            ok = False
        else:
            print(
                f"[6/6] alert {alert['alert_id']} lifecycle "
                f"ack -> resolve -> reopen -> resolve verified"
            )
            if acknowledged.get("acknowledged_by") != "e2e-operator":
                print("FAIL: acknowledgement actor was not persisted")
                ok = False
            if retried.get("acknowledged_by") != "e2e-operator":
                print("FAIL: retried acknowledgement overwrote the audit trail")
                ok = False
            if resolved.get("status") != "resolved":
                print("FAIL: alert resolution was not persisted")
                ok = False
            if reopened.get("status") != "open":
                print("FAIL: alert reopen did not restore active state")
                ok = False
            if reopened.get("reopen_count") != 1:
                print("FAIL: alert reopen count was not persisted")
                ok = False
            if final_resolution.get("status") != "resolved":
                print("FAIL: alert could not be resolved after reopen")
                ok = False
            actions = [event.get("action") for event in alert_events]
            for action in ("acknowledged", "resolved", "reopened"):
                if action not in actions:
                    print(f"FAIL: audit trail is missing {action}")
                    ok = False

    print(
        "PASS: official radio bytes -> MQTT -> DB -> HTTP -> alert lifecycle verified"
        if ok
        else "FAILED"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
