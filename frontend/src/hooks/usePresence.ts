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
    let running = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;

    function schedule() {
      if (!alive || document.visibilityState === "hidden") return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void load(), POLL_MS);
    }

    async function load() {
      if (!alive || running || document.visibilityState === "hidden") return;
      running = true;
      controller?.abort();
      const current = new AbortController();
      controller = current;
      try {
        // The sidebar/header are driven by the WS stream; the registry
        // adds detail fields such as station assignment and operator IDs.
        const [stations, drones] = await Promise.all([
          api.listStations(current.signal),
          api.listDrones({ onlineWithinS: 3600 }, current.signal),
        ]);
        if (!alive || current.signal.aborted) return;
        setState((s) => ({
          ...s,
          stations,
          drones,
          error: null,
          tick: s.tick + 1,
        }));
      } catch (error) {
        if (!alive || current.signal.aborted) return;
        setState((s) => ({ ...s, error: (error as Error).message }));
      } finally {
        if (controller === current) controller = null;
        running = false;
        schedule();
      }
    }

    function onVisibilityChange() {
      if (document.visibilityState === "hidden") {
        controller?.abort();
        controller = null;
        if (timer) clearTimeout(timer);
        timer = null;
        return;
      }
      if (timer) clearTimeout(timer);
      timer = null;
      void load();
    }

    void load();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      alive = false;
      controller?.abort();
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  return state;
}
