/**
 * Browser-side mock transport for the static demo build.
 *
 * `installMockApi()` replaces `fetch` for `/api/*` and swaps
 * `WebSocket` for a timer-driven fake that emits the same frames the
 * real server would.  The app code is untouched.
 */
import type { Alert, AlertStats, WsFrame } from "../types";
import {
  alertEvents,
  alerts,
  droneSummaries,
  nextLiveFrames,
  observations,
  seedHistory,
  stations,
  zones,
} from "./data";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function filterObservations(url: URL): Response {
  let rows = observations;

  const droneId = url.searchParams.get("drone_id");
  if (droneId) rows = rows.filter((o) => o.drone_id === droneId);

  if (url.searchParams.get("unidentified_only") === "true") {
    rows = rows.filter((o) => o.drone_id.startsWith("UNKNOWN-"));
  }

  const from = url.searchParams.get("from");
  if (from) {
    const start = Date.parse(from);
    rows = rows.filter((o) => Date.parse(o.received_at) >= start);
  }

  const to = url.searchParams.get("to");
  if (to) {
    const end = Date.parse(to);
    rows = rows.filter((o) => Date.parse(o.received_at) <= end);
  }

  const requested = Number(url.searchParams.get("limit") ?? "1000");
  const limit = Number.isFinite(requested) ? requested : 1000;
  return jsonResponse(rows.slice(0, limit));
}

function filterAlerts(url: URL): Alert[] {
  let rows = [...alerts].sort(
    (a, b) => Date.parse(b.at) - Date.parse(a.at),
  );
  const status = url.searchParams.get("status");
  if (status === "active") {
    rows = rows.filter((row) => row.status !== "resolved");
  } else if (status) {
    rows = rows.filter((row) => row.status === status);
  }
  const rule = url.searchParams.get("rule");
  if (rule) rows = rows.filter((row) => row.rule === rule);
  const droneId = url.searchParams.get("drone_id");
  if (droneId) rows = rows.filter((row) => row.drone_id === droneId);
  const from = url.searchParams.get("from");
  if (from) {
    const start = Date.parse(from);
    rows = rows.filter((row) => Date.parse(row.at) >= start);
  }
  const to = url.searchParams.get("to");
  if (to) {
    const end = Date.parse(to);
    rows = rows.filter((row) => Date.parse(row.at) <= end);
  }
  return rows;
}

function alertStats(rows: Alert[]): AlertStats {
  const open = rows.filter((row) => row.status === "open").length;
  const acknowledged = rows.filter(
    (row) => row.status === "acknowledged",
  ).length;
  const resolved = rows.filter((row) => row.status === "resolved").length;
  const bySeverity: Record<string, number> = {};
  const byRule: Record<string, number> = {};
  for (const row of rows) {
    bySeverity[row.severity] = (bySeverity[row.severity] ?? 0) + 1;
    byRule[row.rule] = (byRule[row.rule] ?? 0) + 1;
  }
  const activeRows = rows.filter((row) => row.status !== "resolved");
  const oldestActive = activeRows.reduce<string | null>((oldest, row) => {
    if (!oldest) return row.at;
    return Date.parse(row.at) < Date.parse(oldest) ? row.at : oldest;
  }, null);
  return {
    total: rows.length,
    open,
    acknowledged,
    resolved,
    active: open + acknowledged,
    by_severity: bySeverity,
    by_rule: byRule,
    oldest_active_at: oldestActive,
  };
}

function parseMockAlertCursor(value: string | null): number | null {
  if (!value) return 0;
  const match = /^mock-alert-(\d+)$/.exec(value);
  if (!match) return null;
  const offset = Number(match[1]);
  return Number.isSafeInteger(offset) && offset >= 0 ? offset : null;
}

