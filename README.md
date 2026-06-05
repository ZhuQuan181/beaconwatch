# RID Detection System

End-to-end Drone Remote ID (RID) detection system per ASTM F3411-22 / EN 4709-002.

## Components

- **edge/** — Edge receiver on industrial PC (BLE 5.x + Wi-Fi NaN scanning, MQTT publish)
- **server/** — Backend aggregation (FastAPI + TimescaleDB + WebSocket live feed)
- **frontend/** — Real-time dashboard (Vue 3 + Leaflet + dark ATC aesthetic)
- **mosquitto/** — MQTT broker config

## Quick Start

```bash
make up          # Start full stack (Mosquitto + TimescaleDB + Server + Frontend)
make test        # Unit tests
make e2e         # End-to-end test with simulated drone
make down        # Stop everything
```

Open http://localhost:5173 for the dashboard.

## Architecture

See `docs/plans/2026-06-06-drone-rid-detection-system.md` for full design.

## License

MIT
