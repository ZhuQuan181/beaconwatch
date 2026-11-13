/**
 * Thin HTTP client.  Goes through Vite's /api proxy in dev so we
 * never talk to the server's port directly from the browser (no CORS,
 * no hardcoded hostnames).
 */
import type { DroneObservation, DroneSummary, Station } from "../types";

const BASE = "/api";

async function jsonGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => jsonGet<{ status: string }>("/health"),
  listStations: () => jsonGet<Station[]>("/stations"),
  listDrones: (params: { onlineWithinS?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.onlineWithinS !== undefined) {
      q.set("online_within_s", String(params.onlineWithinS));
    }
    const qs = q.toString();
    return jsonGet<DroneSummary[]>(`/drones${qs ? `?${qs}` : ""}`);
  },
  listObservations: (params: { limit?: number; droneId?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.droneId) q.set("drone_id", params.droneId);
    const qs = q.toString();
    return jsonGet<DroneObservation[]>(
      `/observations${qs ? `?${qs}` : ""}`,
    );
  },
};
