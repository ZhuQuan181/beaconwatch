# BeaconWatch Frontend

Live drone-detection dashboard for BeaconWatch.

## Stack

- **Vite 8** + **React 18** + **TypeScript 5** (strict)
- **MapLibre GL JS 6** (no API key — uses an Esri dark raster basemap)
- Native `WebSocket` for the live observation feed

## Run

```bash
# In dev (proxies /api and /ws to localhost:8000)
npm install
npm run dev   # http://localhost:5173

# Production build
npm run build && npm run preview

# Security + production bundle check
npm audit --omit=dev

# Static demo build with in-browser mock data (no backend)
VITE_MOCK=1 npm run build:mock
```

The dev server expects the FastAPI server to be running on
`http://localhost:8000` (override via Vite proxy if needed).

## Layout

```
src/
├── App.tsx                     # Top-level layout: topbar + sidebar + map
├── main.tsx                    # ReactDOM bootstrap
├── styles.css                  # Dark theme
├── types/index.ts              # Wire types (mirror server models)
├── api/client.ts               # REST helpers
├── hooks/
│   ├── useObservationsStream.ts  # WS + backoff
│   └── usePresence.ts            # Stations + drones polling
└── components/
    ├── MapView.tsx                # MapLibre canvas + GeoJSON sources
    ├── ObservationList.tsx        # Live list (one row per drone)
    └── ConnectionBadge.tsx        # Server health pill
```

The history/unidentified refresh is throttled inside `App.tsx`, so a busy live
stream does not issue a REST request for every WebSocket frame.
