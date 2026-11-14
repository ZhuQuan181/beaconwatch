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
import type { DroneObservation, GeofenceZone } from "./types";

/** How far back the history buffer reaches, for the timeline window. */
const HISTORY_LIMIT = 1000;
/** History/unidentified refresh cadence.  Live rows still arrive via WS. */
const HISTORY_REFRESH_MS = 4_000;

export function App() {
  const { recent, alerts, total, connected, error } = useObservationsStream();
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

  const seenStationIds = new Set(displayed.map((o) => o.station_id));
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
  for (const o of displayed) {
    if (!latestByDrone.has(o.drone_id)) {
      latestByDrone.set(o.drone_id, o);
    }
  }
  const latestDrones = Array.from(latestByDrone.values());
  const droneCount = latestDrones.length;

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
                <span className="dim">
                  {(s.coverage_radius_m ?? 0) / 1000 > 0
                    ? `${((s.coverage_radius_m ?? 0) / 1000).toFixed(1)} km`
                    : s.station_id}
                </span>
              </div>
            ))
          )}
        </div>

        <div className="section section--alerts">
          <AlertPanel alerts={alerts} onSelectDrone={setSelected} />
        </div>

        <div className="section" style={{ flex: 1, padding: 0, minHeight: 0 }}>
          <h2 style={{ padding: "12px 14px 8px" }}>
            {isReplay
              ? `Observations @ ${new Date(cursorTs as string).toLocaleTimeString("zh-CN", { hour12: false })}`
              : "Live Observations"}
          </h2>
          {replayLoading ? (
            <div className="obs-list empty">
              <p>正在取回放数据…</p>
            </div>
          ) : (
            <ObservationList
              observations={latestDrones}
              selectedDroneId={selected}
              onSelect={setSelected}
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
        observations={displayed}
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
