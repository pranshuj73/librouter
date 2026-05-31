"""Prometheus collectors used across the gateway."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


REGISTRY = CollectorRegistry()

REQUESTS_TOTAL = Counter(
    "gateway_requests_total",
    "Total requests served, labeled by caller, tier, outcome.",
    labelnames=("caller", "tier", "outcome"),
    registry=REGISTRY,
)

REQUEST_LATENCY = Histogram(
    "gateway_request_latency_seconds",
    "End-to-end latency of /v1/chat/completions.",
    labelnames=("tier", "outcome"),
    buckets=(0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0),
    registry=REGISTRY,
)

ATTEMPTS_TOTAL = Counter(
    "gateway_attempts_total",
    "Total per-attempt outcomes.",
    labelnames=("provider", "model", "status"),
    registry=REGISTRY,
)

ROUTING_WEIGHT = Gauge(
    "gateway_routing_weight",
    "Latest effective routing weight (post-refresh).",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)

BREAKER_STATE = Gauge(
    "gateway_breaker_state",
    "Breaker state per (provider, model): 0=closed, 1=half_open, 2=open.",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)

BUCKET_REMAINING = Gauge(
    "gateway_bucket_remaining",
    "Token-bucket remaining for a (provider, model, dim=rpm|tpm).",
    labelnames=("provider", "model", "dim"),
    registry=REGISTRY,
)

COST_USD_TOTAL = Counter(
    "gateway_cost_usd_total",
    "USD spent per caller/tier/provider.",
    labelnames=("caller", "tier", "provider"),
    registry=REGISTRY,
)

ACCOUNTING_DROPPED = Counter(
    "gateway_accounting_dropped_total",
    "Number of accounting rows dropped due to backpressure or write failure.",
    registry=REGISTRY,
)

REDIS_DOWN = Gauge(
    "gateway_redis_down",
    "1 if Redis is currently unreachable, else 0.",
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
