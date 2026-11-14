import { useEffect, useRef } from "react";
import maplibregl, { type Map as MlMap, type GeoJSONSource } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { DroneObservation, Station } from "../types";

/**
 * MapView — renders stations + live observations on a MapLibre canvas.
 *
 * Base map: Esri "World Dark Gray Base" raster tiles — free for
 * non-commercial use, no API key, and the dark palette matches the
 * ATC-style console instead of punching a bright hole in it (the old
 * demotiles style had a #D8F2FF background; CartoDB now watermarks
 * anonymous tiles with "API KEY REQUIRED").
 *
 * Note the Esri tile URL order: /tile/{z}/{y}/{x} — row before column,
 * the reverse of MapLibre's {x}/{y}. Getting this wrong silently loads
 * mirrored tiles that still look plausible.
 *
 * Data flow:
 *   - stations → once on mount (idempotent setData)
 *   - observations → updates both the dot source and the trail source
 *   - we also auto-fit the camera on the first observation so the
 *     operator sees their fleet immediately.
 *
 * Trails: observations arrive newest-first, so per drone we reverse to
 * chronological order before building the LineString — otherwise the
 * polyline zig-zags backwards through the flight path.
 */
interface Props {
  stations: Station[];
  observations: DroneObservation[];
  onDroneClick?: (droneId: string) => void;
}

const STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    basemap: {
      type: "raster",
      tiles: [
        "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      ],
      tileSize: 256,
      maxzoom: 16,
      attribution:
        "Tiles © Esri — Esri, DeLorme, NAVTEQ",
    },
  },
  layers: [{ id: "basemap", type: "raster", source: "basemap" }],
};

function stationsToGeoJSON(stations: Station[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: stations.map((s) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [s.lon, s.lat] },
      properties: { station_id: s.station_id, name: s.name, online: s.online },
    })),
  };
}

function observationsToGeoJSON(obs: DroneObservation[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: obs.map((o) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [o.position.lon, o.position.lat] },
      properties: {
        drone_id: o.drone_id,
        station_id: o.station_id,
        protocol: o.protocol,
        alt_m: o.position.alt_m,
        received_at: o.received_at,
      },
    })),
  };
}

/** Great-circle distance in metres between two lon/lat pairs. */
function distMeters(a: [number, number], b: [number, number]): number {
  const R = 6371000;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b[1] - a[1]);
  const dLon = toRad(b[0] - a[0]);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a[1])) * Math.cos(toRad(b[1])) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

/**
 * One LineString per drone per *continuous* segment.
 *
 * Real tracks have gaps — the drone leaves radio range, the station
 * hands off, the route wraps. Drawing a straight line across a gap
 * invents a flight path that never happened and makes a slow survey
 * drone look like it teleported 3 km. So we split whenever consecutive
 * fixes are too far apart or too far apart in time.
 */
const MAX_GAP_M = 400;
const MAX_GAP_S = 45;

function trailsToGeoJSON(obs: DroneObservation[]): GeoJSON.FeatureCollection {
  const byDrone = new Map<string, DroneObservation[]>();
  for (const o of obs) {
    const arr = byDrone.get(o.drone_id);
    if (arr) arr.push(o);
    else byDrone.set(o.drone_id, [o]);
  }
  const features: GeoJSON.Feature[] = [];
  for (const [droneId, rows] of byDrone) {
    if (rows.length < 2) continue;
    // `obs` is newest-first; flip to chronological for a sane path.
    const pts = [...rows].reverse().map((o) => ({
      coord: [o.position.lon, o.position.lat] as [number, number],
      t: Date.parse(o.received_at) || 0,
    }));
    let seg: [number, number][] = [pts[0].coord];
    const flush = () => {
      if (seg.length >= 2) {
        features.push({
          type: "Feature",
          geometry: { type: "LineString", coordinates: seg },
          properties: { drone_id: droneId },
        });
      }
    };
    for (let k = 1; k < pts.length; k++) {
      const prev = pts[k - 1];
      const cur = pts[k];
      const tooFar = distMeters(prev.coord, cur.coord) > MAX_GAP_M;
      const tooLate = Math.abs(cur.t - prev.t) > MAX_GAP_S * 1000;
      if (tooFar || tooLate) {
        flush();
        seg = [cur.coord];
      } else {
        seg.push(cur.coord);
      }
    }
    flush();
  }
  return { type: "FeatureCollection", features };
}

