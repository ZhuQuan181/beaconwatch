/**
 * useObservationsStream — subscribes to /ws/observations, replays
 * the last N observations on connect, and surfaces the live feed.
 *
 * Reconnect strategy: exponential backoff capped at 10s, jittered.
 * The hook intentionally does NOT buffer messages longer than the
 * last 200 — old data is for the REST endpoint, not the live view.
 */
import { useEffect, useRef, useState, useCallback } from "react";
import type { DroneObservation, WsFrame } from "../types";
import { api } from "../api/client";

const MAX_RETAIN = 200;

export interface StreamState {
  /** Most-recent observations, newest first. */
  recent: DroneObservation[];
  /** Cumulative count since the page was opened. */
  total: number;
  /** True when the WebSocket is open. */
  connected: boolean;
  /** Most recent error, if any. */
  error: string | null;
  /** Last time a frame arrived. */
  lastMessageAt: number | null;
}

export function useObservationsStream(wsUrl = "/ws/observations") {
  const [state, setState] = useState<StreamState>({
    recent: [],
    total: 0,
    connected: false,
    error: null,
    lastMessageAt: null,
  });
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(500);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}${wsUrl}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setState((s) => ({ ...s, connected: true, error: null }));
      backoffRef.current = 500;
    };
    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as WsFrame;
        if (frame.type !== "obs") return; // ignore ping
        setState((s) => ({
          ...s,
          recent: [frame.data, ...s.recent].slice(0, MAX_RETAIN),
          total: s.total + 1,
          lastMessageAt: Date.now(),
        }));
      } catch (e) {
        setState((s) => ({
          ...s,
          error: `parse error: ${(e as Error).message}`,
        }));
      }
    };
    ws.onerror = () => {
      setState((s) => ({ ...s, error: "WebSocket error" }));
    };
    ws.onclose = () => {
      setState((s) => ({ ...s, connected: false }));
      // Reconnect with jittered backoff.
      const delay = backoffRef.current + Math.random() * 200;
      backoffRef.current = Math.min(backoffRef.current * 2, 10_000);
      timerRef.current = setTimeout(connect, delay);
    };
  }, [wsUrl]);

  useEffect(() => {
    // Seed with the last 50 from REST so the map isn't empty on first paint.
    api
      .listObservations({ limit: 50 })
      .then((rows) => {
        setState((s) => ({
          ...s,
          recent: [...rows, ...s.recent].slice(0, MAX_RETAIN),
        }));
      })
      .catch((e) => {
        setState((s) => ({ ...s, error: `seed failed: ${(e as Error).message}` }));
      });
    connect();
    return () => {
      wsRef.current?.close();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [connect]);

  return state;
}
