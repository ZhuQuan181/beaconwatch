import { useCallback, useState } from "react";
import "./styles.css";
import { MapView } from "./components/MapView";
import { ObservationList } from "./components/ObservationList";
import { ConnectionBadge } from "./components/ConnectionBadge";
import { useObservationsStream } from "./hooks/useObservationsStream";
import { usePresence } from "./hooks/usePresence";

export function App() {
  const { recent, total, connected, error } = useObservationsStream();
  // Stations come from the presence registry (server is the source of
  // truth for which stations exist).  The per-station "online" dot is
  // derived from the `recent` stream — a station is only online if
  // we've heard from it since the page loaded.  This avoids the
  // bug where a station that recently went offline still showed
  // green just because the registry hadn't polled yet.
  const { stations: registeredStations } = usePresence();
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

  // Deduplicate `recent` down to one row per drone (the most-recent
  // observation wins, since `recent` is already newest-first).
  const latestByDrone = new Map<string, (typeof recent)[number]>();
  for (const o of recent) {
    if (!latestByDrone.has(o.drone_id)) {
      latestByDrone.set(o.drone_id, o);
    }
  }
  const latestDrones = Array.from(latestByDrone.values());

  // Drone count for the topbar.
  //
  //   `latestDrones` (built from the WS `recent` stream + REST seed)
  //   is what the sidebar shows, so the header MUST agree with it.
  //
  //   The presence registry polls every 5s with a 1h window, so a
  //   drone last heard 20h ago is in `recent` (and rendered in the
  //   sidebar) but not in `drones` — using the registry would make
  //   the header disagree with the sidebar.  Keep the count purely
  //   client-side, sourced from `latestDrones`.  The registry is still
  //   surfaced for the station/drone detail panels and the map.
  const droneCount = latestDrones.length;

  return (
    <div className="app">
      <header className="topbar">
        <h1>🚁 RID Live Dashboard</h1>
        <span className="stat">
          <span className={`ws-dot ${connected ? "ws-online" : "ws-offline"}`} />
          WS {connected ? "online" : "offline"} · {total} msgs
        </span>
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
          <h2 style={{ padding: "12px 14px 8px" }}>Live Observations</h2>
          <ObservationList
            observations={latestDrones}
            selectedDroneId={selected}
            onSelect={setSelected}
          />
        </div>

        {error && (
          <div className="section" style={{ color: "#ff7575", fontSize: 12 }}>
            ⚠ {error}
          </div>
        )}
      </aside>

      <MapView
        stations={stations}
        observations={recent}
        onDroneClick={handleDroneClick}
      />
    </div>
  );
}
