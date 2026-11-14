"""Parser tests driven by official OpenDroneID encoder output."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from rid_edge.models import Protocol
from rid_edge.odid_parser import (
    basic_id,
    location,
    message_pack,
    operator_id,
    self_id,
    system,
)
from rid_edge.odid_parser.constants import SelfIDType
from rid_edge.odid_parser.frame_parser import (
    extract_wifi_odid_payloads,
    parse_ble_advertisement,
    parse_wifi_frame,
)
from rid_edge.pipeline import PipelineManager, observations_from_frame
from rid_edge.receivers.base import Bearer, RawFrame
from rid_edge.receivers.ble import BLEReceiver
from rid_edge.receivers.wifi_nan import WifiReceiver
from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Elt, RadioTap

from .fixtures.odid_vectors import (
    BASIC_ID,
    BLE_SERVICE_DATA,
    LOCATION,
    MESSAGE_PACK,
    OPERATOR_ID,
    SELF_ID,
    SYSTEM,
    WIFI_BEACON_FRAME,
    WIFI_NAN_FRAME,
)


def _raw(payload: bytes, bearer: Bearer = Bearer.WIFI_NAN) -> RawFrame:
    return RawFrame(
        bearer=bearer,
        source_mac="aa:bb:cc:dd:ee:ff",
        rssi_dbm=-55,
        ts_monotonic=time.monotonic(),
        payload=payload,
        channel=6,
    )


def test_basic_id_official_vector() -> None:
    msg = basic_id.decode(BASIC_ID)
    assert msg is not None
    assert msg.uas_id == "1581F5BKD225800A3C4F"
    assert msg.id_type == basic_id.IDType.SERIAL_NUMBER
    assert msg.ua_type == basic_id.UAType.HELICOPTER_OR_MULTIROTOR
    assert msg.protocol_version == 2


def test_location_official_vector() -> None:
    msg = location.decode(LOCATION)
    assert msg is not None
    assert msg.status == location.LSStatus.AIRBORNE
    assert msg.direction_deg == pytest.approx(216.0)
    assert msg.speed_horizontal_mps == pytest.approx(5.5)
    assert msg.speed_vertical_mps == pytest.approx(5.5)
    assert msg.latitude_deg == pytest.approx(45.539309, abs=1e-6)
    assert msg.longitude_deg == pytest.approx(-122.966389, abs=1e-6)
    assert msg.altitude_pressure_m == pytest.approx(100.0)
    assert msg.altitude_geodetic_m == pytest.approx(110.0)
    assert msg.height_above_ground_m == pytest.approx(80.0)
    assert msg.height_type == location.HeightReference.OVER_GROUND
    assert msg.horiz_accuracy_m == pytest.approx(3.0)
    assert msg.vert_accuracy_m == pytest.approx(3.0)
    assert msg.speed_accuracy_mps == pytest.approx(1.0)
    assert msg.timestamp_s == pytest.approx(360.5)
    assert msg.timestamp_accuracy_s == pytest.approx(0.2)


def test_self_id_official_vector() -> None:
    msg = self_id.decode(SELF_ID)
    assert msg is not None
    assert msg.description_type == SelfIDType.TEXT
    assert msg.description == "survey mission"


def test_system_official_vector() -> None:
    msg = system.decode(SYSTEM)
    assert msg is not None
    assert msg.operator_location_type == system.OperatorLocationType.FIXED
    assert msg.classification_type == 1
    assert msg.operator_lat == pytest.approx(45.539319, abs=1e-6)
    assert msg.operator_lon == pytest.approx(-122.966399, abs=1e-6)
    assert msg.operator_alt_m == pytest.approx(20.5)
    assert msg.area_count == 1
    assert msg.area_radius_m == pytest.approx(70.0)
    assert msg.area_ceiling_m == pytest.approx(120.0)
    assert msg.area_floor_m == pytest.approx(0.0)
    assert msg.category_eu == 2
    assert msg.class_eu == 4
    assert msg.timestamp_s == 28_000_000


def test_operator_id_official_vector() -> None:
    msg = operator_id.decode(OPERATOR_ID)
    assert msg is not None
    assert msg.operator_id == "OP-CN-88421"
    assert msg.operator_id_type == 0


def test_message_pack_official_vector() -> None:
    parsed = message_pack.decode(MESSAGE_PACK)
    assert parsed is not None
    assert len(parsed.basic_id) == 1
    assert len(parsed.locations) == 1
    assert len(parsed.self_ids) == 1
    assert len(parsed.systems) == 1
    assert len(parsed.operator_ids) == 1
    assert parsed.basic_id[0].uas_id == "1581F5BKD225800A3C4F"
    assert parsed.operator_ids[0].operator_id == "OP-CN-88421"


def test_message_pack_encoder_matches_official_header() -> None:
    builder = message_pack.MessagePackParser()
    builder.add(message_pack.SubMessage(message_pack.SubMessageType.BASIC_ID, BASIC_ID))
    builder.add(message_pack.SubMessage(message_pack.SubMessageType.LOCATION, LOCATION))
    encoded = bytes(builder.build())
    assert encoded[:3] == b"\xf2\x19\x02"
    assert len(encoded) == 3 + 2 * 25


def test_wifi_beacon_official_frame() -> None:
    pkt = Dot11(WIFI_BEACON_FRAME)
    parsed = parse_wifi_frame(pkt)
    assert parsed is not None
    assert parsed.basic_id[0].uas_id == "1581F5BKD225800A3C4F"
    assert parsed.locations[0].latitude_deg == pytest.approx(45.539309, abs=1e-6)
    assert parsed.operator_ids[0].operator_id == "OP-CN-88421"


def test_wifi_nan_official_frame() -> None:
    pkt = Dot11(WIFI_NAN_FRAME)
    payloads = extract_wifi_odid_payloads(pkt)
    assert payloads == [MESSAGE_PACK]
    parsed = parse_wifi_frame(pkt)
    assert parsed is not None
    assert parsed.basic_id[0].uas_id == "1581F5BKD225800A3C4F"
    assert parsed.self_ids[0].description == "survey mission"


def test_wifi_receiver_labels_beacon_transport() -> None:
    pkt = RadioTap(
        bytes(RadioTap(present=0x20, dBm_AntSignal=-61)) + WIFI_BEACON_FRAME
    )
    receiver = WifiReceiver()
    receiver._on_pkt(pkt)
    frame = receiver._queue.get_nowait()
    assert frame.bearer == Bearer.WIFI_BEACON
    obs = PipelineManager(station_id="STATION-BEACON").process(frame)[0]
    assert obs.protocol == Protocol.WIFI_BEACON


def test_ble_advertisement_official_service_data() -> None:
    service_data = b"\xfa\xff" + BLE_SERVICE_DATA
    ad = bytes([1 + len(service_data), 0x16]) + service_data
    parsed = parse_ble_advertisement(ad)
    assert parsed is not None
    assert parsed.basic_id[0].uas_id == "1581F5BKD225800A3C4F"
    assert parsed.operator_ids[0].operator_id == "OP-CN-88421"


def test_observations_include_operator_and_self_id() -> None:
    frame = _raw(MESSAGE_PACK)
    manager = PipelineManager(station_id="STATION-TEST-01")
    observations = manager.process(frame)
    assert len(observations) == 1
    obs = observations[0]
    assert obs.drone_id == "1581F5BKD225800A3C4F"
    assert obs.operator_id == "OP-CN-88421"
    assert obs.self_id == "survey mission"
    assert obs.operator_location is not None
    assert obs.operator_location.lat == pytest.approx(45.539319, abs=1e-6)
    assert obs.position.lat == pytest.approx(45.539309, abs=1e-6)
    assert obs.position.lon == pytest.approx(-122.966389, abs=1e-6)
    assert obs.position.alt_m == pytest.approx(110.0)
    assert obs.station_id == "STATION-TEST-01"
    assert obs.protocol == Protocol.WIFI_NAN


def test_empty_payload_yields_nothing() -> None:
    assert observations_from_frame(_raw(b"")) == []


def test_basic_id_only_yields_partial_observation() -> None:
    frame = _raw(BASIC_ID)
    observations = PipelineManager(station_id="STATION-PARTIAL").process(frame)
    assert len(observations) == 1
    assert observations[0].drone_id == "1581F5BKD225800A3C4F"
    assert observations[0].position.lat == 0.0


def test_received_at_is_utc() -> None:
    obs = PipelineManager(station_id="STATION-UTC").process(_raw(MESSAGE_PACK))[0]
    assert obs.received_at.tzinfo is not None
    assert obs.received_at.utcoffset() == timezone.utc.utcoffset(obs.received_at)
    assert isinstance(obs.drone_timestamp, datetime)


def test_wifi_receiver_extracts_vendor_ie_and_string_mac() -> None:
    info = b"\xfa\x0b\xbc\x0d\x01" + MESSAGE_PACK
    pkt = (
        RadioTap(present=0x20, dBm_AntSignal=-61)
        / Dot11(
            type=0,
            subtype=8,
            addr1="ff:ff:ff:ff:ff:ff",
            addr2="12:34:56:21:43:65",
            addr3="12:34:56:21:43:65",
        )
        / Dot11Beacon()
        / Dot11Elt(ID=0xDD, info=info)
    )
    assert isinstance(pkt[Dot11].addr2, str)
    receiver = WifiReceiver()
    receiver._on_pkt(pkt)
    assert receiver._queue.qsize() == 1
    frame = receiver._queue.get_nowait()
    assert frame.source_mac == "12:34:56:21:43:65"
    assert frame.rssi_dbm == -61
    assert frame.payload == MESSAGE_PACK


def test_ble_receiver_does_not_strip_payload_header() -> None:
    adv = SimpleNamespace(
        rssi=-57,
        service_data={"0000fffa-0000-1000-8000-00805f9b34fb": BLE_SERVICE_DATA},
    )
    payload = BLEReceiver._extract_odid(adv)
    assert payload == MESSAGE_PACK
    assert payload[0] == 0xF2


def test_ble_receiver_accepts_uuid_prefixed_service_data() -> None:
    adv = SimpleNamespace(
        rssi=-57,
        service_data={"FFFA": b"\xfa\xff" + BLE_SERVICE_DATA},
    )
    assert BLEReceiver._extract_odid(adv) == MESSAGE_PACK


async def test_ble_receiver_enqueues_complete_payload() -> None:
    receiver = BLEReceiver()
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    adv = SimpleNamespace(
        rssi=-57,
        service_data={"0000fffa-0000-1000-8000-00805f9b34fb": BLE_SERVICE_DATA},
    )
    receiver._on_ad(device, adv)
    frame = receiver._queue.get_nowait()
    assert frame.payload == MESSAGE_PACK
    assert frame.source_mac == "aa:bb:cc:dd:ee:ff"
