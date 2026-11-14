"""Cross-package wire contract: edge → server.

This is the test that was missing.  Both packages had green suites
because each validated its own hand-written fixture — the edge test
built an `RidObservation` and asserted on it, the server test built a
dict that happened to match `DroneObservation`.  Neither ever fed one
package's real output into the other's parser, so a field-placement
mismatch (`speed_mps` at top level vs. inside `position`) shipped
undetected and *every* message the edge emitted was rejected by the
server as invalid.

Run from the repo root so both packages are importable:

    pytest tests/test_wire_contract.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for pkg in ("edge/src", "server/src"):
    p = str(ROOT / pkg)
    if p not in sys.path:
        sys.path.insert(0, p)

from rid_edge.models import OperatorLocation as EdgeOperatorLocation  # noqa: E402
from rid_edge.models import Position as EdgePosition  # noqa: E402
from rid_edge.models import Protocol as EdgeProtocol  # noqa: E402
from rid_edge.models import RidObservation  # noqa: E402
from rid_server.models import DroneObservation  # noqa: E402


def _edge_observation(**overrides) -> RidObservation:
    base = dict(
        observation_id="01H8XYZ0000000000000000AB",
        drone_id="1581F5BKD225800A3C4F",
        operator_id="OP-CN-88421",
        protocol=EdgeProtocol.ASTM_F3411,
        station_id="SZ-Nanshan-01",
        rssi_dbm=-65,
        position=EdgePosition(
            lat=22.5405,
            lon=113.9345,
            alt_m=120.0,
            accuracy_h_m=5.0,
            speed_mps=15.5,
            heading_deg=90.0,
        ),
        drone_timestamp=datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc),
        received_at=datetime(2026, 11, 14, 10, 0, 1, tzinfo=timezone.utc),
        self_id="survey mission",
        operator_location=EdgeOperatorLocation(
            lat=22.5410,
            lon=113.9340,
            alt_m=8.0,
        ),
    )
    base.update(overrides)
    return RidObservation(**base)


def _edge_payload(**overrides) -> dict:
    """What actually goes on the wire."""
    return json.loads(_edge_observation(**overrides).to_mqtt_payload())


def test_edge_payload_is_accepted_by_server():
    """The one assertion whose absence let the system ship broken."""
    payload = _edge_payload()
    obs = DroneObservation.model_validate(payload)

    assert obs.observation_id == payload["observation_id"]
    assert obs.drone_id == payload["drone_id"]
    assert obs.station_id == payload["station_id"]
    # Geometry survives the hop, including the fields that used to be
    # at the top level and got dropped.
    assert obs.position.lat == pytest.approx(22.5405)
    assert obs.position.lon == pytest.approx(113.9345)
    assert obs.position.alt_m == pytest.approx(120.0)
    assert obs.position.speed_mps == pytest.approx(15.5)
    assert obs.position.heading_deg == pytest.approx(90.0)
    assert obs.operator_location is not None
    assert obs.operator_location.lat == pytest.approx(22.5410)
    assert obs.operator_location.lon == pytest.approx(113.9340)
    assert obs.operator_location.alt_m == pytest.approx(8.0)


def test_rssi_survives_the_hop():
    """RSSI is captured by the edge and must not be silently discarded."""
    obs = DroneObservation.model_validate(_edge_payload())
    assert obs.rssi_dbm == -65


def test_every_position_field_is_wire_compatible():
    """Field-by-field check, so a future rename fails here and not in prod."""
    payload = _edge_payload()
    edge_pos = payload["position"]
    server_fields = set(DroneObservation.model_fields["position"].annotation.model_fields)

    missing = server_fields - set(edge_pos)
    assert not missing, f"server requires position fields the edge never sends: {sorted(missing)}"


@pytest.mark.parametrize(
    "protocol",
    [
        EdgeProtocol.ASTM_F3411,
        EdgeProtocol.WIFI_NAN,
        EdgeProtocol.WIFI_BEACON,
    ],
)
def test_both_protocols_are_valid_server_literals(protocol):
    """Server types `protocol` as a Literal; edge uses an enum.

    Both must serialise to a string the server's Literal accepts, or
    the message is rejected at parse time.
    """
    obs = DroneObservation.model_validate(_edge_payload(protocol=protocol))
    assert obs.protocol == protocol.value


def test_round_trip_preserves_geometry():
    """edge → wire → server → back should not shift the position."""
    payload = _edge_payload()
    server_obs = DroneObservation.model_validate(payload)
    assert server_obs.position.alt_m == pytest.approx(payload["position"]["alt_m"])


def test_optional_fields_absent_are_tolerated():
    """A minimal obs (no operator, no self-id) must still validate.

    The server's model marks these optional; the edge's does too, but
    the two defaults could drift.  This pins the wire behaviour.
    """
    payload = _edge_payload(operator_id=None, self_id=None)
    obs = DroneObservation.model_validate(payload)
    assert obs.operator_id is None
    # Either representation means "the drone broadcast no self-ID"; what
    # matters is that None doesn't blow up validation on the way in.
    assert not obs.self_id
