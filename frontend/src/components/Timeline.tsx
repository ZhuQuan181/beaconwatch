import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DroneObservation } from "../types";

/**
 * Timeline — scrub back through history, or snap back to live.
 *
 * Design decisions that matter for an operator:
 *
 * 1. Scrubbing is a *mode*, not a query.  The moment you drag, the live
 *    WebSocket stream stops driving the map and the header switches to
 *    REPLAY.  Otherwise a live message would overwrite the frame you
 *    just scrubbed to and the timeline would feel broken.
 *
 * 2. The window is anchored to real data (min/max received_at of what
 *    the server returned), not to `Date.now()`.  A demo replaying a
 *    recording from last week would otherwise show a slider sitting at
 *    the far right with nothing under it.
 *
 * 3. "Jump to alert" goes to the earliest *unidentified* observation —
 *    the thing an operator actually needs to find.  A timeline you can
 *    only scrub by hand is a toy.
 */
export interface TimelineProps {
  /** Full ordered history (oldest → newest) used to define the window. */
  history: DroneObservation[];
  /** Observation id (or timestamp) currently being viewed, null = live. */
  cursorTs: string | null;
  onSeek: (ts: string | null) => void;
  unidentifiedDroneIds: string[];
}

function fmtClock(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleTimeString("zh-CN", { hour12: false });
}

export function Timeline({
  history,
  cursorTs,
  onSeek,
  unidentifiedDroneIds,
}: TimelineProps) {
  const [playing, setPlaying] = useState(false);
  const playRef = useRef<number | null>(null);

  // Window bounds come from data, not the wall clock.
  const { t0, t1 } = useMemo(() => {
    if (!history.length) return { t0: 0, t1: 0 };
    let min = Infinity;
    let max = -Infinity;
    for (const o of history) {
      const t = Date.parse(o.received_at);
      if (Number.isNaN(t)) continue;
      if (t < min) min = t;
      if (t > max) max = t;
    }
    if (!Number.isFinite(min) || !Number.isFinite(max)) return { t0: 0, t1: 0 };
    return { t0: min, t1: Math.max(max, min + 1000) };
  }, [history]);

  const isLive = cursorTs === null;
  const current = isLive ? t1 : Date.parse(cursorTs);

  // Earliest unidentified fix — the "where did the ghost appear" answer.
  const firstGhost = useMemo(() => {
    if (!unidentifiedDroneIds.length) return null;
    const ids = new Set(unidentifiedDroneIds);
    let best: number | null = null;
    for (const o of history) {
      if (!ids.has(o.drone_id)) continue;
      const t = Date.parse(o.received_at);
      if (Number.isNaN(t)) continue;
      if (best === null || t < best) best = t;
    }
    return best;
  }, [history, unidentifiedDroneIds]);

  const seekTo = useCallback(
    (t: number) => {
      const clamped = Math.min(Math.max(t, t0), t1);
      onSeek(new Date(clamped).toISOString());
    },
    [t0, t1, onSeek],
  );

  // Playback: advance the cursor in wall-clock steps between the data
  // bounds, stopping at the end rather than looping (a loop hides
  // "you've reached the end of the recording").
  useEffect(() => {
    if (!playing) return;
    const stepMs = Math.max(250, (t1 - t0) / 300);
    playRef.current = window.setInterval(() => {
      const next = (Date.parse(cursorTs ?? new Date(t1).toISOString()) || t0) + stepMs;
      if (next >= t1) {
        onSeek(new Date(t1).toISOString());
        setPlaying(false);
        return;
      }
      onSeek(new Date(next).toISOString());
    }, 250);
    return () => {
      if (playRef.current) window.clearInterval(playRef.current);
      playRef.current = null;
    };
  }, [playing, cursorTs, t0, t1, onSeek]);

  if (!history.length || t0 === 0) {
    return (
      <div className="timeline timeline--empty">
        <span className="tl-hint">还没有历史数据，时间轴在收到观测后出现。</span>
      </div>
    );
  }

  const span = t1 - t0;
  const pct = span > 0 ? ((current - t0) / span) * 100 : 100;

  return (
    <div className={`timeline ${isLive ? "timeline--live" : "timeline--replay"}`}>
      <div className="tl-left">
        <button
          type="button"
          className={`tl-mode ${isLive ? "is-live" : "is-replay"}`}
          onClick={() => onSeek(isLive ? new Date(t1).toISOString() : null)}
          title={isLive ? "点击进入历史回放" : "返回实时"}
        >
          <span className={`tl-dot ${isLive ? "live" : "replay"}`} />
          {isLive ? "实时" : "回放"}
        </button>
      </div>

      <div className="tl-track">
        <input
          type="range"
          min={0}
          max={1000}
          value={Math.round(Math.min(1000, Math.max(0, pct * 10)))}
          onChange={(e) => {
            setPlaying(false);
            seekTo(t0 + (Number(e.target.value) / 1000) * span);
          }}
          aria-label="时间轴"
        />
        {firstGhost !== null && span > 0 && (
          <button
            type="button"
            className="tl-ghost"
            style={{ left: `${((firstGhost - t0) / span) * 100}%` }}
            onClick={() => {
              setPlaying(false);
              seekTo(firstGhost);
            }}
            title={`跳到首个未识别目标 ${fmtClock(new Date(firstGhost).toISOString())}`}
          >
            <span className="tl-ghost-mark" />
          </button>
        )}
      </div>

      <div className="tl-right">
        <span className="tl-clock" title={new Date(current).toISOString()}>
          {fmtClock(new Date(current).toISOString())}
        </span>
        <button
          type="button"
          className="tl-play"
          onClick={() => {
            if (isLive) seekTo(t0);
            setPlaying((p) => !p);
          }}
          title={playing ? "暂停" : "播放"}
        >
          {playing ? "❚❚" : "▶"}
        </button>
      </div>
    </div>
  );
}
