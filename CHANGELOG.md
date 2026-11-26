# Changelog

## 1.2.0 - 2026-11-27

### Added

- Migrations `0010_perf_indexes`: a partial index for
  `/drones/unidentified` and a protocol/time index for analytics.
- Configurable database pool timeout, recycling, LIFO reuse and a
  PostgreSQL statement timeout (`RID_DB_POOL_*`,
  `RID_DB_STATEMENT_TIMEOUT_MS`).
- Unit tests proving the min/max pool knobs map to the intended
  persistent/overflow connection counts.

### Changed

- `/drones/unidentified` now returns a deterministic drone-id order.
- Default connection pool is 2 persistent plus 8 overflow connections,
  matching the documented `RID_DB_POOL_MIN=2`, `RID_DB_POOL_MAX=10`
  contract.

### Fixed

- The engine previously passed `RID_DB_POOL_MAX` as `pool_size` and
  `RID_DB_POOL_MIN` as overflow, allowing more connections than the
  configured maximum.

## 1.1.0 - 2026-11-27

### Added

- MQTT regression tests for non-object JSON payloads, invalid UTF-8 and
  empty geofence caches.
- FIFO dedup expiration tests that pin insertion order and refresh
  behavior.

### Changed

- Server package version is now the single source of truth for the
  OpenAPI version and `rid_build_info`.
- All edge, server and frontend package metadata advanced to 1.1.0.

### Fixed

- Malformed MQTT observation/info payloads are isolated and counted as
  invalid instead of raising and stalling the subscriber.
- Geofence reads no longer skip the database when the cached zone list
  is legitimately empty.
- Dedup FIFO expiration now preserves insertion order, so an expired
  key cannot evict a live newer one.
- Lifespan startup uses an async exit stack, guaranteeing cleanup when
  a later component fails to start and when shutdown raises.
- The unidentified-drone route test uses a relative time anchor instead
  of aging out of the default seven-day lookback.

## 1.0.0 - 2026-11-16

### Added

- File-backed configuration for database, Redis, MQTT, legacy API-token
  and webhook secrets through `RID_*_FILE`. Secret files are size-bounded,
  fail closed when missing/empty, and are resolved before cross-field
  validation.
- Proper labeled HTTP duration histograms, edge-receive-to-commit ingest
  latency, notification delivery-lag histograms, WebSocket connection
  counters, database/build information and a notification-enabled gauge.
- A repeatable HTTP/MQTT/WebSocket load generator that reports throughput,
  p50/p95/p99, error rates, final durable-ingest ratio, persisted-based and
  end-to-end WebSocket delivery ratios, server counter deltas and host
  information for an auditable local baseline. The drain phase polls the
  server counter instead of assuming that a successful MQTT publish means
  the row has been committed.
- A destructive but project-guarded resilience drill that stops and
  restarts Redis, EMQX, PostgreSQL and the API on a disposable Compose
  stack, then verifies readiness, local fan-out, reconnect and ingest.
- A production reference topology with an Nginx TLS/WebSocket proxy,
  internal-only database/MQTT/Redis paths, one-shot migrations, multiple
  API replicas, Docker secrets and runtime viewer-key injection.
- Prometheus alert rules for availability, latency, MQTT/Redis failures,
  WebSocket drops, notification backlog/dead letters and retention errors.
- A production operations runbook covering SLOs, capacity testing, fault
  drills, backup/restore, upgrade/rollback, key rotation and incident
  response.

### Changed

- The frontend can load its API token from runtime `config.js`, so production
  deployments no longer need to bake a viewer key into the image or frontend
  build. `VITE_API_TOKEN` remains supported for local/single-tenant builds.
- The HTTP duration metric is now a standard Prometheus histogram with
  `method` and `route` labels. The former unlabeled duration-sum series was
  removed; update dashboards to use the histogram.
- The observation bus derives its subscriber count from the actual queue
  list, avoiding a cached counter drifting from test/direct registrations.
- Repository/package metadata advanced to 1.0.0.

### Fixed

- Database credentials and database names are percent-encoded before being
  assembled into the asyncpg URL, so strong generated passwords containing
  `@`, `:`, `/`, `#` or spaces no longer corrupt the DSN.
