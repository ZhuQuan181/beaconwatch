import { useEffect, useRef } from "react";
import {
  Map as MapLibreMap,
  NavigationControl,
  Popup,
  type GeoJSONSource,
  type Map as MlMap,
  type StyleSpecification,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { DroneObservation, GeofenceZone, Station } from "../types";

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
  zones?: GeofenceZone[];
  selectedDroneId?: string | null;
  onDroneClick?: (droneId: string) => void;
}

const STYLE: StyleSpecification = {
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

/** Closed rings for the coverage circles, in metres → degrees. */
function coverageToGeoJSON(stations: Station[]): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [];
  for (const s of stations) {
    const radius = s.coverage_radius_m ?? 3000;
    const coords: [number, number][] = [];
    // 64 segments is plenty at these radii; more just costs CPU.
    for (let i = 0; i <= 64; i++) {
      const theta = (i / 64) * Math.PI * 2;
      const dLat = (radius * Math.cos(theta)) / 111320;
      const dLon =
        (radius * Math.sin(theta)) /
        (111320 * Math.cos((s.lat * Math.PI) / 180) || 1);
      coords.push([s.lon + dLon, s.lat + dLat]);
    }
    features.push({
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [coords] },
      properties: { station_id: s.station_id, online: s.online, radius },
    });
  }
  return { type: "FeatureCollection", features };
}

/** Geofence rings. The stored ring has no closing point; GeoJSON wants one. */
function zonesToGeoJSON(zones: GeofenceZone[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: zones
      .filter((z) => Array.isArray(z.polygon) && z.polygon.length >= 3)
      .map((z) => {
        const ring = [...z.polygon];
        const [fx, fy] = ring[0];
        const [lx, ly] = ring[ring.length - 1];
        if (fx !== lx || fy !== ly) ring.push([fx, fy]);
        return {
          type: "Feature" as const,
          geometry: { type: "Polygon" as const, coordinates: [ring] },
          properties: {
            zone_id: z.zone_id,
            name: z.name,
            severity: z.severity,
          },
        };
      }),
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
        speed_mps: o.position.speed_mps,
        heading_deg: o.position.heading_deg,
        received_at: o.received_at,
        // A drone with no operator ID is the interesting one; bake it
        // into the feature so the paint expression can style it.
        unidentified: !o.operator_id,
      },
    })),
  };
}

