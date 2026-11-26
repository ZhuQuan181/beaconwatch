import { useCallback, useEffect, useState } from "react";
import "./styles.css";
import { MapView } from "./components/MapView";
import { ObservationList } from "./components/ObservationList";
import { ConnectionBadge } from "./components/ConnectionBadge";
import { Timeline } from "./components/Timeline";
import { AlertPanel } from "./components/AlertPanel";
import { useObservationsStream } from "./hooks/useObservationsStream";
import { usePresence } from "./hooks/usePresence";
import { api } from "./api/client";
import type {
  AuthPrincipal,
  DroneObservation,
  GeofenceZone,
  Protocol,
} from "./types";

/** How far back the history buffer reaches, for the timeline window. */
const HISTORY_LIMIT = 1000;
/** History/unidentified refresh cadence.  Live rows still arrive via WS. */
const HISTORY_REFRESH_MS = 4_000;

function relativeAge(iso?: string | null): string {
  if (!iso) return "";
  const elapsed = Date.now() - Date.parse(iso);
  if (!Number.isFinite(elapsed)) return "";
  const seconds = Math.max(0, Math.floor(elapsed / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export function App() {
  const {
    recent,
    alerts,
    total,
    connected,
    error,
    acknowledgeAlert,
    resolveAlert,
    reopenAlert,
    loadAlertHistory,
    alertNextCursor,
    alertsLoading,
    alertsError,
    alertStats,
    loadOlderAlerts,
  } = useObservationsStream();
  // Stations come from the presence registry (server is the source of
  // truth for which stations exist).  The per-station "online" dot is
  // derived from the `recent` stream — a station is only online if
  // we've heard from it since the page loaded.  This avoids the
  // bug where a station that recently went offline still showed
  // green just because the registry hadn't polled yet.
  const { stations: registeredStations } = usePresence();

  // ── Timeline state ────────────────────────────────────────────────
  // `cursorTs === null` means LIVE.  Anything else is a replay frame:
  // the map and sidebar render `replayRows` (fetched from the server
  // for a window ending at the cursor) instead of the WebSocket stream.
  //
  // Keeping replay data separate from `recent` matters: if we filtered
  // the live buffer instead, scrubbing past the buffer's horizon would
  // silently show nothing while claiming to be showing history.
  const [cursorTs, setCursorTs] = useState<string | null>(null);
  const [history, setHistory] = useState<DroneObservation[]>([]);
  const [replayRows, setReplayRows] = useState<DroneObservation[]>([]);
  const [replayLoading, setReplayLoading] = useState(false);
  const [ghostIds, setGhostIds] = useState<string[]>([]);
  const [zones, setZones] = useState<GeofenceZone[]>([]);
  const [auth, setAuth] = useState<AuthPrincipal | null>(null);
  const [query, setQuery] = useState("");
  const [protocolFilter, setProtocolFilter] = useState<"all" | Protocol>("all");
  const [ghostsOnly, setGhostsOnly] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api
      .getAuthMe(controller.signal)
      .then(setAuth)
      .catch(() => setAuth(null));
    return () => controller.abort();
  }, []);

  // Zones are static for a session; fetch once.
  useEffect(() => {
    api
      .listZones()
      .then(setZones)
      .catch(() => setZones([]));
  }, []);

  // Refresh timeline metadata on a fixed cadence.  This used to depend on
  // `total`, which changes for every WebSocket row: a busy feed launched
  // a 1,000-row history request (and another unidentified query) for every
  // single observation.  Polling decouples HTTP load from ingest rate.
  useEffect(() => {
    let alive = true;
    let refreshing = false;
    let controller: AbortController | null = null;

    const refresh = async () => {
      if (!alive || refreshing || document.visibilityState === "hidden") return;
      refreshing = true;
      controller?.abort();
      const current = new AbortController();
      controller = current;
      try {
        const [rows, unidentified] = await Promise.all([
          api.listObservations({ limit: HISTORY_LIMIT }, current.signal),
          api.listUnidentified(current.signal),
        ]);
        if (!alive || current.signal.aborted) return;
        setHistory(rows);
        setGhostIds(unidentified);
      } catch {
        /* keep the previous snapshot; the live view is unaffected */
      } finally {
        if (controller === current) controller = null;
        refreshing = false;
      }
    };

    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") void refresh();
    };

    void refresh();
    const timer = window.setInterval(() => void refresh(), HISTORY_REFRESH_MS);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      alive = false;
      controller?.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  // When the cursor moves, fetch a window that ends at the cursor.
  //
  // The window is density-driven rather than fixed: with a 90 s window
  // and 12 s reporting, each drone contributes ~7 fixes — enough for a
  // dot, not a track.  Since the whole point of scrubbing is seeing
  // where a drone *came from*, we widen the window until the busiest
  // drone has enough points to draw a path, capped so the query stays
  // cheap.
  useEffect(() => {
    if (cursorTs === null) {
      setReplayRows([]);
      setReplayLoading(false);
      return;
    }
    let alive = true;
    const controller = new AbortController();
    setReplayLoading(true);
    const to = new Date(cursorTs);

    const fetchWindow = async (seconds: number) => {
      const from = new Date(to.getTime() - seconds * 1000);
      return api.listObservations({
        limit: 1000,
        from: from.toISOString(),
        to: to.toISOString(),
      }, controller.signal);
    };

    (async () => {
      try {
        let seconds = 300;
        let rows = await fetchWindow(seconds);
        // Grow once if the trail would still be too sparse.  Two
        // attempts bound the latency: worst case ~2 round trips.
        const perDrone = new Map<string, number>();
        for (const o of rows) {
          perDrone.set(o.drone_id, (perDrone.get(o.drone_id) ?? 0) + 1);
        }
        const maxPts = Math.max(0, ...perDrone.values());
        if (rows.length > 0 && maxPts < 6) {
          seconds = 900;
          rows = await fetchWindow(seconds);
        }
        if (!alive || controller.signal.aborted) return;
        setReplayRows(rows);
        setReplayLoading(false);
      } catch {
        if (!alive || controller.signal.aborted) return;
        setReplayRows([]);
        setReplayLoading(false);
      }
    })();

    return () => {
      alive = false;
      controller.abort();
    };
  }, [cursorTs]);

  const isReplay = cursorTs !== null;
  // In replay, render only fixes at or before the cursor — otherwise a
  // drone appears ahead of where it actually was at that moment.
  const displayed = isReplay
    ? replayRows.filter(
        (o) => Date.parse(o.received_at) <= Date.parse(cursorTs as string),
      )
    : recent;

  const normalizedQuery = query.trim().toLowerCase();
  const filtered = displayed.filter((o) => {
    if (protocolFilter !== "all" && o.protocol !== protocolFilter) return false;
    if (ghostsOnly && o.operator_id) return false;
    if (!normalizedQuery) return true;
    return [o.drone_id, o.operator_id, o.station_id, o.self_id]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(normalizedQuery);
  });

  // Station presence must follow the unfiltered live feed; otherwise
  // hiding a station's observations makes its receiver look offline.
  const seenStationIds = new Set(recent.map((o) => o.station_id));
  const stations = registeredStations.map((s) => ({
    ...s,
    online: s.online && seenStationIds.has(s.station_id),
  }));

  // onDroneClick is wrapped in useCallback so that MapView's
  // mount-only effect (see that file) doesn't tear down on every
  // parent re-render.  setSelected has a stable identity by React's
  // guarantee, so the empty deps array is correct.
  const [selected, setSelected] = useState<string | null>(null);
  const handleDroneClick = useCallback((id: string) => setSelected(id), []);

  // Deduplicate down to one row per drone (the most-recent
  // observation wins, since rows are newest-first).
  const latestByDrone = new Map<string, DroneObservation>();
  for (const o of filtered) {
    if (!latestByDrone.has(o.drone_id)) {
      latestByDrone.set(o.drone_id, o);
    }
  }
  const latestDrones = Array.from(latestByDrone.values());
  const droneCount = latestDrones.length;
  const canOperate =
    auth === null || auth.capabilities.includes("alerts:write");
  const actorName = auth?.authenticated ? auth.display_name : null;

  async function exportCurrentWindow() {
    if (displayed.length === 0 || exporting) return;
    const times = displayed
      .map((o) => Date.parse(o.received_at))
      .filter((value) => Number.isFinite(value));
    if (times.length === 0) return;
    setExporting(true);
    setExportError(null);
    try {
      const from = new Date(Math.min(...times)).toISOString();
      const to = new Date(Math.max(...times) + 1).toISOString();
      const { blob, filename } = await api.exportObservations({
        format: "csv",
        from,
        to,
        unidentifiedOnly: ghostsOnly,
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      setExportError((error as Error).message);
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className={`app ${isReplay ? "app--replay" : ""}`}>
      <header className="topbar">
        <h1>🚁 BeaconWatch</h1>
        <span className="stat">
          <span className={`ws-dot ${connected ? "ws-online" : "ws-offline"}`} />
          WS {connected ? "online" : "offline"} · {total} msgs
        </span>
        {isReplay && (
          <span className="stat stat--replay" title={cursorTs ?? ""}>
            历史回放 · 数据截止 {new Date(cursorTs as string).toLocaleTimeString("zh-CN", { hour12: false })}
          </span>
        )}
        <span className="spacer" />
        <span className="stat">
          {stations.length} station · {droneCount} drone
          {filtered.length !== displayed.length ? ` · ${filtered.length}/${displayed.length}` : ""}
        </span>
        <span
          className="stat auth-identity"
          title={
            auth
              ? `roles: ${auth.roles.join(", ")} · ${
                  auth.authenticated ? "authenticated" : "open mode"
                }`
              : "authentication unavailable"
          }
        >
          {auth?.authenticated ? auth.display_name : "open mode"}
        </span>
        <ConnectionBadge />
      </header>

      <aside className="sidebar">
        <div className="section">
          <h2>Stations</h2>
          {stations.length === 0 ? (
            <p className="dim">No stations registered.</p>
          ) : (
            stations.map((s) => (
              <div key={s.station_id} className="station-row">
                <span>
                  <span className={`dot ${s.online ? "online" : "offline"}`} />
                  {s.name || s.station_id}
                </span>
                <span
                  className="dim"
                  title={s.last_seen ? `last seen ${s.last_seen}` : s.station_id}
                >
                  {!s.online && s.last_seen
                    ? `offline ${relativeAge(s.last_seen)}`
                    : (s.coverage_radius_m ?? 0) / 1000 > 0
                      ? `${((s.coverage_radius_m ?? 0) / 1000).toFixed(1)} km`
                      : s.station_id}
                </span>
              </div>
            ))
          )}
        </div>

        <div className="section section--alerts">
          <AlertPanel
            alerts={alerts}
            onSelectDrone={setSelected}
            onAcknowledge={canOperate ? acknowledgeAlert : undefined}
            onResolve={canOperate ? resolveAlert : undefined}
            onReopen={canOperate ? reopenAlert : undefined}
            onLoadHistory={loadAlertHistory}
            alertStats={alertStats}
            hasMoreAlerts={Boolean(alertNextCursor)}
            alertsLoading={alertsLoading}
            alertsError={alertsError}
            onLoadOlder={loadOlderAlerts}
            currentActor={actorName}
            canOperate={canOperate}
          />
        </div>

        <div className="section" style={{ flex: 1, padding: 0, minHeight: 0 }}>
          <h2 style={{ padding: "12px 14px 8px" }}>
            {isReplay
              ? `Observations @ ${new Date(cursorTs as string).toLocaleTimeString("zh-CN", { hour12: false })}`
              : "Live Observations"}
          </h2>
          <div className="obs-toolbar">
            <input
              className="obs-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索无人机 / 操作员 / 站点"
              aria-label="搜索观测"
            />
            <select
              className="obs-select"
              value={protocolFilter}
              onChange={(event) =>
                setProtocolFilter(event.target.value as "all" | Protocol)
              }
              aria-label="协议筛选"
            >
              <option value="all">全部协议</option>
              <option value="wifi_nan">Wi-Fi NAN</option>
              <option value="wifi_beacon">Wi-Fi Beacon</option>
              <option value="ble">BLE</option>
              <option value="astm_f3411">ASTM F3411</option>
              <option value="unknown">Unknown</option>
            </select>
            <label className="obs-toggle">
              <input
                type="checkbox"
                checked={ghostsOnly}
                onChange={(event) => setGhostsOnly(event.target.checked)}
              />
              仅未识别
            </label>
            <button
              type="button"
              className="obs-export"
              onClick={() => void exportCurrentWindow()}
              disabled={exporting || displayed.length === 0}
              title="导出当前显示时间窗的 CSV"
            >
              {exporting ? "导出中…" : "导出 CSV"}
            </button>
          </div>
          {(filtered.length !== displayed.length || exportError) && (
            <div className="obs-filter-meta">
              <span>
                显示 {filtered.length} / {displayed.length} 条
              </span>
              {exportError && <span className="obs-filter-error">{exportError}</span>}
            </div>
          )}
          {replayLoading ? (
            <div className="obs-list empty">
              <p>正在取回放数据…</p>
            </div>
          ) : (
            <ObservationList
              observations={latestDrones}
              selectedDroneId={selected}
              onSelect={setSelected}
              emptyMessage={
                displayed.length > 0
                  ? "没有符合当前筛选条件的观测。"
                  : undefined
              }
            />
          )}
        </div>

        {error && (
          <div className="section" style={{ color: "#ff7575", fontSize: 12 }}>
            ⚠ {error}
          </div>
        )}
      </aside>

      <MapView
        stations={stations}
        observations={filtered}
        zones={zones}
        selectedDroneId={selected}
        onDroneClick={handleDroneClick}
      />

      <Timeline
        history={history}
        cursorTs={cursorTs}
        onSeek={setCursorTs}
        unidentifiedDroneIds={ghostIds}
      />
    </div>
  );
}
