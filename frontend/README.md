# RID Frontend

Live drone-detection dashboard for the RID system.

## Stack

- **Vite 5** + **React 18** + **TypeScript 5** (strict)
- **MapLibre GL JS 4** (no API key — uses `demotiles.maplibre.org` style)
- Native `WebSocket` for the live observation feed

## Run

```bash
# In dev (proxies /api and /ws to localhost:8000)
npm install
npm run dev   # http://localhost:5173

# Production build
npm run build && npm run preview
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