function hoverCard(properties: Record<string, unknown>): HTMLElement {
  const root = document.createElement("div");
  root.className = "dh";

  const droneId = document.createElement("div");
  droneId.className = "dh-id";
  droneId.textContent = String(properties.drone_id ?? "");
  root.append(droneId);

  const uncertainAltitude = Number(properties.alt_m ?? 0);
  const altitude = Number.isFinite(uncertainAltitude) ? uncertainAltitude : 0;
  const uncertainSpeed = Number(properties.speed_mps ?? 0);
  const speed = Number.isFinite(uncertainSpeed) ? uncertainSpeed : 0;

  const rows: Array<[string, string]> = [
    ["高度", `${altitude.toFixed(0)} m`],
    ["速度", `${speed.toFixed(1)} m/s`],
    ["协议", String(properties.protocol ?? "")],
  ];
  for (const [label, value] of rows) {
    const row = document.createElement("div");
    row.className = "dh-row";
    const name = document.createElement("span");
    name.textContent = label;
    const reading = document.createElement("b");
    reading.textContent = value;
    row.append(name, reading);
    root.append(row);
  }

  const unidentified = Boolean(properties.unidentified);
  const flag = document.createElement("div");
  flag.className = `dh-flag ${unidentified ? "ghost" : "ok"}`;
  flag.textContent = unidentified ? "未广播操作员 ID" : "已识别";
  root.append(flag);
  return root;
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
 * Run `fn` once the map has created `sourceId`.
 *
 * `isStyleLoaded()` is not a good gate here: it flips false whenever
 * sources are being updated, so a styledata-driven retry can starve
 * forever.  The sources are created inside the map's `load` handler,
 * which makes their existence the real readiness signal.
 */
function whenSourceReady(map: MlMap, sourceId: string, fn: () => void): void {
  if (map.getSource(sourceId)) {
    fn();
    return;
  }
  map.once("load", fn);
}

/** One LineString per drone per *continuous* segment.
 *
 * Real tracks have gaps — the drone leaves radio range, the station
 * hands off, the route wraps. Drawing a straight line across a gap
 * invents a flight path that never happened and makes a slow survey
 * drone look like it teleported 3 km. So we split whenever consecutive
 * fixes are too far apart or too far apart in time.
 *
 * Thresholds are derived from the data, not hardcoded: the reported
 * interval and the drone's own speed decide what "too far" means.  A
 * fixed 400 m / 45 s pair broke on a 12 s feed where 14 m/s transit
 * legitimately covers 170 m per fix, and every segment got severed —
 * producing one long vertical line across the map instead of a track.
 *
 * Colour is baked into the feature (not the layer paint) so a ghost
 * drone's track burns red like its dot does — a red dot riding a cyan
 * trail is the kind of detail that makes an operator distrust the map.
 */
const MIN_GAP_M = 500;   // floor: below this is sensor noise, not a gap
const GAP_SPEED_FACTOR = 3;  // a gap is >3× the drone's own step distance
const MIN_GAP_S = 120;   // floor: 2 min of silence is a real gap

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
      unidentified: !o.operator_id,
    }));
    let seg: [number, number][] = [pts[0].coord];
    let segGhost = pts[0].unidentified;

    // Characterise this track's own cadence so the gap thresholds scale
    // with the feed instead of assuming a fixed reporting rate.
    const stepSamples: number[] = [];
    const distSamples: number[] = [];
    for (let k = 1; k < pts.length; k++) {
      stepSamples.push(Math.abs(pts[k].t - pts[k - 1].t) / 1000);
      distSamples.push(distMeters(pts[k - 1].coord, pts[k].coord));
    }
    const median = (xs: number[]) => {
      if (!xs.length) return 0;
      const s = [...xs].sort((a, b) => a - b);
      return s[Math.floor(s.length / 2)];
    };
    const medianStep = median(stepSamples);
    const medianStepM = median(distSamples);
    const flush = () => {
      if (seg.length >= 2) {
        features.push({
          type: "Feature",
          geometry: { type: "LineString", coordinates: seg },
          properties: { drone_id: droneId, unidentified: segGhost },
        });
      }
    };
    for (let k = 1; k < pts.length; k++) {
      const prev = pts[k - 1];
      const cur = pts[k];
      const stepM = distMeters(prev.coord, cur.coord);
      const stepS = Math.abs(cur.t - prev.t) / 1000;

      // Compare this hop against the track's own typical hop, so a fast
      // transit doesn't look like a gap. Fall back to floors when there
      // aren't enough points to characterise the track yet.
      const gapS = Math.max(MIN_GAP_S, medianStep * GAP_SPEED_FACTOR * 1.5);
      const gapM = Math.max(
        MIN_GAP_M,
        (medianStepM || 0) * GAP_SPEED_FACTOR,
      );
      const tooFar = stepM > gapM;
      const tooLate = stepS > gapS;
      // A change in identifiability also breaks the track: the drone
      // either started broadcasting or stopped, and that's information.
      const identityChanged = cur.unidentified !== segGhost;
      if (tooFar || tooLate || identityChanged) {
        flush();
        seg = [cur.coord];
        segGhost = cur.unidentified;
      } else {
        seg.push(cur.coord);
      }
    }
    flush();
  }
  return { type: "FeatureCollection", features };
}

