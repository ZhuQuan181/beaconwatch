import { useEffect, useState } from "react";
import { api } from "../api/client";

interface Props {
  pollMs?: number;
}

export function ConnectionBadge({ pollMs = 10_000 }: Props) {
  const [online, setOnline] = useState<"unknown" | "ok" | "down">("unknown");

  useEffect(() => {
    let alive = true;
    const tick = () => {
      api
        .health()
        .then(() => alive && setOnline("ok"))
        .catch(() => alive && setOnline("down"));
    };
    tick();
    const h = setInterval(tick, pollMs);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [pollMs]);

  const label =
    online === "ok" ? "Server OK" : online === "down" ? "Server DOWN" : "…";
  return <span className={`conn-badge conn-${online}`}>{label}</span>;
}
