"""Official OpenDroneID frame -> edge receiver -> MQTT -> server contract.

Unlike ``test_wire_contract.py`` (which starts from a hand-built edge
model), this test starts with bytes emitted by the upstream C encoder
and exercises every boundary that has broken before:

802.11 frame -> receiver extraction -> ODID decoder -> pipeline ->
MQTT JSON -> server validation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scapy.layers.dot11 import RadioTap

ROOT = Path(__file__).resolve().parent.parent
for path in (
    ROOT / "edge" / "src",
    ROOT / "server" / "src",
    ROOT / "edge" / "tests",
):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

from fixtures.odid_vectors import WIFI_BEACON_FRAME  # noqa: E402
from rid_edge.models import Protocol as EdgeProtocol  # noqa: E402
from rid_edge.pipeline import PipelineManager  # noqa: E402
from rid_edge.receivers.wifi_nan import WifiReceiver  # noqa: E402
from rid_server.models import DroneObservation  # noqa: E402


def test_official_wifi_beacon_reaches_server_model() -> None:
    receiver = WifiReceiver()
    packet = RadioTap(
        bytes(RadioTap(present=0x20, dBm_AntSignal=-61)) + WIFI_BEACON_FRAME
    )

    receiver._on_pkt(packet)
    assert receiver._queue.qsize() == 1
    raw = receiver._queue.get_nowait()

    observations = PipelineManager(station_id="SZ-Nanshan-01").process(raw)
    assert len(observations) == 1
    edge_obs = observations[0]
    wire = json.loads(edge_obs.to_mqtt_payload())
    server_obs = DroneObservation.model_validate(wire)

    assert server_obs.drone_id == "1581F5BKD225800A3C4F"
    assert server_obs.operator_id == "OP-CN-88421"
    assert server_obs.self_id == "survey mission"
    assert server_obs.station_id == "SZ-Nanshan-01"
    assert server_obs.protocol == EdgeProtocol.WIFI_BEACON.value
    assert server_obs.rssi_dbm == -61

    assert server_obs.position.lat == pytest.approx(45.539309, abs=1e-6)
    assert server_obs.position.lon == pytest.approx(-122.966389, abs=1e-6)
    assert server_obs.position.alt_m == pytest.approx(110.0)
    assert server_obs.position.speed_mps == pytest.approx(5.5)
    assert server_obs.position.heading_deg == pytest.approx(216.0)

    assert server_obs.operator_location is not None
    assert server_obs.operator_location.lat == pytest.approx(45.539319, abs=1e-6)
    assert server_obs.operator_location.lon == pytest.approx(-122.966399, abs=1e-6)
