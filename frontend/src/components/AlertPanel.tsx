import { useState } from "react";
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
  onAcknowledge,
}: {
  alerts: Alert[];
  onSelectDrone?: (droneId: string) => void;
  onAcknowledge?: (
    alertId: string,
    acknowledgedBy: string,
    note?: string,
  ) => Promise<Alert>;
}) {
  const [operator, setOperator] = useState(() => {
    try {
      return window.localStorage.getItem("rid.operator") ?? "operator";
    } catch {
      return "operator";
    }
  });
  const [note, setNote] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [ackError, setAckError] = useState<string | null>(null);

  const sorted = [...alerts].sort((a, b) => {
    const sa = SEVERITY_ORDER[a.severity] ?? 3;
    const sb = SEVERITY_ORDER[b.severity] ?? 3;
    if (sa !== sb) return sa - sb;
    const aa = a.acknowledged_at ? 1 : 0;
    const ab = b.acknowledged_at ? 1 : 0;
    if (aa !== ab) return aa - ab;
    return Date.parse(b.at || "") - Date.parse(a.at || "");
  });

  const openAlerts = sorted.filter((alert) => !alert.acknowledged_at);
  const criticalCount = openAlerts.filter(
    (alert) => alert.severity === "critical",
  ).length;

  const persistOperator = (value: string) => {
    setOperator(value);
    try {
      window.localStorage.setItem("rid.operator", value);
    } catch {
      /* private-mode storage failure must not block acknowledgement */
    }
  };

  const handleAcknowledge = async (
    event: React.MouseEvent<HTMLButtonElement>,
    alertId: string,
  ) => {
    event.stopPropagation();
    if (!onAcknowledge || busyId) return;
    const actor = operator.trim();
    if (!actor) {
      setAckError("请先填写处置人");
      return;
    }
    setBusyId(alertId);
    setAckError(null);
    try {
      await onAcknowledge(alertId, actor, note.trim());
    } catch (error) {
      setAckError(`确认失败：${(error as Error).message}`);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="alert-panel">
      <div className="alert-head">
        <h2>告警</h2>
        <span className="alert-count">
          {sorted.length === 0
            ? "无"
            : criticalCount > 0
              ? `${criticalCount} 严重 · ${openAlerts.length} 待处置 / ${sorted.length}`
              : `${openAlerts.length} 待处置 / ${sorted.length}`}
        </span>
      </div>

      {onAcknowledge && (
        <div className="alert-tools">
          <label>
            <span>处置人</span>
            <input
              value={operator}
              onChange={(event) => persistOperator(event.target.value)}
              onBlur={() => {
                try {
                  window.localStorage.setItem("rid.operator", operator.trim());
                } catch {
                  /* ignore storage failure */
                }
              }}
              maxLength={128}
              aria-label="告警处置人"
            />
          </label>
          <label className="alert-note-field">
            <span>备注</span>
            <input
              value={note}
              onChange={(event) => setNote(event.target.value)}
              maxLength={1000}
              placeholder="可选"
              aria-label="告警处置备注"
            />
          </label>
        </div>
      )}
      {ackError && (
        <p className="alert-error" role="alert">
          {ackError}
        </p>
      )}

      {sorted.length === 0 ? (
        <p className="alert-empty">当前没有告警。</p>
      ) : (
        <ul className="alert-list">
          {sorted.slice(0, 30).map((a) => (
            <li
              key={a.alert_id}
              className={`alert-row sev-${a.severity} ${
                a.acknowledged_at ? "acked" : ""
              }`}
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
                <div className="alert-line-3">
                  <span>{fmtTime(a.at)}</span>
                  {(a.trigger_count ?? 1) > 1 && (
                    <span> · 第 {a.trigger_count} 次</span>
                  )}
                  {a.acknowledged_at ? (
                    <span className="alert-ack-meta">
                      {" "}
                      · 已确认 {a.acknowledged_by} ·{" "}
                      {fmtTime(a.acknowledged_at)}
                      {a.acknowledgement_note
                        ? ` · ${a.acknowledgement_note}`
                        : ""}
                    </span>
                  ) : (
                    onAcknowledge && (
                      <button
                        type="button"
                        className="alert-ack-button"
                        disabled={busyId !== null}
                        onClick={(event) =>
                          void handleAcknowledge(event, a.alert_id)
                        }
                      >
                        {busyId === a.alert_id ? "确认中…" : "确认"}
                      </button>
                    )
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
