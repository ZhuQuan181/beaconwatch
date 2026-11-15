"""Dependency-free Prometheus metrics for the API process."""
from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Histogram:
    """Fixed-bucket cumulative histogram rendered in Prometheus text form."""

    def __init__(self, buckets: Sequence[float]) -> None:
        self.buckets = tuple(sorted(float(value) for value in buckets))
        self.counts = [0 for _ in self.buckets]
        self.count = 0
        self.total = 0.0

    def observe(self, value: float) -> None:
        sample = max(0.0, float(value))
        if not math.isfinite(sample):
            return
        self.count += 1
        self.total += sample
        for index, upper_bound in enumerate(self.buckets):
            if sample <= upper_bound:
                self.counts[index] += 1
                return

    def render(
        self,
        name: str,
        help_text: str,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> list[str]:
        lines = [
            f"# HELP {name} {help_text}",
            f"# TYPE {name} histogram",
        ]
        cumulative = 0
        for upper_bound, count in zip(self.buckets, self.counts):
            cumulative += count
            lines.append(
                f"{name}_bucket"
                f"{_labels(labels, le=_format_le(upper_bound))} {cumulative}"
            )
        lines.append(
            f"{name}_bucket"
            f"{_labels(labels, le='+Inf')} {self.count}"
        )
        lines.append(f"{name}_sum{_labels(labels)} {self.total:.6f}")
        lines.append(f"{name}_count{_labels(labels)} {self.count}")
        return lines


def _format_le(value: float) -> str:
    return f"{value:g}"


def _labels(
    labels: Mapping[str, str] | None,
    *,
    le: str | None = None,
) -> str:
    if not labels and le is None:
        return ""
    pairs = [
        f'{key}="{_escape_label(value)}"'
        for key, value in sorted((labels or {}).items())
    ]
    if le is not None:
        pairs.append(f'le="{le}"')
    return "{" + ",".join(pairs) + "}"


class RequestMetrics:
    """HTTP, ingest and delivery counters exposed at ``/metrics``."""

    HTTP_BUCKETS = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    )
    INGEST_BUCKETS = (
        0.01,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
    )
    NOTIFICATION_BUCKETS = (
        0.1,
        0.5,
        1.0,
        5.0,
        15.0,
        30.0,
        60.0,
        300.0,
        900.0,
    )

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.in_flight = 0
        self.requests_total = 0
        self.duration_sum = 0.0
        self.auth_failures = 0
        self.authz_failures = 0
        self.ws_connections_total = 0
        self.ws_disconnections_total = 0
        self.ws_rejected_total = 0
        self.ws_malformed_messages_total = 0
        self._responses: dict[tuple[str, str, int], int] = defaultdict(int)
        self._http_durations: dict[tuple[str, str], Histogram] = {}
        self._ingest_latency = Histogram(self.INGEST_BUCKETS)
        self._notification_lag = Histogram(self.NOTIFICATION_BUCKETS)

    def begin(self) -> float:
        self.in_flight += 1
        return time.perf_counter()

    def observe(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        started_at: float,
    ) -> None:
        elapsed = time.perf_counter() - started_at
        self.in_flight = max(0, self.in_flight - 1)
        self.requests_total += 1
        self.duration_sum += elapsed
        self._responses[(method, route, status_code)] += 1
        key = (method, route)
        histogram = self._http_durations.get(key)
        if histogram is None:
            histogram = Histogram(self.HTTP_BUCKETS)
            self._http_durations[key] = histogram
        histogram.observe(elapsed)

    def observe_ingest_latency(self, seconds: float) -> None:
        """Record edge ``received_at`` to committed ingest latency."""

        self._ingest_latency.observe(seconds)

    def observe_notification_lag(self, seconds: float) -> None:
        """Record outbox enqueue to successful webhook delivery."""

        self._notification_lag.observe(seconds)

    def render(self) -> list[str]:
        lines = [
            "# HELP rid_http_requests_total HTTP requests handled by this process.",
            "# TYPE rid_http_requests_total counter",
            f"rid_http_requests_total {self.requests_total}",
            "# HELP rid_http_requests_in_flight Requests currently being handled.",
            "# TYPE rid_http_requests_in_flight gauge",
            f"rid_http_requests_in_flight {self.in_flight}",
            "# HELP rid_http_auth_failures_total Rejected API-key attempts.",
            "# TYPE rid_http_auth_failures_total counter",
            f"rid_http_auth_failures_total {self.auth_failures}",
            "# HELP rid_http_authz_failures_total Authenticated calls denied by role.",
            "# TYPE rid_http_authz_failures_total counter",
            f"rid_http_authz_failures_total {self.authz_failures}",
            "# HELP rid_ws_connections_total Accepted WebSocket connections.",
            "# TYPE rid_ws_connections_total counter",
            f"rid_ws_connections_total {self.ws_connections_total}",
            "# HELP rid_ws_disconnections_total Closed WebSocket connections.",
            "# TYPE rid_ws_disconnections_total counter",
            f"rid_ws_disconnections_total {self.ws_disconnections_total}",
            "# HELP rid_ws_rejected_total Rejected WebSocket handshakes.",
            "# TYPE rid_ws_rejected_total counter",
            f"rid_ws_rejected_total {self.ws_rejected_total}",
            "# HELP rid_ws_malformed_messages_total Invalid client WebSocket frames.",
            "# TYPE rid_ws_malformed_messages_total counter",
            (
                "rid_ws_malformed_messages_total "
                f"{self.ws_malformed_messages_total}"
            ),
            "# HELP rid_process_uptime_seconds Process uptime.",
            "# TYPE rid_process_uptime_seconds gauge",
            f"rid_process_uptime_seconds {time.monotonic() - self.started_at:.3f}",
            "# HELP rid_http_responses_total HTTP responses by method, route and status.",
            "# TYPE rid_http_responses_total counter",
        ]
        for (method, route, status_code), count in sorted(self._responses.items()):
            lines.append(
                "rid_http_responses_total"
                f'{{method="{_escape_label(method)}",'
                f'route="{_escape_label(route)}",'
                f'status="{status_code}"}} {count}'
            )
        for (method, route), histogram in sorted(self._http_durations.items()):
            lines.extend(
                histogram.render(
                    "rid_http_request_duration_seconds",
                    "HTTP request handling duration.",
                    labels={"method": method, "route": route},
                )
            )
        lines.extend(
            self._ingest_latency.render(
                "rid_observation_ingest_latency_seconds",
                "Edge received_at to committed server ingest latency.",
            )
        )
        lines.extend(
            self._notification_lag.render(
                "rid_notification_delivery_lag_seconds",
                "Notification outbox enqueue to successful delivery latency.",
            )
        )
        return lines
