import type { DroneObservation } from "../types";

interface Props {
  observations: DroneObservation[];
  selectedDroneId?: string | null;
  onSelect?: (droneId: string) => void;
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

export function ObservationList({ observations, selectedDroneId, onSelect }: Props) {
  if (observations.length === 0) {
    return (
      <div className="obs-list empty">
        <p>No observations yet. Waiting for a drone to come online…</p>
      </div>
    );
  }
  return (
    <ul className="obs-list">
      {observations.map((o) => {
        const isSel = o.drone_id === selectedDroneId;
        return (
          <li
            key={o.observation_id}
            className={`obs-row ${isSel ? "selected" : ""}`}
            onClick={() => onSelect?.(o.drone_id)}
          >
            <div className="obs-row-1">
              <span className={`drone-id ${o.operator_id ? "" : "ghost"}`}>
                {o.operator_id ? "" : "⚠ "}
                {o.drone_id}
              </span>
              <span className={`tag proto-${o.protocol}`}>{o.protocol}</span>
            </div>
            <div className="obs-row-2">
              <span>
                {o.position.lat.toFixed(4)}, {o.position.lon.toFixed(4)}
              </span>
              <span>{o.position.alt_m.toFixed(0)} m</span>
              <span className="dim">{fmtTime(o.received_at)}</span>
            </div>
            <div className="obs-row-3 dim">
              via {o.station_id}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