- Notification delivery latency is recorded only after the outbox row is
  successfully acknowledged as delivered.
- Station heartbeat refreshes on the observation path now share the
  observation transaction instead of committing separately, removing one
  durable WAL flush per ingested message.

## 0.9.0 - 2026-11-16

### Added

- Optional Redis Pub/Sub live bus for multi-replica WebSocket fan-out. Every
  API replica receives observations and alert transitions from one shared
  channel, and Redis is included in the reference Compose topology.
- Redis connection health, reconnect, publish-failure and remote-frame
  counters, plus local-fan-out fallback while the shared bus is unavailable.
- Optional MQTT shared subscriptions through `RID_MQTT_SHARED_GROUP`, so
  replicas consume each observation once instead of each ingesting every
  message. MQTT client IDs now always receive a process-unique instance
  suffix, including when Compose supplies an empty `RID_INSTANCE_ID`.
- Durable cross-replica alert deduplication. Replica candidates inside
  `RID_ALERT_DEDUP_SECONDS` are suppressed before count, audit event or
  notification insertion; PostgreSQL advisory locks close the concurrent
  first-insert race.
- Redis-backed readiness semantics: `RID_REDIS_REQUIRED=true` returns 503
  while the bus cannot connect, without putting the process into a crash loop.
- Tests for two-instance Redis fan-out, reconnect after publish failure,
  malformed frames, readiness and metrics, shared subscription topics,
  empty instance IDs and durable alert deduplication.
- A real two-replica E2E (`make e2e-multi`) that starts two uvicorn processes
  against the existing PostgreSQL, EMQX and Redis, verifies exactly-once
  shared MQTT consumption, Redis fan-out to both WebSocket clients, and one
  durable alert/audit event despite concurrent rule evaluation.

### Changed

- Redis Pub/Sub uses no socket read deadline; the listener performs bounded
  idle reads so a healthy quiet subscription is not disconnected every few
  seconds.
- The reference Compose stack starts Redis and exposes no Redis host port.
  Multi-replica deployments should enable `RID_BUS_BACKEND=redis`, set the
  same `RID_MQTT_SHARED_GROUP`, and place the API replicas behind a
  WebSocket-capable load balancer.
- Repository/package metadata advanced to 0.9.0.

### Fixed

- A failed Redis publish now wakes the listener into reconnecting instead of
  leaving it blocked on an old Pub/Sub connection.
- Empty instance IDs can no longer make multiple MQTT clients share the same
  broker identity and repeatedly disconnect each other.
- HTTP readiness and metrics now expose Redis backend state, and the app
  cleanly starts/stops the configured bus during its lifespan.
- The container entrypoint retries Alembic with bounded exponential backoff
  while PostgreSQL finishes recovery after becoming probe-ready, instead of
  relying on a Docker restart to complete a cold start.

## 0.8.0 - 2026-11-16

### Added

- Named API-key identities backed by a hot-reloadable JSON credential file,
  with SHA-256 digests, key IDs, subjects, display names, enable/disable
  flags and duplicate/format validation.
- Viewer, operator and admin RBAC. Read routes require viewer; alert
  disposition requires operator; zone changes and cross-resource audit
  require admin.
- `GET /auth/me`, `GET /audit/events` with filters and keyset pagination,
  and `rid_auth_required`, `rid_auth_principals` and
  `rid_http_authz_failures_total` metrics.
- Per-request security audit rows for alert and zone mutations, written in
  the same transaction as the domain change with actor subject, display name,
  roles, request ID, outcome and resource identifiers.
- Alert events now retain the authenticated subject and request ID alongside
  the human-readable actor.
- Dashboard identity display, role-aware alert controls and static-demo
  `/auth/me` support.
- Alembic `0009` for the audit ledger and alert-event identity columns, plus
  authentication, RBAC, rotation, audit and WebSocket tests.

### Changed

- Authenticated callers cannot spoof `acknowledged_by`, `resolved_by` or
  `reopened_by`; the authenticated display name is authoritative. Those body
  fields are optional when a key is configured and remain usable in open mode.
- `RID_API_TOKEN` is retained as a single administrator compatibility
  credential; `RID_AUTH_USERS_FILE` is the preferred least-privilege setup.
