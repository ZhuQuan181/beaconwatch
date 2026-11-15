import { useState } from "react";
import type { Alert, AlertEvent, AlertStatus } from "../types";

/**
 * AlertPanel — the operator's action list.
 *
 * Severity and lifecycle state drive the order: a critical open alert
 * outranks a fresh acknowledged warning, because the panel answers
 * "what needs attention right now". The filter keeps resolved history
 * available without letting it crowd the active queue.
 */
const SEVERITY_ORDER: Record<string, number> = {
  critical: 0,
  warn: 1,
  info: 2,
};

const STATUS_ORDER: Record<AlertStatus, number> = {
  open: 0,
  acknowledged: 1,
  resolved: 2,
};

const RULE_LABEL: Record<string, string> = {
  geofence: "禁飞区",
  altitude: "超限高",
  unidentified: "未广播 ID",
};

const STATUS_LABEL: Record<AlertStatus, string> = {
  open: "待处置",
  acknowledged: "已确认",
  resolved: "已解决",
};

const EVENT_LABEL: Record<AlertEvent["action"], string> = {
  triggered: "首次触发",
  acknowledged: "确认",
  resolved: "解决",
  reopened: "重新打开",
};

type AlertFilter = "active" | "resolved" | "all";
type AlertAction = "acknowledge" | "resolve" | "reopen";

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleTimeString("zh-CN", { hour12: false });
}

function statusOf(alert: Alert): AlertStatus {
  if (alert.status === "resolved" || alert.resolved_at) return "resolved";
  if (alert.status === "acknowledged" || alert.acknowledged_at) {
    return "acknowledged";
  }
  return "open";
}

function matchesFilter(alert: Alert, filter: AlertFilter): boolean {
  const status = statusOf(alert);
  if (filter === "active") return status !== "resolved";
  if (filter === "resolved") return status === "resolved";
  return true;
}

