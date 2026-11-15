# Changelog

## 0.4.0 - 2026-11-15

### Added

- Durable alert ledger backed by a new `alerts` table and Alembic migration;
  alert history now survives server restarts.
- `POST /alerts/{alert_id}/ack` records the first operator acknowledgement,
  timestamp, actor and optional note as an audit trail.
- Deterministic alert recurrence handling: the same alert ID increments
  `trigger_count`, updates the latest trigger time and preserves the original
  first-seen and acknowledgement metadata.
- Acknowledged-state WebSocket broadcasts keep every open dashboard in sync.
- Dashboard acknowledgement controls, operator-name persistence and optional
  handling notes; the static demo implements the same REST contract.
- Recent alerts are restored into the in-process operational ring at startup.
- Alert persistence failures are logged and degraded to live-only delivery;
  they do not block observation ingest or suppress the WebSocket alert.
- The static demo build now always enables its mock transport instead of
  relying on an external environment variable.

### Changed

- `GET /alerts` now reads the durable ledger newest-trigger first instead of
  returning only alerts produced by the current process.
- MapLibre's worker is emitted as an explicit Vite asset, so production and
  Pages builds no longer request a worker file that only exists in the npm
  package.
- Dashboard layout resets the document box model and confines scrolling to the
  alert list instead of nesting page/sidebar scroll containers.
- Repository/package metadata advanced to 0.4.0.
- Documentation now reflects the implemented alert workflow and removes alert
  persistence from the production-gap checklist.

## 0.3.0 - 2026-11-15

### Added

- Optional Bearer-token authentication for REST and WebSocket traffic via
  `RID_API_TOKEN`; health checks and metrics remain unauthenticated for probes.
- Request IDs, security headers, gzip compression, optional CORS origins and
  HTTP request/duration/auth-failure Prometheus metrics.
- Stable keyset pagination for observation history through `X-Next-Cursor`,
  `cursor=` compatibility and a dedicated `/observations/page` response.
- Database indexes for recent-drone, station-window, operator-window and
  cursor-ordered observation queries.
- WebSocket heartbeat/pong frames, malformed-client handling and browser-side
  stale-connection watchdog.
- Supervised edge receivers with bounded exponential restart and queue-drop
  counters.
- Verified MQTT TLS contexts on the server, including optional private CA and
  client-certificate support.
- Browser request deadlines, retry/backoff, request cancellation and
  visibility-aware polling.

### Fixed

- Duplicate MQTT deliveries no longer increase drone observation counts, emit
  duplicate WebSocket frames or re-run alert rules.
- Out-of-order fixes no longer move a drone's last-seen position backwards.
- Duplicate REST seed/live rows are deduplicated in the dashboard.
- WebSocket clients no longer schedule reconnect timers after unmount, and
  half-open connections no longer stay "connected" indefinitely.
- Unidentified-drone queries no longer scan all history by default.

### Changed

- API history ordering is deterministic on `(received_at, observation_id)`.
- Frontend polling stops while the tab is hidden and refreshes immediately on
  return.
- Repository/package metadata advanced to 0.3.0.

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