- Repository/package metadata advanced to 0.8.0.

## 0.7.0 - 2026-11-16

### Added

- Stable alert keyset pagination on `(triggered_at, alert_id)`, exposed
  through `X-Next-Cursor` on `GET /alerts` and an explicit
  `GET /alerts/page` response.
- `GET /alerts/stats` with lifecycle, severity, rule and oldest-active
  aggregates under the existing filter set.
- Opt-in, batched retention policies for resolved alerts and terminal
  notification outbox rows. Unresolved alerts and alerts with pending or
  in-flight webhook deliveries are never purged.
- Retention worker metrics for deleted rows, failures, last success and
  worker state.
- Dashboard alert statistics, cursor-based "load older" history and matching
  static-demo endpoints.
- Alert query, hydration and retention tests, plus real E2E checks for the
  page and statistics contracts.

### Changed

- Alert ledger indexes now cover the keyset order, rule/severity windows,
  resolved cleanup and outbox terminal-state cleanup.
- Retention defaults to disabled so an upgrade cannot silently delete audit
  records; operators must set explicit day counts.
- Repository/package metadata advanced to 0.7.0.

## 0.6.0 - 2026-11-16

### Added

- Durable `notification_outbox` table and Alembic `0007` migration for alert
  lifecycle webhooks.
- Atomic enqueueing for triggered, acknowledged, resolved and reopened alert
  transitions, including automatic recurrence reopening.
- Background notification worker with per-alert ordering, cross-alert
  concurrency, an earlier-event ordering barrier, PostgreSQL `SKIP LOCKED`
  claims and expiring recovery leases.
- HMAC-SHA256 request signing, stable `delivery_id` idempotency keys,
  configurable timeouts, bounded jittered exponential backoff, permanent-failure
  classification and dead-lettering after the configured attempt limit.
- Direct webhook egress by default, with explicit
  `RID_NOTIFY_WEBHOOK_TRUST_ENV` opt-in for deployments that require an
  environment-configured HTTP(S) proxy.
- Prometheus metrics for pending/delivering/delivered/dead outbox rows,
  cumulative attempts/failures, worker state and last successful delivery.
- Server tests for transactional enqueue, idempotent lifecycle events, signing,
  timeout/5xx/4xx behavior, retries, dead letters, lease recovery, API wiring,
  metrics and notification failure isolation.

### Changed

- The real official-frame E2E now starts a local HMAC-verifying webhook receiver
  and checks triggered, acknowledged, resolved, reopened and final resolution
  deliveries in order.
- Webhook notifications are disabled unless both
  `RID_NOTIFY_WEBHOOK_URL` and `RID_NOTIFY_WEBHOOK_SECRET` are configured;
  invalid URL/signing configuration fails startup.
- Repository/package metadata advanced to 0.6.0.

## 0.5.0 - 2026-11-15

### Added

- Complete alert lifecycle with `open`, `acknowledged` and `resolved` states,
  including operator resolution and reopen actions.
- Immutable `alert_events` audit ledger recording every trigger,
  acknowledgement, resolution and reopen with actor, note and timestamp.
- `GET /alerts/{alert_id}` and `GET /alerts/{alert_id}/events` endpoints.
- Alert filters for lifecycle status, rule, drone and triggered-at window;
  `status=active` selects open plus acknowledged alerts.
- Automatic reopening when a resolved alert's rule fires again, with a system
  audit event and recurrence count.
- Dashboard lifecycle filters, resolve/reopen controls and expandable audit
  timelines; the static demo implements the matching mock contract.

### Changed

- Alert recurrence after resolution starts a fresh handling cycle and clears
  the prior cycle's live acknowledgement/resolution fields while preserving
  its immutable event history.
- Alert records now expose `status`, `updated_at`, resolution fields and
  `reopen_count`; stale REST/WebSocket responses cannot roll newer state back.
- Upgrading an existing 0.4 database backfills legacy triggers and
  acknowledgements into the new audit timeline.
- Real-chain E2E verification now exercises acknowledge, resolve, audit,
  reopen and final resolution through PostgreSQL and HTTP.
- Repository/package metadata advanced to 0.5.0.

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
