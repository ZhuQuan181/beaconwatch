import type { Alert } from "../types";

/**
 * AlertPanel — the operator's action list.
 *
 * Ordering is by severity first, then recency: a critical
 * "unidentified drone" must outrank a fresh altitude warning, because
 * the whole point of the panel is "what do I look at right now".
 *
 * Each row leads with the drone ID in mono, because that's the token an
 * operator reads off a radio call. The rule tag is secondary.
 */
const SEVERITY_ORDER: Record<string, number> = {
  critical: 0,
  warn: 1,
  info: 2,
};

const RULE_LABEL: Record<string, string> = {
  geofence: "禁飞区",
  altitude: "超限高",
  unidentified: "未广播 ID",
};

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleTimeString("zh-CN", { hour12: false });
}

export function AlertPanel({
  alerts,
  onSelectDrone,
}: {
  alerts: Alert[];
  onSelectDrone?: (droneId: string) => void;
}) {
  const sorted = [...alerts].sort((a, b) => {
    const sa = SEVERITY_ORDER[a.severity] ?? 3;
    const sb = SEVERITY_ORDER[b.severity] ?? 3;
    if (sa !== sb) return sa - sb;
    return Date.parse(b.at || "") - Date.parse(a.at || "");
  });

  const criticalCount = sorted.filter((a) => a.severity === "critical").length;

  return (
    <div className="alert-panel">
      <div className="alert-head">
        <h2>告警</h2>
        <span className="alert-count">
          {sorted.length === 0
            ? "无"
            : criticalCount > 0
              ? `${criticalCount} 严重 / ${sorted.length}`
              : `${sorted.length}`}
        </span>
      </div>

      {sorted.length === 0 ? (
        <p className="alert-empty">当前没有告警。</p>
      ) : (
        <ul className="alert-list">
          {sorted.slice(0, 30).map((a) => (
            <li
              key={a.alert_id}
              className={`alert-row sev-${a.severity}`}
              onClick={() => onSelectDrone?.(a.drone_id)}
              title={a.message}
            >
              <span className={`alert-mark sev-${a.severity}`} aria-hidden="true" />
              <div className="alert-body">
                <div className="alert-line-1">
                  <span className="alert-drone">{a.drone_id}</span>
                  <span className="alert-rule">{RULE_LABEL[a.rule] ?? a.rule}</span>
                </div>
                <div className="alert-line-2">{a.message}</div>
                <div className="alert-line-3">{fmtTime(a.at)}</div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