export function MapView({ stations, observations, onDroneClick }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const fittedRef = useRef(false);
  // Latest onDroneClick, read by the click handler so the effect can
  // stay mount-only (see comment above).
  const onDroneClickRef = useRef(onDroneClick);

  // Keep the ref pointing at the latest callback, in render.  This is
  // safe because we don't *call* the callback during render — we only
  // read the ref from the map's click handler.
  onDroneClickRef.current = onDroneClick;

  // Init map once.
  // We intentionally do NOT include `onDroneClick` in the dep array —
  // App.tsx passes a fresh lambda on every render, including it would
  // tear down and recreate the entire map + sources on each parent
  // update.  The click handler is set once inside the load() closure
  // and reads the latest callback via the `onDroneClickRef` indirection.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: STYLE,
      center: [116.397, 39.908], // Beijing-ish default
      zoom: 4,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.on("load", () => {
      map.addSource("stations", {
        type: "geojson",
        data: stationsToGeoJSON([]),
      });
      map.addLayer({
        id: "stations-dot",
        type: "circle",
        source: "stations",
        paint: {
          "circle-radius": 6,
          "circle-color": "#5cc8ff",
          "circle-stroke-width": 2,
          "circle-stroke-color": "#0b0d12",
        },
      });
      map.addSource("trails", {
        type: "geojson",
        data: trailsToGeoJSON([]),
      });
      map.addLayer({
        id: "trails-line",
        type: "line",
        source: "trails",
        layout: { "line-join": "round", "line-cap": "round" },
        paint: {
          "line-color": "#ff5c5c",
          "line-width": 1.5,
          "line-opacity": 0.45,
        },
      });
      map.addSource("drones", {
        type: "geojson",
        data: observationsToGeoJSON([]),
      });
      map.addLayer({
        id: "drones-dot",
        type: "circle",
        source: "drones",
        paint: {
          "circle-radius": 5,
          "circle-color": "#ff5c5c",
          "circle-stroke-width": 1.5,
          "circle-stroke-color": "#fff",
        },
      });
      map.on("click", "drones-dot", (e) => {
        const f = e.features?.[0];
        const id = f?.properties?.drone_id as string | undefined;
        // Read through the ref so the effect deps can stay empty.
        const cb = onDroneClickRef.current;
        if (id && cb) cb(id);
      });
      map.on("mouseenter", "drones-dot", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "drones-dot", () => {
        map.getCanvas().style.cursor = "";
      });
    });
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      fittedRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Push stations.
  useEffect(() => {
    const m = mapRef.current;
    if (!m) return;
    const apply = () => {
      const src = m.getSource("stations") as GeoJSONSource | undefined;
      src?.setData(stationsToGeoJSON(stations));
    };
    if (m.isStyleLoaded()) apply();
    else m.once("load", apply);
  }, [stations]);

  // Push observations + auto-fit on first.
  useEffect(() => {
    const m = mapRef.current;
    if (!m || observations.length === 0) return;
    const apply = () => {
      const src = m.getSource("drones") as GeoJSONSource | undefined;
      src?.setData(observationsToGeoJSON(observations));
      const trailSrc = m.getSource("trails") as GeoJSONSource | undefined;
      trailSrc?.setData(trailsToGeoJSON(observations));
      if (!fittedRef.current) {
        // Fit to the latest 20 observations.
        const pts = observations.slice(0, 20);
        const lons = pts.map((o) => o.position.lon);
        const lats = pts.map((o) => o.position.lat);
        const bounds: [[number, number], [number, number]] = [
          [Math.min(...lons), Math.min(...lats)],
          [Math.max(...lons), Math.max(...lats)],
        ];
        m.fitBounds(bounds, { padding: 60, duration: 800, maxZoom: 14 });
        fittedRef.current = true;
      }
    };
    if (m.isStyleLoaded()) apply();
    else m.once("load", apply);
  }, [observations]);

  return <div ref={containerRef} className="map-container" />;
}
