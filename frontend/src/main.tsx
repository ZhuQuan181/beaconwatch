import ReactDOM from "react-dom/client";
import { App } from "./App";

// NOTE: StrictMode is intentionally OFF.
//
// Why: StrictMode double-invokes effects in dev, which breaks two
// things for this app:
//
//   1. useObservationsStream's WebSocket would be opened AND closed
//      immediately, leaving the user staring at "WS offline".
//   2. MapView's map init would run twice and the cleanup would
//      remove the first instance — easy to leave the canvas in a
//      torn state.  We also fight this with a mount-once effect,
//      but disabling StrictMode removes the footgun for any future
//      effect added to the tree.
//
// If we ever need to re-enable StrictMode for effect-related bug
// hunts, we must (a) make useObservationsStream idempotent under
// double-mount, and (b) make MapView's init effect handle the
// double-init race.  Both are doable, just not free.
async function bootstrap() {
  if (import.meta.env.VITE_MOCK === "1") {
    const { installMockApi } = await import("./mock/install");
    installMockApi();
  }
  ReactDOM.createRoot(document.getElementById("root")!).render(<App />);
}

void bootstrap();