function handleApi(url: URL, init: RequestInit): Response {
  const method = (init.method ?? "GET").toUpperCase();
  const actionMatch = url.pathname.match(
    /^\/api\/alerts\/([^/]+)\/(ack|resolve|reopen)$/,
  );
  if (actionMatch && method === "POST") {
    const alertId = decodeURIComponent(actionMatch[1]);
    const action = actionMatch[2];
    const alert = alerts.find((row) => row.alert_id === alertId);
    if (!alert) {
      return jsonResponse({ detail: "alert not found" }, 404);
    }
    let body: {
      acknowledged_by?: string;
      resolved_by?: string;
      reopened_by?: string;
      note?: string;
    } = {};
    try {
      body = JSON.parse(
        typeof init.body === "string" ? init.body : "{}",
      ) as typeof body;
    } catch {
      return jsonResponse({ detail: "invalid JSON" }, 400);
    }
    const actor =
      (
        action === "ack"
          ? body.acknowledged_by
          : action === "resolve"
            ? body.resolved_by
            : body.reopened_by
      )?.trim() ?? "";
    if (!actor) {
      return jsonResponse({ detail: "actor is required" }, 422);
    }
    const now = new Date().toISOString();
    const note = body.note?.trim() || null;
    const events = (alertEvents[alertId] ??= []);
    if (action === "ack") {
      if (alert.status === "resolved") {
        return jsonResponse(
          { detail: "resolved alerts must be reopened first" },
          409,
        );
      }
      if (!alert.acknowledged_at) {
        alert.acknowledged_at = now;
        alert.acknowledged_by = actor;
        alert.acknowledgement_note = note;
        alert.status = "acknowledged";
        alert.updated_at = now;
        events.push({
          event_id: `event-${alertId}-ack-${Date.now()}`,
          alert_id: alertId,
          action: "acknowledged",
          actor,
          note,
          at: now,
          detail: {},
        });
      }
    } else if (action === "resolve") {
      if (!alert.resolved_at) {
        alert.resolved_at = now;
        alert.resolved_by = actor;
        alert.resolution_note = note;
        alert.status = "resolved";
        alert.updated_at = now;
        events.push({
          event_id: `event-${alertId}-resolve-${Date.now()}`,
          alert_id: alertId,
          action: "resolved",
          actor,
          note,
          at: now,
          detail: {},
        });
      }
    } else {
      if (alert.status !== "resolved") {
        return jsonResponse(
          { detail: "only resolved alerts can be reopened" },
          409,
        );
      }
      const previousResolutionNote = alert.resolution_note;
      alert.status = "open";
      alert.acknowledged_at = null;
      alert.acknowledged_by = null;
      alert.acknowledgement_note = null;
      alert.resolved_at = null;
      alert.resolved_by = null;
      alert.resolution_note = null;
      alert.reopen_count = (alert.reopen_count ?? 0) + 1;
      alert.updated_at = now;
      events.push({
        event_id: `event-${alertId}-reopen-${Date.now()}`,
        alert_id: alertId,
        action: "reopened",
        actor,
        note,
        at: now,
        detail: {
          automatic: false,
          previous_resolution_note: previousResolutionNote,
        },
      });
    }
    return jsonResponse(alert);
  }

  const eventMatch = url.pathname.match(/^\/api\/alerts\/([^/]+)\/events$/);
  if (eventMatch && method === "GET") {
    const alertId = decodeURIComponent(eventMatch[1]);
    if (!alerts.some((row) => row.alert_id === alertId)) {
      return jsonResponse({ detail: "alert not found" }, 404);
    }
    return jsonResponse(alertEvents[alertId] ?? []);
  }

  switch (url.pathname) {
    case "/api/health":
      return jsonResponse({ status: "ok" });
    case "/api/stations":
      return jsonResponse(stations);
    case "/api/drones":
      return jsonResponse(droneSummaries());
    case "/api/drones/unidentified":
      return jsonResponse(
        Array.from(
          new Set(
            observations
              .filter((o) => o.drone_id.startsWith("UNKNOWN-"))
              .map((o) => o.drone_id),
          ),
        ),
      );
    case "/api/observations":
      return filterObservations(url);
    case "/api/zones":
      return jsonResponse(zones);
    case "/api/alerts/page": {
      const rows = filterAlerts(url);
      const requested = Number(url.searchParams.get("limit") ?? "50");
      const limit = Number.isFinite(requested) ? requested : 50;
      const offset = parseMockAlertCursor(url.searchParams.get("cursor"));
      if (offset === null) {
        return jsonResponse({ detail: "invalid alert cursor" }, 400);
      }
      const items = rows.slice(offset, offset + limit);
      const nextOffset = offset + items.length;
      return jsonResponse({
        items,
        next_cursor:
          nextOffset < rows.length
            ? `mock-alert-${nextOffset}`
            : null,
      });
    }
    case "/api/alerts/stats":
      return jsonResponse(alertStats(filterAlerts(url)));
    case "/api/alerts": {
      const requested = Number(url.searchParams.get("limit") ?? "50");
      const limit = Number.isFinite(requested) ? requested : 50;
      return jsonResponse(filterAlerts(url).slice(0, limit));
    }
    default:
      return jsonResponse({ detail: "mock endpoint not found" }, 404);
  }
}

class MockWebSocket {
  readonly url: string;
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  private openTimer: number | null = null;
  private feedTimer: number | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    this.openTimer = window.setTimeout(() => {
      this.readyState = 1;
      this.onopen?.(new Event("open"));
      this.feedTimer = window.setInterval(() => this.emit(), 1400);
    }, 250);
  }

  send(_data: string): void {
    /* the demo feed is read-only */
  }

  close(): void {
    if (this.openTimer !== null) window.clearTimeout(this.openTimer);
    if (this.feedTimer !== null) window.clearInterval(this.feedTimer);
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close"));
  }

  private emit(): void {
    const frames: WsFrame[] = nextLiveFrames();
    for (const frame of frames) {
      this.onmessage?.(
        new MessageEvent("message", { data: JSON.stringify(frame) }),
      );
    }
  }
}

let installed = false;

export function installMockApi(): void {
  if (installed) return;
  installed = true;
  seedHistory();

  const realFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const raw =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const url = new URL(raw, window.location.origin);
    if (url.pathname.startsWith("/api/")) {
      return Promise.resolve(handleApi(url, init ?? {}));
    }
    return realFetch(input, init);
  };

  window.WebSocket = MockWebSocket as unknown as typeof WebSocket;
}
