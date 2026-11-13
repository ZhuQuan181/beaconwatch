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
}

/** Frame sent by the server over /ws/observations. */
export type WsFrame =
  | { type: "obs"; data: DroneObservation }
  | { type: "ping" };