export function MapView({
  stations,
  observations,
  zones = [],
  selectedDroneId = null,
  onDroneClick,
}: Props) {
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
    const map = new MapLibreMap({
      container: containerRef.current,
      style: STYLE,
      center: [116.397, 39.908], // Beijing-ish default
      zoom: 4,
      attributionControl: { compact: true },
    });
    map.addControl(new NavigationControl(), "top-right");
    map.on("load", () => {
      // ── Layer order (bottom → top) ──────────────────────────────
      // coverage → geofence → trails → drones
      // Context first, targets last: a donut of coverage circles must
      // never obscure the dot an operator is trying to click.
      map.addSource("coverage", {
        type: "geojson",
        data: coverageToGeoJSON([]),
      });
      map.addLayer({
        id: "coverage-fill",
        type: "fill",
        source: "coverage",
        paint: {
          "fill-color": "#5cc8ff",
          "fill-opacity": 0.05,
        },
      });
      map.addLayer({
        id: "coverage-outline",
        type: "line",
        source: "coverage",
        paint: {
          "line-color": "#5cc8ff",
          "line-width": 1.2,
          // 0.28 was invisible against the dark basemap; 0.5 reads as a
          // deliberate boundary without competing with the geofence.
          "line-opacity": 0.5,
          "line-dasharray": [2, 2],
        },
      });

      map.addSource("zones", {
        type: "geojson",
        data: zonesToGeoJSON([]),
      });
      map.addLayer({
        id: "zones-fill",
        type: "fill",
        source: "zones",
        paint: {
          // Severity drives colour so a critical zone reads as hotter
          // without needing a legend.
          "fill-color": [
            "match",
            ["get", "severity"],
            "critical", "#ff3b30",
            "warn", "#f0b429",
            "#8a94a6",
          ],
          "fill-opacity": 0.14,
        },
      });
      map.addLayer({
        id: "zones-outline",
        type: "line",
        source: "zones",
        paint: {
          "line-color": [
            "match",
            ["get", "severity"],
            "critical", "#ff3b30",
            "warn", "#f0b429",
            "#8a94a6",
          ],
          "line-width": 1.6,
          "line-opacity": 0.9,
        },
      });

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
          // Same colour logic as the dots: cooperative traffic cyan,
          // ghost traffic red. One visual language for one fact.
          "line-color": [
            "case",
            ["get", "unidentified"], "#ff3b30", "#5cc8ff",
          ],
          "line-width": [
            "case",
            ["get", "unidentified"], 2.0, 1.5,
          ],
          "line-opacity": [
            "case",
            ["get", "unidentified"], 0.75, 0.45,
          ],
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
          // Ghost drones (no operator ID) burn red; cooperative traffic
          // stays cyan. The colour is the whole story: the operator
          // doesn't read the sidebar to decide what to look at.
          "circle-radius": [
            "case",
            ["get", "unidentified"], 7, 5,
          ],
          "circle-color": [
            "case",
            ["get", "unidentified"], "#ff3b30", "#5cc8ff",
          ],
          "circle-stroke-width": 1.5,
          "circle-stroke-color": "#0b0d12",
        },
      });
      // Selection ring — a separate layer because colour already encodes
      // "is this drone a ghost". Reusing colour for selection would make
      // the two meanings indistinguishable.
      map.addSource("selected", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "selected-ring",
        type: "circle",
        source: "selected",
        paint: {
          "circle-radius": 15,
          "circle-color": "transparent",
          // Two-tone ring: white core with a dark halo.  A single white
          // stroke disappeared over the amber no-fly fill, and a single
          // dark one disappeared over the dark basemap.
          "circle-stroke-width": 3,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-opacity": 0.95,
        },
      });
      map.addLayer({
        id: "selected-ring-outer",
        type: "circle",
        source: "selected",
        paint: {
          "circle-radius": 18,
          "circle-color": "transparent",
          "circle-stroke-width": 1,
          "circle-stroke-color": "#0b0d12",
          "circle-stroke-opacity": 0.6,
        },
      });
      map.on("click", "drones-dot", (e) => {
        const f = e.features?.[0];
        const id = f?.properties?.drone_id as string | undefined;
        // Read through the ref so the effect deps can stay empty.
        const cb = onDroneClickRef.current;
        if (id && cb) cb(id);
      });

      // Hover card.  Built on Popup rather than a React overlay so it
      // tracks the map during pan/zoom without a re-render per frame.
      const hover = new Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 12,
        className: "drone-hover",
      });
      map.on("mouseenter", "drones-dot", (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties ?? {};
        hover
          .setLngLat((f.geometry as GeoJSON.Point).coordinates as [number, number])
          .setDOMContent(hoverCard(p))
          .addTo(map);
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mousemove", "drones-dot", (e) => {
        const f = e.features?.[0];
        if (f) {
          hover.setLngLat(
            (f.geometry as GeoJSON.Point).coordinates as [number, number],
          );
        }
      });
      map.on("mouseleave", "drones-dot", () => {
        hover.remove();
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
      const cov = m.getSource("coverage") as GeoJSONSource | undefined;
      cov?.setData(coverageToGeoJSON(stations));
    };
    whenSourceReady(m, "stations", apply);
  }, [stations]);

  // Push geofence zones.  Separate from stations because zones change
  // far less often — an operator draws one and leaves it alone.
  useEffect(() => {
    const m = mapRef.current;
    if (!m) return;
    const apply = () => {
      const src = m.getSource("zones") as GeoJSONSource | undefined;
      src?.setData(zonesToGeoJSON(zones));
    };
    whenSourceReady(m, "zones", apply);
  }, [zones]);

  // Push observations + auto-fit on first.
  useEffect(() => {
    const m = mapRef.current;
    if (!m || observations.length === 0) return;
    const apply = () => {
      const src = m.getSource("drones") as GeoJSONSource | undefined;
      // One dot per drone: the latest fix.  Drawing every historical fix
      // piled 50 overlapping circles into a string of beads that hid the
      // actual contact.  History belongs to the trail layer, position
      // belongs to the dot.
      const latestByDrone = new Map<string, DroneObservation>();
      for (const o of observations) {
        const prev = latestByDrone.get(o.drone_id);
        if (!prev || Date.parse(o.received_at) > Date.parse(prev.received_at)) {
          latestByDrone.set(o.drone_id, o);
        }
      }
      src?.setData(observationsToGeoJSON([...latestByDrone.values()]));
      const trailSrc = m.getSource("trails") as GeoJSONSource | undefined;
      trailSrc?.setData(trailsToGeoJSON(observations));
      if (!fittedRef.current) {
        // Fit to everything the operator must see at once: the drones,
        // the restricted volumes, and the stations.  Fitting to drones
        // alone pushed an alerted zone off the edge of the screen — an
        // alert pointing at something invisible is worse than no alert.
        const lons: number[] = [];
        const lats: number[] = [];
        for (const o of observations.slice(0, 20)) {
          lons.push(o.position.lon);
          lats.push(o.position.lat);
        }
        for (const z of zones) {
          for (const [zLon, zLat] of z.polygon ?? []) {
            lons.push(zLon);
            lats.push(zLat);
          }
        }
        for (const s of stations) {
          lons.push(s.lon);
          lats.push(s.lat);
        }
        if (lons.length === 0) return;
        const bounds: [[number, number], [number, number]] = [
          [Math.min(...lons), Math.min(...lats)],
          [Math.max(...lons), Math.max(...lats)],
        ];
        // maxZoom 13 rather than 14: at 14 the ~2 km coverage rings
        // extend past the viewport and read as stray arcs instead of
        // circles.  One zoom level out and the deployment geometry
        // (which station covers which zone) is legible.
        m.fitBounds(bounds, { padding: 70, duration: 0, maxZoom: 13 });
        fittedRef.current = true;
      }
    };
    whenSourceReady(m, "drones", apply);
  }, [observations, zones, stations]);

  // Push the selection ring.
  useEffect(() => {
    const m = mapRef.current;
    if (!m) return;
    const apply = () => {
      const src = m.getSource("selected") as GeoJSONSource | undefined;
      if (!src) return;
      const hit = selectedDroneId
        ? observations.find((o) => o.drone_id === selectedDroneId)
        : undefined;
      src.setData({
        type: "FeatureCollection",
        features: hit
          ? [
              {
                type: "Feature",
                geometry: {
                  type: "Point",
                  coordinates: [hit.position.lon, hit.position.lat],
                },
                properties: {},
              },
            ]
          : [],
      });
    };
    whenSourceReady(m, "selected", apply);
  }, [selectedDroneId, observations]);

  return <div ref={containerRef} className="map-container" />;
}
