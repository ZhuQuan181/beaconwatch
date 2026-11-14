/**
 * useObservationsStream — subscribes to /ws/observations, replays
 * the last N observations on connect, and surfaces the live feed.
 *
 * Reconnect strategy: exponential backoff capped at 10s, jittered.
 * A watchdog closes half-open sockets when the server's periodic ping
 * stops arriving.  The hook intentionally does NOT buffer messages
 * longer than the last 200 — old data belongs to the REST endpoint.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Alert, DroneObservation, WsFrame } from "../types";
import { api, getApiToken } from "../api/client";

const MAX_RETAIN = 200;
const MAX_ALERTS = 100;
const WS_STALE_MS = 45_000;

export interface StreamState {
  /** Most-recent observations, newest first. */
  recent: DroneObservation[];
  /** Most-recent alerts, newest first. */
  alerts: Alert[];
  /** Cumulative count since the page was opened. */
  total: number;
  /** True when the WebSocket is open. */
  connected: boolean;
  /** Most recent error, if any. */
  error: string | null;
  /** Last time a frame arrived. */
  lastMessageAt: number | null;
}

function mergeObservationRows(
  current: DroneObservation[],
  incoming: DroneObservation[],
): DroneObservation[] {
  const unique = new Map<string, DroneObservation>();
  for (const row of incoming) unique.set(row.observation_id, row);
  for (const row of current) {
    if (!unique.has(row.observation_id)) unique.set(row.observation_id, row);
  }
  return [...unique.values()]
    .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at))
    .slice(0, MAX_RETAIN);
}

export function useObservationsStream(wsUrl = "/ws/observations") {
  const [state, setState] = useState<StreamState>({
    recent: [],
    alerts: [],
    total: 0,
    connected: false,
    error: null,
    lastMessageAt: null,
  });
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(500);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current) {
      clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);

  // Alerts predate this page load.  Without the seed the panel is empty
  // until the next offending fix arrives, which on a quiet feed looks
  // like "no alerts" rather than "nothing new since you opened it".
  useEffect(() => {
    const controller = new AbortController();
    api
      .listAlerts(50, controller.signal)
      .then((rows) => {
        if (!rows.length || controller.signal.aborted) return;
        setState((s) => {
          const seen = new Set(s.alerts.map((a) => a.alert_id));
          const merged = [...s.alerts, ...rows.filter((a) => !seen.has(a.alert_id))]
            .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
            .slice(0, MAX_ALERTS);
          return { ...s, alerts: merged };
        });
      })
      .catch(() => {
        /* panel stays empty; live alerts still work */
      });
    return () => controller.abort();
  }, []);

  const connect = useCallback(() => {
    if (stoppedRef.current) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}${wsUrl}`;
    const protocols = ["rid.v1"];
    const token = getApiToken();
    if (token) protocols.push(`bearer.${token}`);

    let ws: WebSocket;
    try {
      ws = new WebSocket(url, protocols);
    } catch (error) {
      setState((s) => ({
        ...s,
        connected: false,
        error: `WebSocket setup failed: ${(error as Error).message}`,
      }));
      if (!stoppedRef.current) {
        timerRef.current = setTimeout(connect, backoffRef.current);
        backoffRef.current = Math.min(backoffRef.current * 2, 10_000);
      }
      return;
    }
    wsRef.current = ws;

    const armWatchdog = () => {
      clearWatchdog();
      watchdogRef.current = setTimeout(() => {
        if (stoppedRef.current || wsRef.current !== ws) return;
        setState((s) => ({ ...s, error: "WebSocket stale; reconnecting" }));
        ws.close();
      }, WS_STALE_MS);
    };

    ws.onopen = () => {
      armWatchdog();
      setState((s) => ({ ...s, connected: true, error: null }));
      backoffRef.current = 500;
    };
    ws.onmessage = (ev) => {
      armWatchdog();
      try {
        const frame = JSON.parse(ev.data) as WsFrame;
        if (frame.type === "ping" || frame.type === "pong") {
          setState((s) => ({ ...s, lastMessageAt: Date.now() }));
          return;
        }
        if (frame.type === "obs") {
          setState((s) => {
            const duplicate = s.recent.some(
              (row) => row.observation_id === frame.data.observation_id,
            );
            return {
              ...s,
              recent: duplicate
                ? s.recent
                : [frame.data, ...s.recent].slice(0, MAX_RETAIN),
              total: s.total + 1,
              lastMessageAt: Date.now(),
            };
          });
        } else if (frame.type === "alert") {
          setState((s) => {
            // The server dedups per (drone, rule, zone) in its own
            // engine, but a reconnect or a seed can still hand us the
            // same alert_id twice. Keying on the id keeps the panel
            // count honest — a list that says "12" while rendering 19
            // rows is worse than no counter at all.
            const exists = s.alerts.some(
              (alert) => alert.alert_id === frame.data.alert_id,
            );
            return {
              ...s,
              alerts: exists
                ? s.alerts
                : [frame.data, ...s.alerts].slice(0, MAX_ALERTS),
              lastMessageAt: Date.now(),
            };
          });
        }
      } catch (error) {
        setState((s) => ({
          ...s,
          error: `parse error: ${(error as Error).message}`,
        }));
      }
    };
    ws.onerror = () => {
      setState((s) => ({ ...s, error: "WebSocket error" }));
    };
    ws.onclose = (event) => {
      clearWatchdog();
      if (wsRef.current === ws) wsRef.current = null;
      if (stoppedRef.current) return;
      setState((s) => ({
        ...s,
        connected: false,
        error:
          event.code === 1008
            ? "WebSocket unauthorized — check VITE_API_TOKEN"
            : s.error,
      }));
      const delay = backoffRef.current + Math.random() * 200;
      backoffRef.current = Math.min(backoffRef.current * 2, 10_000);
      timerRef.current = setTimeout(connect, delay);
    };
  }, [clearWatchdog, wsUrl]);

  useEffect(() => {
    stoppedRef.current = false;
    const controller = new AbortController();

    // Seed with the last 50 from REST so the map isn't empty on first paint.
    api
      .listObservations({ limit: 50 }, controller.signal)
      .then((rows) => {
        if (controller.signal.aborted) return;
        setState((s) => ({
          ...s,
          recent: mergeObservationRows(s.recent, rows),
        }));
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setState((s) => ({
          ...s,
          error: `seed failed: ${(error as Error).message}`,
        }));
      });

    connect();
    return () => {
      stoppedRef.current = true;
      controller.abort();
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      clearWatchdog();
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close();
    };
  }, [clearWatchdog, connect]);

  return state;
}
