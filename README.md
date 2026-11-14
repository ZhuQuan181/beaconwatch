# BeaconWatch

[![CI](https://github.com/ZhuQuan181/beaconwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/ZhuQuan181/beaconwatch/actions/workflows/ci.yml)
[![Pages](https://github.com/ZhuQuan181/beaconwatch/actions/workflows/pages.yml/badge.svg)](https://github.com/ZhuQuan181/beaconwatch/actions/workflows/pages.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Live demo](https://img.shields.io/badge/demo-live-2ea44f)](https://ZhuQuan181.github.io/beaconwatch/)

Real-time Remote ID (RID) monitoring for the airspace. BeaconWatch listens for
ASTM F3411-22 / EN 4709-002 broadcasts over BLE 5.x and Wi-Fi NaN, aggregates
them on a server, and visualises live drone traffic on a dark ATC-style
dashboard.

## Live Demo

https://ZhuQuan181.github.io/beaconwatch/

The demo is a static build with simulated drone traffic, so it runs without a
backend. For the real system, run the stack locally with `make up`.

## Components

- **`edge/`** — edge receiver on an industrial PC (BLE 5.x + Wi-Fi NaN scanning, MQTT 5 publish)
- **`server/`** — FastAPI + TimescaleDB aggregation, REST API, WebSocket live feed
- **`frontend/`** — React 18 + TypeScript + MapLibre GL real-time dashboard
- **`deploy/`** — broker and deployment configuration

## Quick Start

Requires Docker and Node 20+.

```bash
make up          # Start the stack (TimescaleDB + EMQX + Redis + server)
make test        # Unit tests (edge + server) + wire contract
make e2e         # End-to-end: edge -> MQTT -> server -> DB -> HTTP
make down        # Stop everything
```

The API is at http://localhost:8000/docs. For the dashboard:

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

## Architecture

See `docs/01-architecture.md` and
`docs/plans/2026-06-06_011223-drone-rid-detection-system.md`.

## License

MIT
