import { useEffect, useRef } from "react";
import maplibregl, { type Map as MlMap, type GeoJSONSource } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { DroneObservation, Station } from "../types";

/**
 * MapView — renders stations + live observations on a MapLibre canvas.
 *
 * We use the MapLibre demo style (no API key) which is fine for
 * dev.  Swap to a self-hosted style + raster fallback for prod.
 *
 * Data flow:
 *   - stations → once on mount (idempotent setData)
 *   - observations → updates the source on every change
 *   - we also auto-fit the camera on the first observation so the
 *     operator sees their fleet immediately.
 */
interface Props {
  stations: Station[];
  observations: DroneObservation[];
  onDroneClick?: (droneId: string) => void;
}

const STYLE_URL = "https://demotiles.maplibre.org/style.json";

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
      style: STYLE_URL,
      center: [116.397, 39.908], // Beijing-ish default
      zoom: 4,
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
