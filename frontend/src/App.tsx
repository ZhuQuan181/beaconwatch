import { useCallback, useEffect, useState } from "react";
import "./styles.css";
import { MapView } from "./components/MapView";
import { ObservationList } from "./components/ObservationList";
import { ConnectionBadge } from "./components/ConnectionBadge";
import { Timeline } from "./components/Timeline";
import { useObservationsStream } from "./hooks/useObservationsStream";
import { usePresence } from "./hooks/usePresence";
import { api } from "./api/client";
import type { DroneObservation } from "./types";

/** How far back the history buffer reaches, for the timeline window. */
const HISTORY_LIMIT = 1000;

export function App() {
  const { recent, total, connected, error } = useObservationsStream();
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

  // Pull the history window once, and refresh it when the live stream
  // grows, so the scrubber's bounds track reality without a polling loop.
  useEffect(() => {
    let alive = true;
    api
      .listObservations({ limit: HISTORY_LIMIT })
      .then((rows) => {
        if (alive) setHistory(rows);
      })
      .catch(() => {
        /* timeline stays empty; the live view is unaffected */
      });
    return () => {
      alive = false;
    };
  }, [total]);

  useEffect(() => {
    api
      .listUnidentified()
      .then(setGhostIds)
      .catch(() => setGhostIds([]));
  }, [total]);

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
    setReplayLoading(true);
    const to = new Date(cursorTs);

    const fetchWindow = async (seconds: number) => {
      const from = new Date(to.getTime() - seconds * 1000);
      return api.listObservations({
        limit: 1000,
        from: from.toISOString(),
        to: to.toISOString(),
      });
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
        if (!alive) return;
        setReplayRows(rows);
        setReplayLoading(false);
      } catch {
        if (!alive) return;
        setReplayRows([]);
        setReplayLoading(false);
      }
    })();

    return () => {
      alive = false;
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
        <h1>🚁 RID Live Dashboard</h1>
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
                <span className="dim">{s.station_id}</span>
              </div>
            ))
          )}
        </div>

        <div className="section" style={{ flex: 1, padding: 0 }}>
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
