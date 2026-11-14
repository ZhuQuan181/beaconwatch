/**
 * Browser-side mock transport for the static demo build.
 *
 * `installMockApi()` replaces `fetch` for `/api/*` and swaps
 * `WebSocket` for a timer-driven fake that emits the same frames the
 * real server would.  The app code is untouched.
 */
import type { WsFrame } from "../types";
import {
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

function handleApi(url: URL): Response {
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
    case "/api/alerts": {
      const requested = Number(url.searchParams.get("limit") ?? "50");
      const limit = Number.isFinite(requested) ? requested : 50;
      return jsonResponse(alerts.slice(0, limit));
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
      return Promise.resolve(handleApi(url));
    }
    return realFetch(input, init);
  };

  window.WebSocket = MockWebSocket as unknown as typeof WebSocket;
}
