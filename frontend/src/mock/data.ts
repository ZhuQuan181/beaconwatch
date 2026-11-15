/**
 * Deterministic demo data for the GitHub Pages build.
 *
 * Nothing here talks to a backend: observations advance on a timer,
 * alerts are synthesised from the same feed, and every REST shape
 * mirrors `src/types/index.ts`.
 */
import type {
  Alert,
  AlertEvent,
  DroneObservation,
  DroneSummary,
  GeofenceZone,
  Position,
  Protocol,
  Station,
  WsFrame,
} from "../types";

interface MockDrone {
  droneId: string;
  uasId: string;
  uaType: string;
  base: Position;
  phase: number;
  protocol: Protocol;
}

const DRONES: MockDrone[] = [
  {
    droneId: "FAA-1583A2",
    uasId: "1583A2",
    uaType: "Helicopter",
    base: { lat: 37.7899, lon: -122.4014, alt_m: 120, speed_mps: 8, heading_deg: 45 },
    phase: 0.2,
    protocol: "wifi_nan",
  },
  {
    droneId: "FAA-9C41D0",
    uasId: "9C41D0",
    uaType: "Fixed wing",
    base: { lat: 37.7812, lon: -122.4132, alt_m: 210, speed_mps: 18, heading_deg: 190 },
    phase: 1.4,
    protocol: "ble",
  },
  {
    droneId: "FAA-2B77E9",
    uasId: "2B77E9",
    uaType: "Multirotor",
    base: { lat: 37.8035, lon: -122.4186, alt_m: 90, speed_mps: 5, heading_deg: 300 },
    phase: 2.7,
    protocol: "wifi_beacon",
  },
  {
    droneId: "FAA-51AF08",
    uasId: "51AF08",
    uaType: "Multirotor",
    base: { lat: 37.7688, lon: -122.4312, alt_m: 60, speed_mps: 3, heading_deg: 120 },
    phase: 3.9,
    protocol: "astm_f3411",
  },
  {
    droneId: "FAA-6D20B4",
    uasId: "6D20B4",
    uaType: "Glider",
    base: { lat: 37.7955, lon: -122.4378, alt_m: 330, speed_mps: 12, heading_deg: 80 },
    phase: 5.1,
    protocol: "wifi_nan",
  },
  {
    droneId: "UNKNOWN-7C1A",
    uasId: "",
    uaType: "Unknown",
    base: { lat: 37.7758, lon: -122.4091, alt_m: 145, speed_mps: 6, heading_deg: 220 },
    phase: 4.4,
    protocol: "unknown",
  },
];

export const stations: Station[] = [
  {
    station_id: "ST-01",
    name: "Pier 39",
    lat: 37.8087,
    lon: -122.4098,
    alt_m: 5,
    online: true,
    last_seen: new Date().toISOString(),
    firmware_version: "1.4.2",
    coverage_radius_m: 2500,
  },
  {
    station_id: "ST-02",
    name: "Ferry Building",
    lat: 37.7955,
    lon: -122.3937,
    alt_m: 8,
    online: true,
    last_seen: new Date().toISOString(),
    firmware_version: "1.4.2",
    coverage_radius_m: 1800,
  },
  {
    station_id: "ST-03",
    name: "Mission Bay",
    lat: 37.7701,
    lon: -122.3931,
    alt_m: 12,
    online: true,
    last_seen: new Date().toISOString(),
    firmware_version: "1.4.1",
    coverage_radius_m: 1500,
  },
];

export const zones: GeofenceZone[] = [
  {
    zone_id: "ZONE-ALCATRAZ",
    name: "Alcatraz No-Fly",
    polygon: [
      [-122.4255, 37.8282],
      [-122.4201, 37.8298],
      [-122.4168, 37.8259],
      [-122.4224, 37.8237],
    ],
    min_alt_m: 0,
    max_alt_m: 150,
    severity: "critical",
  },
  {
    zone_id: "ZONE-DOWNTOWN",
    name: "Downtown Warning",
    polygon: [
      [-122.4075, 37.7935],
      [-122.3975, 37.7952],
      [-122.3962, 37.7878],
      [-122.4068, 37.7861],
    ],
    min_alt_m: 0,
    max_alt_m: 120,
    severity: "warn",
  },
];

