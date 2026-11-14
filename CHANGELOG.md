# Changelog

## 0.2.0 - 2026-11-15

### Added

- Standards-compliant Open Drone ID Message Pack parsing and official golden
  vectors from the upstream C encoder.
- Wi-Fi Beacon, Wi-Fi NAN and BLE end-to-end receiver paths.
- Cross-package tests from official radio bytes through MQTT to the server
  model and HTTP API.
- Reliable SQLite outbox semantics with QoS acknowledgements and timeout
  re-queueing.
- Station heartbeat ingest, readiness/liveness endpoints and Prometheus text
  metrics.
- Docker entrypoint that applies Alembic migrations before startup.
- Release workflow for `v*` tags and Docker build validation in CI.
- Deployment and testing documentation.

### Fixed

- Corrected 25-byte ODID layouts, Message Pack header handling, Message Pack
  length validation and protocol enum interpretation.
- Preserved operator ID, Self-ID, operator location, RSSI and position accuracy
  through the full chain.
- Labelled Wi-Fi Beacon observations as `wifi_beacon` instead of `wifi_nan`.
- Prevented unbounded history requests from the frontend live stream.
- Replaced untrusted HTML popup construction with safe DOM APIs.
- Made alert IDs deterministic across process restarts.
- Preserved compatibility with historical rows containing full operator
  positions.

### Changed

- Upgraded MapLibre GL to 6.18, Vite to 8 and the React Vite plugin to 6.
- Split MapLibre into a separate production chunk.
- Removed the unused Redis service from the reference Compose stack.
- Made `make clean` non-destructive; database deletion is now `make reset`.

## 0.1.0

Initial prototype release.
