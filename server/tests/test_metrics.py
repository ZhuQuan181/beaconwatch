"""Prometheus histogram and operational metric tests."""
from __future__ import annotations

import httpx
from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.config import Settings
from rid_server.metrics import Histogram, RequestMetrics
from rid_server.mqtt_subscriber import MqttSubscriber


def test_histogram_renders_cumulative_buckets_and_inf():
    histogram = Histogram((0.1, 1.0))
    histogram.observe(0.05)
    histogram.observe(0.5)
    histogram.observe(2.0)

    lines = histogram.render("test_seconds", "Test duration.")

    assert 'test_seconds_bucket{le="0.1"} 1' in lines
    assert 'test_seconds_bucket{le="1"} 2' in lines
    assert 'test_seconds_bucket{le="+Inf"} 3' in lines
    assert "test_seconds_sum 2.550000" in lines
    assert "test_seconds_count 3" in lines


def test_request_metrics_renders_all_histogram_families():
    metrics = RequestMetrics()
    started = metrics.begin()
    metrics.observe(
        method="GET",
        route="/observations",
        status_code=200,
        started_at=started,
    )
    metrics.observe_ingest_latency(0.2)
    metrics.observe_notification_lag(12.0)
    metrics.ws_connections_total = 3
    metrics.ws_disconnections_total = 2
    metrics.ws_rejected_total = 1
    metrics.ws_malformed_messages_total = 4

    body = "\n".join(metrics.render())

    assert (
        'rid_http_request_duration_seconds_count'
        '{method="GET",route="/observations"} 1'
    ) in body
    assert "rid_observation_ingest_latency_seconds_count 1" in body
    assert "rid_notification_delivery_lag_seconds_count 1" in body
    assert "rid_ws_connections_total 3" in body
    assert "rid_ws_disconnections_total 2" in body
    assert "rid_ws_rejected_total 1" in body
    assert "rid_ws_malformed_messages_total 4" in body


async def test_http_and_database_metrics_are_exported(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(Settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        assert (await client.get("/health")).status_code == 200
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "rid_database_up 1" in response.text
    assert "rid_build_info{" in response.text
    assert (
        'rid_http_request_duration_seconds_bucket'
        '{method="GET",route="/health",le="0.005"}'
    ) in response.text
