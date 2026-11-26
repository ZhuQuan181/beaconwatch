import { useEffect, useState } from "react";
import type { OperationalOverview, Station } from "../types";
import { api } from "../api/client";

export interface PresenceState {
  stations: Station[];
  overview: OperationalOverview | null;
  error: string | null;
  /** Triggered when the underlying fleet changes (used for re-fetch). */
  tick: number;
}

// Presence changes on the order of heartbeats, not frames.  Polling
// every 5 s used to also fetch a drone list the dashboard never read.
const POLL_MS = 10_000;

export function usePresence() {
  const [state, setState] = useState<PresenceState>({
    stations: [],
    overview: null,
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
        const [stations, overview] = await Promise.all([
          api.listStations(current.signal),
          api.getOverview({}, current.signal),
        ]);
        if (!alive || current.signal.aborted) return;
        setState((s) => ({
          ...s,
          stations,
          overview,
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