export function AlertPanel({
  alerts,
  onSelectDrone,
  onAcknowledge,
  onResolve,
  onReopen,
  onLoadHistory,
}: {
  alerts: Alert[];
  onSelectDrone?: (droneId: string) => void;
  onAcknowledge?: (
    alertId: string,
    acknowledgedBy: string,
    note?: string,
  ) => Promise<Alert>;
  onResolve?: (
    alertId: string,
    resolvedBy: string,
    note?: string,
  ) => Promise<Alert>;
  onReopen?: (
    alertId: string,
    reopenedBy: string,
    note?: string,
  ) => Promise<Alert>;
  onLoadHistory?: (alertId: string) => Promise<AlertEvent[]>;
}) {
  const [operator, setOperator] = useState(() => {
    try {
      return window.localStorage.getItem("rid.operator") ?? "operator";
    } catch {
      return "operator";
    }
  });
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<{ id: string; action: AlertAction } | null>(
    null,
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [filter, setFilter] = useState<AlertFilter>("active");
  const [historyOpenId, setHistoryOpenId] = useState<string | null>(null);
  const [historyLoadingId, setHistoryLoadingId] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyById, setHistoryById] = useState<
    Record<string, AlertEvent[]>
  >({});

  const sorted = [...alerts].sort((a, b) => {
    const sa = SEVERITY_ORDER[a.severity] ?? 3;
    const sb = SEVERITY_ORDER[b.severity] ?? 3;
    if (sa !== sb) return sa - sb;
    const statA = STATUS_ORDER[statusOf(a)];
    const statB = STATUS_ORDER[statusOf(b)];
    if (statA !== statB) return statA - statB;
    return Date.parse(b.at || "") - Date.parse(a.at || "");
  });

  const openAlerts = sorted.filter((alert) => statusOf(alert) === "open");
  const acknowledgedAlerts = sorted.filter(
    (alert) => statusOf(alert) === "acknowledged",
  );
  const resolvedAlerts = sorted.filter(
    (alert) => statusOf(alert) === "resolved",
  );
  const activeAlerts = [...openAlerts, ...acknowledgedAlerts];
  const criticalCount = openAlerts.filter(
    (alert) => alert.severity === "critical",
  ).length;
  const visible = sorted.filter((alert) => matchesFilter(alert, filter));
  const canAct = Boolean(onAcknowledge || onResolve || onReopen);

  const persistOperator = (value: string) => {
    setOperator(value);
    try {
      window.localStorage.setItem("rid.operator", value);
    } catch {
      /* private-mode storage failure must not block acknowledgement */
    }
  };

  const invalidateHistory = (alertId: string) => {
    setHistoryById((current) => {
      const next = { ...current };
      delete next[alertId];
      return next;
    });
  };

  const handleAction = async (
    event: React.MouseEvent<HTMLButtonElement>,
    alertId: string,
    action: AlertAction,
  ) => {
    event.stopPropagation();
    if (busy || !canAct) return;
    const actor = operator.trim();
    if (!actor) {
      setActionError("请先填写处置人");
      return;
    }

    setBusy({ id: alertId, action });
    setActionError(null);
    try {
      if (action === "acknowledge") {
        await onAcknowledge?.(alertId, actor, note.trim());
      } else if (action === "resolve") {
        await onResolve?.(alertId, actor, note.trim());
      } else {
        await onReopen?.(alertId, actor, note.trim());
      }
      invalidateHistory(alertId);
      if (historyOpenId === alertId && onLoadHistory) {
        setHistoryLoadingId(alertId);
        try {
          const rows = await onLoadHistory(alertId);
          setHistoryById((current) => ({ ...current, [alertId]: rows }));
        } catch (error) {
          setHistoryError(`审计记录加载失败：${(error as Error).message}`);
        } finally {
          setHistoryLoadingId(null);
        }
      }
      setNote("");
    } catch (error) {
      setActionError(`操作失败：${(error as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  const toggleHistory = async (
    event: React.MouseEvent<HTMLButtonElement>,
    alertId: string,
  ) => {
    event.stopPropagation();
    if (historyOpenId === alertId) {
      setHistoryOpenId(null);
      return;
    }
    setHistoryOpenId(alertId);
    setHistoryError(null);
    if (historyById[alertId] || !onLoadHistory) return;
    setHistoryLoadingId(alertId);
    try {
      const rows = await onLoadHistory(alertId);
      setHistoryById((current) => ({ ...current, [alertId]: rows }));
    } catch (error) {
      setHistoryError(`审计记录加载失败：${(error as Error).message}`);
    } finally {
      setHistoryLoadingId(null);
    }
  };

  const actionButtons = (alert: Alert) => {
    const status = statusOf(alert);
    const isBusy = busy?.id === alert.alert_id;
    const button = (
      action: AlertAction,
      label: string,
      onClick: (
        event: React.MouseEvent<HTMLButtonElement>,
        alertId: string,
        action: AlertAction,
      ) => Promise<void>,
    ) => (
      <button
        type="button"
        className={`alert-action-button action-${action}`}
        disabled={busy !== null}
        onClick={(event) => void onClick(event, alert.alert_id, action)}
      >
        {isBusy && busy?.action === action ? "处理中…" : label}
      </button>
    );

    return (
      <span className="alert-actions">
        {status === "open" && onAcknowledge &&
          button("acknowledge", "确认", handleAction)}
        {(status === "open" || status === "acknowledged") && onResolve &&
          button("resolve", "解决", handleAction)}
        {status === "resolved" && onReopen &&
          button("reopen", "重新打开", handleAction)}
        {onLoadHistory && (
          <button
            type="button"
            className="alert-history-button"
            onClick={(event) => void toggleHistory(event, alert.alert_id)}
            aria-expanded={historyOpenId === alert.alert_id}
          >
            {historyOpenId === alert.alert_id ? "收起记录" : "审计"}
          </button>
        )}
      </span>
    );
  };

  return (
    <div className="alert-panel">
      <div className="alert-head">
        <h2>告警</h2>
        <span className="alert-count">
          {alerts.length === 0
            ? "无"
            : criticalCount > 0
              ? `${criticalCount} 严重 · ${openAlerts.length} 待处置 / ${alerts.length}`
              : `${openAlerts.length} 待处置 / ${alerts.length}`}
        </span>
      </div>

      <div className="alert-filters" role="group" aria-label="告警状态筛选">
        {(
          [
            ["active", "未关闭", activeAlerts.length],
            ["resolved", "已解决", resolvedAlerts.length],
            ["all", "全部", alerts.length],
          ] as const
        ).map(([value, label, count]) => (
          <button
            key={value}
            type="button"
            className={filter === value ? "active" : ""}
            aria-pressed={filter === value}
            onClick={() => setFilter(value)}
          >
            {label} <span>{count}</span>
          </button>
        ))}
      </div>

      {canAct && (
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
              placeholder="确认 / 解决 / 重开说明"
              aria-label="告警处置备注"
            />
          </label>
        </div>
      )}
      {actionError && (
        <p className="alert-error" role="alert">
          {actionError}
        </p>
      )}

      {visible.length === 0 ? (
        <p className="alert-empty">
          {filter === "active" ? "当前没有未关闭告警。" : "没有匹配的告警。"}
        </p>
      ) : (
        <ul className="alert-list">
          {visible.slice(0, 30).map((alert) => {
            const status = statusOf(alert);
            const history = historyById[alert.alert_id];
            return (
              <li key={alert.alert_id} className="alert-item">
                <div
                  className={`alert-row sev-${alert.severity} status-${status}`}
                  onClick={() => onSelectDrone?.(alert.drone_id)}
                  title={alert.message}
                >
                  <span
                    className={`alert-mark sev-${alert.severity}`}
                    aria-hidden="true"
                  />
                  <div className="alert-body">
                    <div className="alert-line-1">
                      <span className="alert-drone">{alert.drone_id}</span>
                      <span className="alert-rule">
                        {RULE_LABEL[alert.rule] ?? alert.rule}
                      </span>
                      <span className={`alert-status status-${status}`}>
                        {STATUS_LABEL[status]}
                      </span>
                    </div>
                    <div className="alert-line-2">{alert.message}</div>
                    <div className="alert-line-3">
                      <span>{fmtTime(alert.at)}</span>
                      {(alert.trigger_count ?? 1) > 1 && (
                        <span> · 第 {alert.trigger_count} 次</span>
                      )}
                      {(alert.reopen_count ?? 0) > 0 && (
                        <span> · 重开 {alert.reopen_count} 次</span>
                      )}
                      {status === "acknowledged" && (
                        <span className="alert-ack-meta">
                          {" "}
                          · 已确认 {alert.acknowledged_by} ·{" "}
                          {fmtTime(alert.acknowledged_at)}
                          {alert.acknowledgement_note
                            ? ` · ${alert.acknowledgement_note}`
                            : ""}
                        </span>
                      )}
                      {status === "resolved" && (
                        <span className="alert-resolve-meta">
                          {" "}
                          · 已解决 {alert.resolved_by} ·{" "}
                          {fmtTime(alert.resolved_at)}
                          {alert.resolution_note
                            ? ` · ${alert.resolution_note}`
                            : ""}
                        </span>
                      )}
                      {canAct && actionButtons(alert)}
                    </div>
                  </div>
                </div>
                {historyOpenId === alert.alert_id && onLoadHistory && (
                  <div
                    className="alert-history"
                    onClick={(event) => event.stopPropagation()}
                  >
                    {historyLoadingId === alert.alert_id ? (
                      <span>正在读取审计记录…</span>
                    ) : historyError ? (
                      <span className="alert-history-error">{historyError}</span>
                    ) : history && history.length > 0 ? (
                      <ol>
                        {history.map((entry) => (
                          <li key={entry.event_id}>
                            <span className="history-action">
                              {EVENT_LABEL[entry.action] ?? entry.action}
                            </span>
                            <span className="history-actor">
                              {entry.actor || "系统"}
                            </span>
                            <span className="history-time">
                              {fmtTime(entry.at)}
                            </span>
                            {entry.note && (
                              <span className="history-note">{entry.note}</span>
                            )}
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <span>暂无审计记录。</span>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
