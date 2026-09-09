import { useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

import "maplibre-gl/dist/maplibre-gl.css";
import type {
  Feature,
  FeatureCollection,
  Point,
  LineString,
} from "geojson";
import "./App.css";

maplibregl.setWorkerUrl(workerUrl);
const EARTH_RADIUS_M = 6_371_000;
const MAX_EXTRAP_S = 30;
const STALE_AFTER_S = 20;
const EXTRAPOLATION_INTERVAL_MS = 100;
const RECONCILIATION_MS = 500;
const SNAP_DISTANCE_M = 2_000;


type AircraftRow = [
  string, // icao24
  number | null, // latitude
  number | null, // longitude
  number | null, // baro_altitude
  number | null, // true_track
  number | null, // velocity
  number | null, // vertical_rate
  boolean, // on_ground
  number | null, // time_position
  number, // last_contact
  string | null, // callsign
];


type SnapshotFrame = {
  t: "snapshot";
  seq: number;
  server_time: number;
  fields: string[];
  aircraft: AircraftRow[];
};


type DeltaFrame = {
  t: "delta";
  seq: number;
  server_time: number;
  enter: AircraftRow[];
  update: AircraftRow[];
  leave: string[];
};


type LiveFrame = SnapshotFrame | DeltaFrame;


function rowToFeature(
  row: AircraftRow,
): Feature<Point> | null {
  const [
    icao24,
    latitude,
    longitude,
    baroAltitude,
    track,
    velocity,
    verticalRate,
    onGround,
    timePosition,
    lastContact,
    callsign,
  ] = row;

  if (latitude === null || longitude === null) {
    return null;
  }

  return {
    type: "Feature",
    id: icao24,

    geometry: {
      type: "Point",
      coordinates: [
        longitude,
        latitude,
      ],
    },

    properties: {
      icao24,
      callsign,
      baro_alt: baroAltitude,
      track,
      velocity,
      vrate: verticalRate,
      on_ground: onGround,
      time_position: timePosition,
      last_contact: lastContact,
    },
  };
}


function featureCollection(
  features: Feature<Point>[],
): FeatureCollection<Point> {
  return {
    type: "FeatureCollection",
    features,
  };
}


type SelectedAircraft = {
  icao24: string;
  properties: NonNullable<Feature<Point>["properties"]>;
  ageS: number;
};

type TrackResponse = {
  points: { longitude: number | null; latitude: number | null }[];
  truncated: boolean;
};

const EMPTY_TRACK: FeatureCollection<LineString | Point> = {
  type: "FeatureCollection",
  features: [],
};

function trackFeatures(track: TrackResponse): FeatureCollection<LineString | Point> {
  // The endpoint orders observations newest first; GeoJSON uses longitude, latitude.
  const coordinates = track.points.filter((point) =>
    typeof point.longitude === "number" && Number.isFinite(point.longitude) &&
    Math.abs(point.longitude) <= 180 &&
    typeof point.latitude === "number" && Number.isFinite(point.latitude) &&
    Math.abs(point.latitude) <= 90,
  ).reverse().map((point) => [point.longitude!, point.latitude!]);
  const features: Feature<LineString | Point>[] = [];
  if (coordinates.length >= 2) {
    features.push({
      type: "Feature", properties: {},
      geometry: { type: "LineString", coordinates },
    });
  }
  if (coordinates.length) {
    features.push({
      type: "Feature", properties: {},
      geometry: { type: "Point", coordinates: coordinates[coordinates.length - 1] },
    });
  }
  return { type: "FeatureCollection", features };
}

function formatMeasurement(value: unknown, unit: string, digits = 0) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toLocaleString(undefined, { maximumFractionDigits: digits })} ${unit}`
    : "Unavailable";
}


function App() {
  const [connection, setConnection] = useState<"connecting" | "connected" | "disconnected" | "error">("connecting");
  const [mapReady, setMapReady] = useState(false);
  const [mapError, setMapError] = useState("");
  const [feedError, setFeedError] = useState("");
  const [summary, setSummary] = useState({ visible: 0, live: 0, stale: 0, frameAge: -1 });
  const [viewport, setViewport] = useState("36.80° N · 119.50° W · Zoom 5.5");
  const selectedIdRef = useRef<string | null>(null);
  const [selectedAircraft, setSelectedAircraft] = useState<SelectedAircraft | null>(null);

  const [trackStatus, setTrackStatus] = useState({ icao24: "", message: "" });
  const selectedIcao24 = selectedAircraft?.icao24 ?? null;

  function closeAircraftPanel() {
    selectedIdRef.current = null;
    setSelectedAircraft(null);
  
    setTrackStatus({
      icao24: "",
      message: "",
    });
  }

  const mapContainer = useRef<HTMLDivElement | null>(
    null,
  );

  const mapRef = useRef<maplibregl.Map | null>(
    null,
  );

  const wsRef = useRef<WebSocket | null>(
    null,
  );
  const serverOffsetMsRef = useRef(0);

  const aircraftRef = useRef<
    Map<string, Feature<Point>>
  >(new Map());


  useEffect(() => {
    if (!mapContainer.current) {
      return;
    }

    if (mapRef.current) {
      return;
    }

    const aircraftStore = aircraftRef.current;

    const map = new maplibregl.Map({
      container: mapContainer.current,

      style: {
        version: 8,

        sources: {
          osm: {
            type: "raster",

            tiles: [
              "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            ],

            tileSize: 256,

            attribution:
              "© OpenStreetMap contributors",
          },
        },

        layers: [
          {
            id: "map-background",
            type: "background",
            paint: { "background-color": "#050505" },
          },
          {
            id: "osm",
            type: "raster",
            source: "osm",
            paint: {
              // Keep the background dark while brightening map labels and details.
              "raster-saturation": -1,
              "raster-brightness-min": 0.65,
              "raster-brightness-max": 0.02,
            },
          },
        ],
      },

      center: [
        -119.5,
        36.8,
      ],

      zoom: 5.5,
    });


    mapRef.current = map;


    map.addControl(
      new maplibregl.NavigationControl(),
      "top-right",
    );


    // Rendering state is separate from the authoritative WebSocket features.
    const corrections = new Map<string, {
      longitude: number;
      latitude: number;
      startedAt: number;
    }>();
    let sourceFeatures = new Map<string, Feature<Point>>();
    let renderedFeatures = new Map<string, Feature<Point>>();
    let pendingSnapshot: Map<string, Feature<Point>> | null = null;
    let framePending = false;
    let inFlight = false;
    let disposed = false;
    let reconnectTimer: number | undefined;
    let reconnectAttempt = 0;
    let lastFrameAt: number | null = null;
    // Opt-in local acceptance measurement; no per-aircraft React state or profiler.
    const measurePerformance = new URLSearchParams(window.location.search).has("perf");
    let renderSamples: number[] = [];
    let skippedRenderTicks = 0;
    let renderWindowStarted = performance.now();

    // Query only once per second, deduplicating symbols across tiles/world copies.
    // React receives scalar summaries, never the live aircraft collection.
    function refreshSummary() {
      if (disposed) return;
      const ids = new Set<string>();
      if (map.getLayer("aircraft-symbols")) {
        for (const feature of map.queryRenderedFeatures({ layers: ["aircraft-symbols"] })) {
          ids.add(String(feature.properties?.icao24 ?? feature.id));
        }
      }
      let live = 0;
      let stale = 0;
      for (const id of ids) {
        const feature = aircraftRef.current.get(id);
        if (!feature) continue;
        if (serverNowMs() / 1000 - feature.properties!.last_contact >= STALE_AFTER_S) stale++;
        else live++;
      }
      const frameAge = lastFrameAt === null ? -1 : Math.floor((performance.now() - lastFrameAt) / 1000);
      setSummary((previous) => previous.live === live && previous.stale === stale && previous.frameAge === frameAge
        ? previous : { visible: live + stale, live, stale, frameAge });
    }

    function refreshViewport() {
      const center = map.getCenter().wrap();
      setViewport(`${Math.abs(center.lat).toFixed(2)}° ${center.lat >= 0 ? "N" : "S"} · ${Math.abs(center.lng).toFixed(2)}° ${center.lng >= 0 ? "E" : "W"} · Zoom ${map.getZoom().toFixed(1)}`);
      refreshSummary();
    }

    map.on("error", () => {
      if (!disposed) setMapError("Some map content could not load. Check your connection and reload if it persists.");
    });

    async function updateMapSource() {
      if (disposed) return;
      if (inFlight) {
        if (measurePerformance) skippedRenderTicks++;
        return;
      }
      const renderStarted = performance.now();
      const source = map.getSource(
        "aircraft",
      ) as maplibregl.GeoJSONSource | undefined;
      if (!source) return;

      inFlight = true;
      framePending = false;
      const snapshot = pendingSnapshot;
      pendingSnapshot = null;
      const target = snapshot ?? new Map(aircraftRef.current);
      try {
        const now = serverNowMs();
        const animationNow = performance.now();
        const renderedTarget = new Map(
          [...target].map(([id, feature]) => [
            id, renderFeature(feature, now, animationNow),
          ]),
        );
        if (snapshot) {
          await source.setData(featureCollection([...renderedTarget.values()]));
        } else {
          const diff: maplibregl.GeoJSONSourceDiff = {
            remove: [...sourceFeatures.keys()].filter((id) => !target.has(id)),
            add: [],
            update: [],
          };
          for (const [id, feature] of target) {
            const rendered = renderedTarget.get(id)!;
            const previous = sourceFeatures.get(id);
            if (!previous) {
              diff.add!.push(rendered);
            } else {
              diff.update!.push({
                id,
                newGeometry: rendered.geometry,
                addOrUpdateProperties: previous !== feature
                  ? Object.entries(rendered.properties ?? {})
                    .map(([key, value]) => ({ key, value }))
                  : [{ key: "stale", value: rendered.properties!.stale }],
              });
            }
          }
          if (diff.remove!.length || diff.add!.length || diff.update!.length) {
            await source.updateData(diff);
          }
        }
        if (!disposed) {
          setMapError((previous) => previous === "Aircraft rendering interrupted. Retrying automatically…" ? "" : previous);
        }
        sourceFeatures = target;
        renderedFeatures = renderedTarget;
      } catch (error) {
        console.error("Aircraft source update failed", error);
        if (!disposed) setMapError("Aircraft rendering interrupted. Retrying automatically…");
        // Re-establish a known source state on the next tick after a failure.
        pendingSnapshot ??= new Map(aircraftRef.current);
        framePending = false;
      } finally {
        inFlight = false;
        if (measurePerformance) {
          renderSamples.push(performance.now() - renderStarted);
          if (renderSamples.length >= 100) {
            const sorted = [...renderSamples].sort((a, b) => a - b);
            console.info("SkyWatch render performance", JSON.stringify({
              aircraft: target.size, samples: sorted.length,
              elapsed_ms: performance.now() - renderWindowStarted,
              update_ms_p50: sorted[Math.ceil(sorted.length * 0.5) - 1],
              update_ms_p95: sorted[Math.ceil(sorted.length * 0.95) - 1],
              update_ms_max: sorted[sorted.length - 1], skipped_ticks: skippedRenderTicks,
            }));
            renderSamples = [];
            skippedRenderTicks = 0;
            renderWindowStarted = performance.now();
          }
        }
        // Only authoritative frames need an immediate follow-up; visual ticks drop.
        if (!disposed && framePending) void updateMapSource();
      }
    }

    function updateServerClock(
      serverTimeSeconds: number,
    ) {
      const observedOffset =
        serverTimeSeconds * 1000 - Date.now();
    
      serverOffsetMsRef.current =
        0.9 * serverOffsetMsRef.current +
        0.1 * observedOffset;
    }
    
    
    function serverNowMs() {
      return (
        Date.now() +
        serverOffsetMsRef.current
      );
    }

    // Only the selected feature's properties enter React state, never the collection.
    function refreshSelection() {
      const id = selectedIdRef.current;
      if (id === null) return;
      const properties = aircraftRef.current.get(id)?.properties;
      if (!properties) {
        selectedIdRef.current = null;
        setSelectedAircraft(null);
        return;
      }
      const ageS = Math.max(0, serverNowMs() / 1000 - properties.last_contact);
      setSelectedAircraft({ icao24: id, properties, ageS });
    }

    function selectAircraft(event: maplibregl.MapLayerMouseEvent) {
      const feature = event.features?.[0];
      const id = feature?.properties?.icao24 ?? feature?.id;
    
      if (id == null) {
        return;
      }
    
      const icao24 = String(id);
    
      selectedIdRef.current = icao24;
    
      setTrackStatus({
        icao24,
        message: "Loading recent track…",
      });
    
      refreshSelection();
    }

    function enterAircraft() {
      map.getCanvas().style.cursor = "pointer";
    }

    function leaveAircraft() {
      map.getCanvas().style.cursor = "";
    }

    map.on("click", "aircraft-symbols", selectAircraft);
    map.on("mouseenter", "aircraft-symbols", enterAircraft);
    map.on("mouseleave", "aircraft-symbols", leaveAircraft);

    function applyFrame(
      frame: LiveFrame,
    ) {
      updateServerClock(
        frame.server_time,
      );
      if (frame.t === "snapshot") {
        aircraftRef.current.clear();
        corrections.clear();

        for (const row of frame.aircraft) {
          const feature = rowToFeature(row);

          if (feature === null) {
            continue;
          }

          aircraftRef.current.set(
            String(feature.id),
            feature,
          );
        }

        refreshSelection();
        pendingSnapshot = new Map(aircraftRef.current);
        framePending = true;
        void updateMapSource();

        return;
      }


      for (const icao24 of frame.leave) {
        corrections.delete(icao24);
        aircraftRef.current.delete(
          icao24,
        );
      }


      const now = serverNowMs();
      const animationNow = performance.now();
      for (const row of [...frame.enter, ...frame.update]) {
        const feature = rowToFeature(row);
        if (feature === null) continue;

        const id = String(feature.id);
        const previous = aircraftRef.current.get(id);
        // Contact-only updates must not restart a position correction.
        if (previous && (
          previous.properties?.time_position !== feature.properties?.time_position ||
          previous.geometry.coordinates[0] !== feature.geometry.coordinates[0] ||
          previous.geometry.coordinates[1] !== feature.geometry.coordinates[1]
        )) {
          const from = renderFeature(previous, now, animationNow).geometry.coordinates;
          const to = extrapolateFeature(feature, now).geometry.coordinates;
          const longitude = ((from[0] - to[0] + 540) % 360 + 360) % 360 - 180;
          const latitude = from[1] - to[1];
          const radians = Math.PI / 180;
          const haversine = Math.sin(latitude * radians / 2) ** 2 +
            Math.cos(from[1] * radians) * Math.cos(to[1] * radians) *
            Math.sin(longitude * radians / 2) ** 2;
          const distance = 2 * EARTH_RADIUS_M *
            Math.asin(Math.sqrt(Math.min(1, Math.max(0, haversine))));
          corrections.delete(id);
          if (distance < SNAP_DISTANCE_M) {
            corrections.set(id, { longitude, latitude, startedAt: animationNow });
          }
        }
        aircraftRef.current.set(id, feature);
      }

      refreshSelection();
      framePending = true;
      void updateMapSource();
    }


    function sendViewport(
      ws: WebSocket,
    ) {
      if (
        ws.readyState !== WebSocket.OPEN
      ) {
        return;
      }

      const bounds = map.getBounds();

      ws.send(
        JSON.stringify({
          t: "viewport",

          bbox: [
            bounds.getSouth(),
            bounds.getWest(),
            bounds.getNorth(),
            bounds.getEast(),
          ],
        }),
      );
    }

    
    function extrapolateFeature(
      feature: Feature<Point>,
      nowMs: number,
    ): Feature<Point> {
      const properties = feature.properties;

      if (!properties) {
        return feature;
      }

      if (nowMs / 1000 - properties.last_contact >= STALE_AFTER_S) {
        return feature;
      }

      const velocity = properties.velocity as number | null;
      const track = properties.track as number | null;
      const timePosition = properties.time_position as number | null;

      if (
        velocity === null ||
        track === null ||
        timePosition === null
      ) {
        return feature;
      }

      const [lon0, lat0] = feature.geometry.coordinates;
      const elapsedS = (nowMs - timePosition * 1000) / 1000;

      if (elapsedS <= 0) {
        return feature;
      }

      const dt = Math.min(elapsedS, MAX_EXTRAP_S);
      const trackRad = track * Math.PI / 180;
      const northM = velocity * dt * Math.cos(trackRad);
      const eastM = velocity * dt * Math.sin(trackRad);

      const lat =
        lat0 + (northM / EARTH_RADIUS_M) * (180 / Math.PI);
      const lon =
        lon0 +
        (eastM / (EARTH_RADIUS_M * Math.cos(lat0 * Math.PI / 180))) *
          (180 / Math.PI);

      return {
        ...feature,
        geometry: {
          type: "Point",
          coordinates: [lon, lat],
        },
      };
    }


    function renderFeature(
      feature: Feature<Point>,
      nowMs: number,
      animationNow: number,
    ): Feature<Point> {
      const id = String(feature.id);
      const stale = nowMs / 1000 - feature.properties!.last_contact >= STALE_AFTER_S;
      const predicted = {
        ...extrapolateFeature(feature, nowMs),
        properties: { ...feature.properties, stale },
      };
      if (stale) {
        corrections.delete(id);
        // Freeze the last submitted position, including any reconciliation offset.
        // Aircraft first seen stale stay at their authoritative position.
        return {
          ...predicted,
          geometry: renderedFeatures.get(id)?.geometry ?? feature.geometry,
        };
      }
      const correction = corrections.get(id);
      if (!correction) return predicted;
      const remaining = Math.max(
        0, 1 - (animationNow - correction.startedAt) / RECONCILIATION_MS,
      );
      if (remaining === 0) {
        corrections.delete(id);
        return predicted;
      }
      return {
        ...predicted,
        geometry: {
          type: "Point",
          coordinates: [
            predicted.geometry.coordinates[0] + correction.longitude * remaining,
            predicted.geometry.coordinates[1] + correction.latitude * remaining,
          ],
        },
      };
    }


    function clearLiveFeed() {
      aircraftRef.current.clear();
      corrections.clear();
      renderedFeatures.clear();
      lastFrameAt = null;
      pendingSnapshot = new Map();
      framePending = true;
      refreshSelection();
      refreshSummary();
      // Serialize the empty snapshot behind any worker update already in flight.
      void updateMapSource();
    }

    function connectWebSocket() {
      if (disposed) return;
      setConnection("connecting");
      let socketFailed = false;
      const ws = new WebSocket(
        "ws://localhost:8000/ws/live",
      );

      wsRef.current = ws;


      ws.addEventListener(
        "open",
        () => {
          if (disposed) return;
          setConnection("connected");
          console.log(
            "SkyWatch WebSocket connected",
          );

          sendViewport(ws);
        },
      );


      ws.addEventListener(
        "message",
        (event) => {
          if (disposed) return;
          try {
            const message = JSON.parse(
              event.data,
            ) as unknown;

            if (
              typeof message !== "object" ||
              message === null ||
              !("t" in message)
            ) {
              return;
            }

            const frame = message as {
              t: string;
            };

            if (
              frame.t !== "snapshot" &&
              frame.t !== "delta"
            ) {
              console.warn(
                "Ignoring WebSocket message:",
                message,
              );

              return;
            }

            applyFrame(
              message as LiveFrame,
            );
            reconnectAttempt = 0;
            lastFrameAt = performance.now();
            setFeedError("");
          } catch {
            setFeedError("Could not read a live update. Waiting for the next valid update…");
          }
        },
      );


      ws.addEventListener(
        "close",
        () => {
          if (disposed) return;
          wsRef.current = null;
          clearLiveFeed();
          setConnection(socketFailed ? "error" : "disconnected");
          // Equal jitter avoids synchronized retries; reset after a valid frame.
          const cap = Math.min(30_000, 1000 * 2 ** Math.min(reconnectAttempt++, 5));
          reconnectTimer = window.setTimeout(connectWebSocket, cap * (0.5 + Math.random() * 0.5));
          console.log(
            "SkyWatch WebSocket disconnected",
          );
        },
      );


      ws.addEventListener(
        "error",
        () => {
          if (disposed) return;
          socketFailed = true;
          clearLiveFeed();
          ws.close();
          setConnection("error");
          console.error(
            "SkyWatch WebSocket error",
          );
        },
      );
    }


    function addAircraftLayer() {
      if (
        map.getLayer(
          "aircraft-symbols",
        )
      ) {
        return;
      }

      map.addLayer({
        id: "aircraft-symbols",

        type: "symbol",

        source: "aircraft",

        layout: {
          "icon-image": "plane",

          "icon-size": [
            "interpolate",
            ["linear"],
            ["zoom"],

            5,
            0.45,

            11,
            0.8,
          ],

          "icon-rotate": [
            "coalesce",
            ["get", "track"],
            0,
          ],

          "icon-rotation-alignment":
            "map",

          "icon-allow-overlap": true,

          "icon-ignore-placement": true,

          "text-field": [
            "coalesce",
            ["get", "callsign"],
            "",
          ],

          "text-size": 11,

          "text-offset": [
            0,
            1.5,
          ],

          "text-anchor": "top",

          "text-optional": true,

          "text-allow-overlap": false,
        },

        paint: {
          "icon-color": [
            "interpolate",
            ["linear"],

            [
              "coalesce",
              ["get", "baro_alt"],
              0,
            ],

            0,
            "#4ade80",

            3000,
            "#facc15",

            11000,
            "#f97316",
          ],

          "icon-opacity": ["case", ["get", "stale"], 0.35, 0.95],

          "text-opacity": ["case", ["get", "stale"], 0.35, 1.0],

          "text-color": "#ffffff",

          "text-halo-color":
            "#111827",

          "text-halo-width": 1.5,
        },
      });
    }


    map.on(
      "load",
      () => {
        map.addSource(
          "aircraft",
          {
            type: "geojson",

            data: {
              type: "FeatureCollection",
              features: [],
            },
          },
        );


        map.addSource("selected-track", { type: "geojson", data: EMPTY_TRACK });
        // Added before the aircraft symbols so live aircraft remain on top.
        map.addLayer({
          id: "selected-track-line",
          type: "line",
          source: "selected-track",
          filter: ["==", ["geometry-type"], "LineString"],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#38bdf8", "line-width": 3, "line-opacity": 0.85 },
        });
        map.addLayer({
          id: "selected-track-endpoint",
          type: "circle",
          source: "selected-track",
          filter: ["==", ["geometry-type"], "Point"],
          paint: {
            "circle-radius": 4, "circle-color": "#38bdf8",
            "circle-stroke-color": "#e0f2fe", "circle-stroke-width": 1.5,
          },
        });

        const planeSvg = `
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="64"
            height="64"
            viewBox="0 0 64 64"
          >
            <path
              d="
                M32 4
                L37 25
                L55 33
                L55 39
                L37 35
                L36 49
                L44 55
                L44 59
                L32 55
                L20 59
                L20 55
                L28 49
                L27 35
                L9 39
                L9 33
                L27 25
                Z
              "
              fill="#ffffff"
            />
          </svg>
        `;


        const planeImage =
          new Image();


        planeImage.onload = () => {
          if (disposed) return;
          if (
            !map.hasImage(
              "plane",
            )
          ) {
            map.addImage(
              "plane",
              planeImage,
              {
                sdf: true,
              },
            );
          }

          addAircraftLayer();
          setMapReady(true);
        };


        planeImage.onerror = () => {
          if (!disposed) setMapError("Aircraft icons could not load. Reload to try again.");
        };

        planeImage.src =
          "data:image/svg+xml;charset=utf-8," +
          encodeURIComponent(
            planeSvg,
          );


        connectWebSocket();
      },
    );


    map.on(
      "moveend",
      () => {
        refreshViewport();
        const ws =
          wsRef.current;

        if (ws !== null) {
          sendViewport(ws);
        }
      },
    );


    const extrapolationTimer = window.setInterval(
      () => {
        if (wsRef.current?.readyState === WebSocket.OPEN || pendingSnapshot !== null) {
          void updateMapSource();
        }
      },
      EXTRAPOLATION_INTERVAL_MS,
    );


    const selectionTimer = window.setInterval(() => {
      refreshSelection();
      refreshSummary();
    }, 1000);

    return () => {
      window.clearInterval(selectionTimer);
      map.off("click", "aircraft-symbols", selectAircraft);
      map.off("mouseenter", "aircraft-symbols", enterAircraft);
      map.off("mouseleave", "aircraft-symbols", leaveAircraft);
      disposed = true;
      window.clearTimeout(reconnectTimer);
      corrections.clear();
      renderedFeatures.clear();
      pendingSnapshot = null;
      window.clearInterval(extrapolationTimer);

      wsRef.current?.close();
      wsRef.current = null;

      aircraftStore.clear();

      map.remove();
      mapRef.current = null;
    };
  }, []);


  useEffect(() => {
    const map = mapRef.current;
    const source = map?.getSource("selected-track") as maplibregl.GeoJSONSource | undefined;
    if (!source) return;
    source.setData(EMPTY_TRACK);
    if (!selectedIcao24) return;

    const controller = new AbortController();
    const isCurrent = () => !controller.signal.aborted &&
      selectedIdRef.current === selectedIcao24;
    async function loadTrack() {
      try {
        const response = await fetch(`/aircraft/${encodeURIComponent(selectedIcao24!)}/track`, {
          signal: controller.signal,
        });
        if (!isCurrent()) return;
        if (response.status === 404) {
          setTrackStatus({ icao24: selectedIcao24!, message: "No recent track available." });
          return;
        }
        if (!response.ok) throw new Error(`Track request failed: ${response.status}`);
        const track: TrackResponse = await response.json();
        const data = trackFeatures(track);
        if (!isCurrent()) return;
        source!.setData(data);
        const message = data.features.length === 0
          ? "No recent track available."
          : data.features.length === 1
            ? "Only one recent position available; showing its marker."
            : "Recent track shown in blue; the dot marks the latest recorded position.";
        setTrackStatus({
          icao24: selectedIcao24!,
          message: message + (track.truncated ? " Track limited to the latest observations." : ""),
        });
      } catch {
        if (isCurrent()) {
          setTrackStatus({
            icao24: selectedIcao24!,
            message: "Could not load recent track. Select the aircraft again later to retry.",
          });
        }
      }
    }
    void loadTrack();
    return () => {
      controller.abort();
      if (mapRef.current === map && map?.getSource("selected-track")) {
        source.setData(EMPTY_TRACK);
      }
    };
  }, [selectedIcao24]);

  const operationalMessage = mapError || feedError || (
    connection === "error" ? "Live connection failed. Retrying automatically…" :
    connection === "disconnected" ? "Live connection closed. Aircraft cleared; reconnecting automatically…" :
    !mapReady ? "Loading map and aircraft symbols…" :
    connection === "connecting" ? "Connecting to the live aircraft feed…" :
    summary.frameAge < 0 ? "Connected. Waiting for the first aircraft snapshot…" :
    summary.frameAge >= STALE_AFTER_S ? "Live feed is quiet. Displayed positions may be out of date." :
    summary.visible === 0 ? "No aircraft in this view. Pan or zoom out to explore." : ""
  );

  return (
    <main className="app">
      <header className="topbar">
        <div>
          <h1>SkyWatch</h1>
          <p className="brand-caption">Live airspace explorer</p>
        </div>
        <div className={`connection connection-${connection}`} role="status">
          <i aria-hidden="true" /> WebSocket · {connection}
        </div>
        <dl className="flight-counts" aria-label="Aircraft in the current viewport">
          <div><dt>Visible aircraft</dt><dd>{summary.visible.toLocaleString()}</dd></div>
          <div className="count-live"><dt>LIVE</dt><dd>{summary.live.toLocaleString()}</dd></div>
          <div className="count-stale"><dt>STALE</dt><dd>{summary.stale.toLocaleString()}</dd></div>
        </dl>
      </header>

      <section className="map-status" aria-label="Map context and legend">
        <p className="viewport-context">Map center · {viewport}</p>
        <details className="map-legend">
          <summary>Map legend</summary>
          <div className="altitude-scale" aria-hidden="true" />
          <div className="altitude-labels"><span>0 m · green</span><span>3,000 m · yellow</span><span>11,000+ m · orange</span></div>
          <p>Barometric altitude · unknown altitude uses green.</p>
          <p>LIVE: contact within {STALE_AFTER_S}s. STALE: {STALE_AFTER_S}s or older, frozen at 35% opacity.</p>
        </details>
        <p className="feed-age">{summary.frameAge < 0 ? "Awaiting live data" : `Last feed update ${summary.frameAge}s ago`} · Counts refresh every second</p>
        {operationalMessage && <p className="operational-message" role="status">{operationalMessage}</p>}
      </section>

      <div
        ref={mapContainer}
        className="map"
      />

      {selectedAircraft && (
        <aside className="aircraft-panel" aria-labelledby="aircraft-title">
          <div className="aircraft-panel-header">
            <div>
              <p className="aircraft-eyebrow">Aircraft details</p>
              <h2 id="aircraft-title">
                {selectedAircraft.properties.callsign?.trim() || "Unknown callsign"}
              </h2>
              <p className="aircraft-identity">ICAO24 · {selectedAircraft.icao24.toUpperCase()}</p>
            </div>
            <button className="aircraft-close" type="button" onClick={closeAircraftPanel}
              aria-label="Close aircraft details" title="Close aircraft details">×</button>
          </div>
          <div className="aircraft-freshness">
            <span className={`aircraft-badge ${selectedAircraft.ageS >= STALE_AFTER_S ? "is-stale" : "is-live"}`}>
              {selectedAircraft.ageS >= STALE_AFTER_S ? "STALE" : "LIVE"}
            </span>
            <span>Last update {Math.floor(selectedAircraft.ageS)}s ago</span>
          </div>
          <dl className="aircraft-metrics">
            <div><dt>Barometric altitude</dt><dd>{formatMeasurement(selectedAircraft.properties.baro_alt, "m")}</dd></div>
            <div><dt>Ground speed</dt><dd>{formatMeasurement(selectedAircraft.properties.velocity, "m/s", 1)}</dd></div>
            <div><dt>True track</dt><dd>{formatMeasurement(selectedAircraft.properties.track, "°", 1)}</dd></div>
            <div><dt>Vertical rate</dt><dd>{formatMeasurement(selectedAircraft.properties.vrate, "m/s", 1)}</dd></div>
            <div><dt>On ground</dt><dd>{selectedAircraft.properties.on_ground === true ? "Yes" : selectedAircraft.properties.on_ground === false ? "No · Airborne" : "Unavailable"}</dd></div>
          </dl>
          <p className="aircraft-panel-note" role="status">
            {trackStatus.icao24 === selectedAircraft.icao24
              ? trackStatus.message : "Loading recent track…"}
          </p>
          <p className="aircraft-panel-note">
            {selectedAircraft.ageS >= STALE_AFTER_S
              ? "Updates delayed. The map holds the last rendered position."
              : "Receiving recent aircraft observations."}
          </p>
        </aside>
      )}
    </main>
  );
}


export default App;