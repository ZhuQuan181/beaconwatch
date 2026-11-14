import { useEffect, useState } from "react";
import { api } from "../api/client";

interface Props {
  pollMs?: number;
}

export function ConnectionBadge({ pollMs = 10_000 }: Props) {
  const [online, setOnline] = useState<"unknown" | "ok" | "down">("unknown");

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;

    function schedule() {
      if (!alive || document.visibilityState === "hidden") return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void tick(), pollMs);
    }

    async function tick() {
      if (!alive || document.visibilityState === "hidden") return;
      controller?.abort();
      const current = new AbortController();
      controller = current;
      try {
        await api.health(current.signal);
        if (alive && !current.signal.aborted) setOnline("ok");
      } catch {
        if (alive && !current.signal.aborted) setOnline("down");
      } finally {
        if (controller === current) controller = null;
        schedule();
      }
    }

    function onVisibilityChange() {
      if (document.visibilityState === "hidden") {
        controller?.abort();
        controller = null;
        if (timer) clearTimeout(timer);
        timer = null;
        return;
      }
      if (timer) clearTimeout(timer);
      timer = null;
      void tick();
    }

    void tick();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      alive = false;
      controller?.abort();
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [pollMs]);

  const label =
    online === "ok" ? "Server OK" : online === "down" ? "Server DOWN" : "…";
  return <span className={`conn-badge conn-${online}`}>{label}</span>;
}
