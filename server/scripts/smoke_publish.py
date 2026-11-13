"""Smoke test publisher — pushes a few fake observations to the local broker.

Schema must match ``server/src/rid_server/models.py::DroneObservation``.
Usage:  python scripts/smoke_publish.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from paho.mqtt import client as mqtt

BROKER = "localhost"
PORT = 1883
TOPIC = "rid/v1/station-east-01/obs"


def make_obs(i: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "observation_id": f"e2e-smoke-{i+1}",
        "drone_id": "drn-e2e",
        "operator_id": "op-e2e",
        "station_id": "station-east-01",
        "protocol": "wifi_beacon",
        "self_id": "1F3A4B2C5D6E7890ABCDEF12",
        "drone_timestamp": now.isoformat().replace("+00:00", "Z"),
        "received_at": now.isoformat().replace("+00:00", "Z"),
        "position": {
            "lat": 40.7128 + i * 0.001,
            "lon": -74.006,
            "alt_m": 120.0,
            "accuracy_h_m": 5.0,
            "accuracy_v_m": 8.0,
            "speed_mps": 15.0,
            "heading_deg": 90.0,
        },
        "operator_location": {
            "lat": 40.7000, "lon": -74.0000, "alt_m": 10.0,
            "accuracy_h_m": 20.0, "accuracy_v_m": 30.0,
            "speed_mps": 0.0, "heading_deg": 0.0,
        },
    }


def main() -> None:
    c = mqtt.Client(
        client_id="smoke-publisher",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    c.connect(BROKER, PORT, keepalive=10)
    c.loop_start()
    for i in range(3):
        obs = make_obs(i)
        c.publish(TOPIC, json.dumps(obs), qos=1)
        print(f"published {obs['observation_id']}")
        time.sleep(0.5)
    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