export const observations: DroneObservation[] = [];
export const alerts: Alert[] = [];
export const alertEvents: Record<string, AlertEvent[]> = {};

let sequence = 0;
let liveTick = 0;

function positionFor(drone: MockDrone, tick: number): Position {
  const radius = 0.0045 + (tick % 12) * 0.00008;
  const angle = drone.phase + tick * 0.045;
  return {
    lat: drone.base.lat + Math.sin(angle) * radius,
    lon: drone.base.lon + Math.cos(angle) * radius,
    alt_m: Math.round(drone.base.alt_m + Math.sin(angle * 2) * 14),
    speed_mps: drone.base.speed_mps,
    heading_deg: Math.round((((angle * 180) / Math.PI) % 360 + 360) % 360),
    accuracy_h_m: 3,
    accuracy_v_m: 5,
  };
}

function makeObservation(
  drone: MockDrone,
  receivedAt: string,
  tick: number,
): DroneObservation {
  sequence += 1;
  return {
    observation_id: `mock-${drone.droneId}-${sequence}`,
    drone_id: drone.droneId,
    operator_id: drone.uasId ? `OP-${drone.uasId.slice(0, 4)}` : null,
    station_id: stations[tick % stations.length].station_id,
    protocol: drone.protocol,
    position: positionFor(drone, tick),
    drone_timestamp: receivedAt,
    received_at: receivedAt,
    self_id: drone.uasId ? `drone-${drone.uasId}` : "",
    operator_location:
      drone.uasId && tick % 4 === 0
        ? {
            lat: drone.base.lat - 0.003,
            lon: drone.base.lon - 0.002,
            alt_m: 3,
          }
        : null,
  };
}

/** Seed ~3 minutes of history so the timeline and map are populated. */
export function seedHistory(pointsPerDrone = 90, intervalMs = 2000): void {
  if (observations.length > 0) return;
  const now = Date.now();
  for (let i = 0; i < pointsPerDrone; i += 1) {
    const receivedAt = new Date(now - i * intervalMs).toISOString();
    for (const drone of DRONES) {
      observations.push(makeObservation(drone, receivedAt, i));
    }
  }
  observations.sort(
    (a, b) => Date.parse(b.received_at) - Date.parse(a.received_at),
  );

  const firstAt = new Date(now - 42_000).toISOString();
  const secondAt = new Date(now - 96_000).toISOString();
  const thirdAt = new Date(now - 155_000).toISOString();
  alerts.push(
    {
      alert_id: "alert-mock-001",
      rule: "geofence",
      severity: "critical",
      drone_id: "FAA-1583A2",
      message: "Drone entered Alcatraz No-Fly zone",
      detail: { zone_id: "ZONE-ALCATRAZ", zone_name: "Alcatraz No-Fly" },
      at: firstAt,
      first_seen_at: firstAt,
      trigger_count: 1,
      status: "open",
      updated_at: firstAt,
      reopen_count: 0,
    },
    {
      alert_id: "alert-mock-002",
      rule: "unidentified",
      severity: "warn",
      drone_id: "UNKNOWN-7C1A",
      message: "Unidentified drone broadcasting without a Remote ID",
      detail: { station_id: "ST-03" },
      at: secondAt,
      first_seen_at: secondAt,
      trigger_count: 2,
      status: "acknowledged",
      updated_at: new Date(now - 78_000).toISOString(),
      acknowledged_at: new Date(now - 78_000).toISOString(),
      acknowledged_by: "demo-operator",
      acknowledgement_note: "正在用测向仪复核",
      reopen_count: 0,
    },
    {
      alert_id: "alert-mock-003",
      rule: "altitude",
      severity: "info",
      drone_id: "FAA-6D20B4",
      message: "Drone above 300 m advisory ceiling",
      detail: { altitude_m: 330, ceiling_m: 300 },
      at: thirdAt,
      first_seen_at: thirdAt,
      trigger_count: 1,
      status: "resolved",
      updated_at: new Date(now - 120_000).toISOString(),
      resolved_at: new Date(now - 120_000).toISOString(),
      resolved_by: "demo-operator",
      resolution_note: "已离开告警高度",
      reopen_count: 0,
    },
  );
  alertEvents["alert-mock-001"] = [
    {
      event_id: "event-mock-001-triggered",
      alert_id: "alert-mock-001",
      action: "triggered",
      actor: "system",
      at: firstAt,
      detail: { rule: "geofence" },
    },
  ];
  alertEvents["alert-mock-002"] = [
    {
      event_id: "event-mock-002-triggered",
      alert_id: "alert-mock-002",
      action: "triggered",
      actor: "system",
      at: secondAt,
      detail: { rule: "unidentified" },
    },
    {
      event_id: "event-mock-002-acknowledged",
      alert_id: "alert-mock-002",
      action: "acknowledged",
      actor: "demo-operator",
      note: "正在用测向仪复核",
      at: new Date(now - 78_000).toISOString(),
      detail: {},
    },
  ];
  alertEvents["alert-mock-003"] = [
    {
      event_id: "event-mock-003-triggered",
      alert_id: "alert-mock-003",
      action: "triggered",
      actor: "system",
      at: thirdAt,
      detail: { rule: "altitude" },
    },
    {
      event_id: "event-mock-003-resolved",
      alert_id: "alert-mock-003",
      action: "resolved",
      actor: "demo-operator",
      note: "已离开告警高度",
      at: new Date(now - 120_000).toISOString(),
      detail: {},
    },
  ];
}

