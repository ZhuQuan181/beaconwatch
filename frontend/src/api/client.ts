/**
 * Thin HTTP client.  Goes through Vite's /api proxy in dev so we
 * never talk to the server's port directly from the browser (no CORS,
 * no hardcoded hostnames).
 *
 * Every GET has a deadline, supports caller cancellation, and retries
 * only safe/idempotent requests with bounded jittered backoff.
 */
import type {
  Alert,
  AlertEvent,
  AlertPage,
  AlertStats,
  AuthPrincipal,
  DroneObservation,
  DroneSummary,
  GeofenceZone,
  ObservationPage,
  Station,
} from "../types";

const BASE = "/api";
const DEFAULT_TIMEOUT_MS = 8_000;
const DEFAULT_RETRIES = 2;
const API_TOKEN = (
  window.__BEACONWATCH_CONFIG__?.apiToken ||
  import.meta.env.VITE_API_TOKEN ||
  ""
).trim();

export interface AlertQuery {
  limit?: number;
  status?: "active" | "open" | "acknowledged" | "resolved";
  rule?: string;
  droneId?: string;
  from?: string;
  to?: string;
  cursor?: string;
}

class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly retryable = false,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function getApiToken(): string {
  return API_TOKEN;
}

function isRetryable(error: unknown): boolean {
  if (error instanceof ApiError) return error.retryable;
  return error instanceof TypeError;
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    let timer: ReturnType<typeof setTimeout>;
    const cleanup = () => signal?.removeEventListener("abort", onAbort);
    const onAbort = () => {
      clearTimeout(timer);
      cleanup();
      reject(new DOMException("Aborted", "AbortError"));
    };
    timer = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function fetchOnce<T>(
  path: string,
  timeoutMs: number,
  signal?: AbortSignal,
  method: "GET" | "POST" = "GET",
  body?: unknown,
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onOuterAbort = () => controller.abort();

  if (signal?.aborted) {
    controller.abort();
  } else {
    signal?.addEventListener("abort", onOuterAbort, { once: true });
  }

  try {
    const headers: Record<string, string> = {};
    if (API_TOKEN) headers.Authorization = `Bearer ${API_TOKEN}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(`${BASE}${path}`, {
      method,
      headers,
      signal: controller.signal,
      cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) {
      throw new ApiError(
        `${method} ${path} failed: ${res.status} ${res.statusText}`,
        res.status,
        res.status === 429 || res.status >= 500,
      );
    }
    return (await res.json()) as T;
  } catch (error) {
    if (timedOut) {
      throw new ApiError(`${method} ${path} timed out`, undefined, true);
    }
    if (signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onOuterAbort);
  }
}

async function jsonRequest<T>(
  path: string,
  options: {
    signal?: AbortSignal;
    timeoutMs?: number;
    retries?: number;
    method?: "GET" | "POST";
    body?: unknown;
  } = {},
): Promise<T> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const retries = Math.max(0, options.retries ?? DEFAULT_RETRIES);
  let lastError: unknown;

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await fetchOnce<T>(
        path,
        timeoutMs,
        options.signal,
        options.method ?? "GET",
        options.body,
      );
    } catch (error) {
      lastError = error;
      if (options.signal?.aborted || !isRetryable(error) || attempt === retries) {
        throw error;
      }
      await sleep(250 * 2 ** attempt + Math.random() * 150, options.signal);
    }
  }
  throw lastError;
}

async function jsonGet<T>(
  path: string,
  options: {
    signal?: AbortSignal;
    timeoutMs?: number;
    retries?: number;
  } = {},
): Promise<T> {
  return jsonRequest<T>(path, options);
}

async function jsonPost<T>(
  path: string,
  body: unknown,
  options: {
    signal?: AbortSignal;
    timeoutMs?: number;
    retries?: number;
  } = {},
): Promise<T> {
  return jsonRequest<T>(path, { ...options, method: "POST", body });
}

function alertQueryString(params: AlertQuery): string {
  const q = new URLSearchParams();
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  if (params.status) q.set("status", params.status);
  if (params.rule) q.set("rule", params.rule);
  if (params.droneId) q.set("drone_id", params.droneId);
  if (params.from) q.set("from", params.from);
  if (params.to) q.set("to", params.to);
  if (params.cursor) q.set("cursor", params.cursor);
  return q.toString();
}

export const api = {
  health: (signal?: AbortSignal) =>
    jsonGet<{ status: string }>("/health", { signal }),

  getAuthMe: (signal?: AbortSignal) =>
    jsonGet<AuthPrincipal>("/auth/me", { signal }),

  listStations: (signal?: AbortSignal) =>
    jsonGet<Station[]>("/stations", { signal }),

  listDrones: (
    params: { onlineWithinS?: number } = {},
    signal?: AbortSignal,
  ) => {
    const q = new URLSearchParams();
    if (params.onlineWithinS !== undefined) {
      q.set("online_within_s", String(params.onlineWithinS));
    }
    const qs = q.toString();
    return jsonGet<DroneSummary[]>(`/drones${qs ? `?${qs}` : ""}`, { signal });
  },

  listObservations: (
    params: {
      limit?: number;
      droneId?: string;
      from?: string;
      to?: string;
      unidentifiedOnly?: boolean;
      cursor?: string;
    } = {},
    signal?: AbortSignal,
  ) => {
    const q = new URLSearchParams();
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.droneId) q.set("drone_id", params.droneId);
    if (params.from) q.set("from", params.from);
    if (params.to) q.set("to", params.to);
    if (params.unidentifiedOnly) q.set("unidentified_only", "true");
    if (params.cursor) q.set("cursor", params.cursor);
    const qs = q.toString();
    return jsonGet<DroneObservation[]>(
      `/observations${qs ? `?${qs}` : ""}`,
      { signal },
    );
  },

  listObservationsPage: (
    params: {
      limit?: number;
      droneId?: string;
      from?: string;
      to?: string;
      unidentifiedOnly?: boolean;
      cursor?: string;
    } = {},
    signal?: AbortSignal,
  ) => {
    const q = new URLSearchParams();
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.droneId) q.set("drone_id", params.droneId);
    if (params.from) q.set("from", params.from);
    if (params.to) q.set("to", params.to);
    if (params.unidentifiedOnly) q.set("unidentified_only", "true");
    if (params.cursor) q.set("cursor", params.cursor);
    const qs = q.toString();
    return jsonGet<ObservationPage>(
      `/observations/page${qs ? `?${qs}` : ""}`,
      { signal },
    );
  },

  listUnidentified: (signal?: AbortSignal) =>
    jsonGet<string[]>("/drones/unidentified", { signal }),

  listZones: (signal?: AbortSignal) =>
    jsonGet<GeofenceZone[]>("/zones", { signal }),

  listAlerts: (limit = 50, signal?: AbortSignal) =>
    jsonGet<Alert[]>(`/alerts?limit=${limit}`, { signal }),

  listAlertsPage: (
    params: AlertQuery = {},
    signal?: AbortSignal,
  ) => {
    const qs = alertQueryString(params);
    return jsonGet<AlertPage>(
      `/alerts/page${qs ? `?${qs}` : ""}`,
      { signal },
    );
  },

  getAlertStats: (
    params: Omit<AlertQuery, "limit" | "cursor"> = {},
    signal?: AbortSignal,
  ) => {
    const qs = alertQueryString(params);
    return jsonGet<AlertStats>(
      `/alerts/stats${qs ? `?${qs}` : ""}`,
      { signal },
    );
  },

  acknowledgeAlert: (
    alertId: string,
    body: { acknowledged_by: string; note?: string },
    signal?: AbortSignal,
  ) =>
    jsonPost<Alert>(
      `/alerts/${encodeURIComponent(alertId)}/ack`,
      body,
      { signal },
    ),

  resolveAlert: (
    alertId: string,
    body: { resolved_by: string; note?: string },
    signal?: AbortSignal,
  ) =>
    jsonPost<Alert>(
      `/alerts/${encodeURIComponent(alertId)}/resolve`,
      body,
      { signal },
    ),

  reopenAlert: (
    alertId: string,
    body: { reopened_by: string; note?: string },
    signal?: AbortSignal,
  ) =>
    jsonPost<Alert>(
      `/alerts/${encodeURIComponent(alertId)}/reopen`,
      body,
      { signal, retries: 0 },
    ),

  listAlertEvents: (alertId: string, signal?: AbortSignal) =>
    jsonGet<AlertEvent[]>(
      `/alerts/${encodeURIComponent(alertId)}/events`,
      { signal },
    ),
};
