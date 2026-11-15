/**
 * Wire types — mirror the server's Pydantic models in
 * server/src/rid_server/models.py.  Keep these in lockstep.
 *
 * Position accuracy values are not persisted server-side (they live
 * in the raw JSON blob only), so we tolerate their absence on the
 * wire.
 */
export interface Position {
  lat: number;
  lon: number;
  alt_m: number;
  accuracy_h_m?: number;
  accuracy_v_m?: number;
  speed_mps?: number;
  heading_deg?: number;
}

export type Protocol = "wifi_nan" | "ble" | "wifi_beacon" | "astm_f3411" | "unknown";

export interface DroneObservation {
  observation_id: string;
  drone_id: string;
  operator_id?: string | null;
  station_id: string;
  protocol: Protocol;
  rssi_dbm?: number | null;
  position: Position;
  drone_timestamp?: string | null;
  received_at: string;
  self_id?: string;
  operator_location?: Position | null;
}

export interface DroneSummary {
  drone_id: string;
  uas_id: string;
  ua_type: string;
  first_seen: string;
  last_seen: string;
  last_position: Position;
  last_station_id: string;
  observation_count: number;
}

export interface Station {
  station_id: string;
  name: string;
  lat: number;
  lon: number;
  alt_m: number;
  online: boolean;
  last_seen?: string | null;
  firmware_version: string;
  /** Receiver reach in metres — drawn as a circle on the map. */
  coverage_radius_m?: number;
}

export interface ObservationPage {
  items: DroneObservation[];
  next_cursor: string | null;
}

/** A no-fly volume; the map draws it and the rules test against it. */
export interface GeofenceZone {
  zone_id: string;
  name: string;
  /** Ordered [lon, lat] ring, implicitly closed. */
  polygon: [number, number][];
  min_alt_m?: number | null;
  max_alt_m?: number | null;
  severity: "info" | "warn" | "critical" | string;
}

export type AlertRule = "geofence" | "altitude" | "unidentified";
export type AlertStatus = "open" | "acknowledged" | "resolved";

export interface Alert {
  alert_id: string;
  rule: AlertRule | string;
  severity: "info" | "warn" | "critical" | string;
  drone_id: string;
  message: string;
  detail: Record<string, unknown>;
  at: string;
  first_seen_at?: string | null;
  trigger_count?: number;
  status: AlertStatus;
  updated_at?: string | null;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
  acknowledgement_note?: string | null;
  resolved_at?: string | null;
  resolved_by?: string | null;
  resolution_note?: string | null;
  reopen_count?: number;
}

export interface AlertPage {
  items: Alert[];
  next_cursor: string | null;
}

export interface AlertStats {
  total: number;
  open: number;
  acknowledged: number;
  resolved: number;
  active: number;
  by_severity: Record<string, number>;
  by_rule: Record<string, number>;
  oldest_active_at?: string | null;
}

export interface AlertEvent {
  event_id: string;
  alert_id: string;
  action: "triggered" | "acknowledged" | "resolved" | "reopened";
  actor?: string | null;
  note?: string | null;
  at: string;
  detail: Record<string, unknown>;
}

/** Frame sent by the server over /ws/observations. */
export type WsFrame =
  | { type: "obs"; data: DroneObservation }
  | { type: "alert"; data: Alert }
  | { type: "ping" }
  | { type: "pong" };
