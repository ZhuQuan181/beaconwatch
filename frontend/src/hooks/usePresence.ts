import { useEffect, useState } from "react";
import type { DroneSummary, Station } from "../types";
import { api } from "../api/client";

export interface PresenceState {
  stations: Station[];
  drones: DroneSummary[];
  error: string | null;
  /** Triggered when the underlying fleet changes (used for re-fetch). */
  tick: number;
}

const POLL_MS = 5_000;

export function usePresence() {
  const [state, setState] = useState<PresenceState>({
    stations: [],
    drones: [],
    error: null,
    tick: 0,
  });

  useEffect(() => {
    let alive = true;
    const tick = () => {
      // 1h window (server caps `online_within_s` at 3600).
      //
      // The sidebar/header are now driven by `latestDrones` (the WS
      // `recent` stream), not the registry, so this 1h value only
      // affects the *detail* payload of the registry (pilot/operator
      // IDs, last station, observation count).  We keep the window
      // tight because the registry is meant to reflect "currently
      // operating" drones — not a 24h historical list.
      Promise.all([
        api.listStations(),
        api.listDrones({ onlineWithinS: 3600 }),
      ])
        .then(([stations, drones]) => {
          if (!alive) return;
          setState((s) => ({ ...s, stations, drones, error: null, tick: s.tick + 1 }));
        })
        .catch((e) => {
          if (!alive) return;
          setState((s) => ({ ...s, error: (e as Error).message }));
        });
    };
    tick();
    const h = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, []);

  return state;
}