export function droneSummaries(): DroneSummary[] {
  const grouped = new Map<string, DroneObservation[]>();
  for (const observation of observations) {
    const rows = grouped.get(observation.drone_id);
    if (rows) rows.push(observation);
    else grouped.set(observation.drone_id, [observation]);
  }
  return Array.from(grouped.entries()).map(([droneId, rows]) => {
    const latest = rows[0];
    const drone = DRONES.find((d) => d.droneId === droneId);
    return {
      drone_id: droneId,
      uas_id: drone?.uasId ?? "",
      ua_type: drone?.uaType ?? "Unknown",
      first_seen: rows[rows.length - 1].received_at,
      last_seen: latest.received_at,
      last_position: latest.position,
      last_station_id: latest.station_id,
      observation_count: rows.length,
    };
  });
}

/** Advance the simulated feed by one tick and return the frames to emit. */
export function nextLiveFrames(): WsFrame[] {
  liveTick += 1;
  const frames: WsFrame[] = [];

  for (const drone of DRONES) {
    const observation = makeObservation(
      drone,
      new Date().toISOString(),
      liveTick,
    );
    observations.unshift(observation);
    frames.push({ type: "obs", data: observation });
  }
  if (observations.length > 3000) observations.length = 3000;

  if (liveTick % 9 === 0) {
    const alertAt = new Date().toISOString();
    const alert: Alert = {
      alert_id: `alert-live-${liveTick}`,
      rule: "geofence",
      severity: "critical",
      drone_id: "FAA-1583A2",
      message: "Drone entered Downtown Warning zone",
      detail: { zone_id: "ZONE-DOWNTOWN", zone_name: "Downtown Warning" },
      at: alertAt,
      first_seen_at: alertAt,
      trigger_count: 1,
      status: "open",
      updated_at: alertAt,
      reopen_count: 0,
    };
    alerts.unshift(alert);
    if (alerts.length > 100) alerts.length = 100;
    alertEvents[alert.alert_id] = [
      {
        event_id: `event-${alert.alert_id}-triggered`,
        alert_id: alert.alert_id,
        action: "triggered",
        actor: "system",
        at: alert.at,
        detail: { rule: alert.rule },
      },
    ];
    frames.push({ type: "alert", data: alert });
  }

  return frames;
}
