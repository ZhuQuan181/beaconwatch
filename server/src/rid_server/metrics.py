"""Small, dependency-free Prometheus text metrics for the API process."""
from __future__ import annotations

import time
from collections import defaultdict


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class RequestMetrics:
    """Counters updated by the HTTP middleware and rendered at /metrics."""

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.in_flight = 0
        self.requests_total = 0
        self.duration_sum = 0.0
        self.auth_failures = 0
        self.authz_failures = 0
        self._responses: dict[tuple[str, str, int], int] = defaultdict(int)

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

    def render(self) -> list[str]:
        lines = [
            "# HELP rid_http_requests_total HTTP requests handled by this process.",
            "# TYPE rid_http_requests_total counter",
            f"rid_http_requests_total {self.requests_total}",
            "# HELP rid_http_request_duration_seconds_sum Total HTTP handling time.",
            "# TYPE rid_http_request_duration_seconds_sum counter",
            f"rid_http_request_duration_seconds_sum {self.duration_sum:.6f}",
            "# HELP rid_http_requests_in_flight Requests currently being handled.",
            "# TYPE rid_http_requests_in_flight gauge",
            f"rid_http_requests_in_flight {self.in_flight}",
            "# HELP rid_http_auth_failures_total Rejected Bearer-token attempts.",
            "# TYPE rid_http_auth_failures_total counter",
            f"rid_http_auth_failures_total {self.auth_failures}",
            "# HELP rid_http_authz_failures_total Authenticated calls denied by role.",
            "# TYPE rid_http_authz_failures_total counter",
            f"rid_http_authz_failures_total {self.authz_failures}",
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
        return lines
