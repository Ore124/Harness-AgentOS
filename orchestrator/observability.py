"""Optional OpenTelemetry instrumentation with stable correlation fields."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import threading
from typing import Any, Iterator

from opentelemetry import metrics, trace


_correlation: ContextVar[dict[str, str]] = ContextVar("harness_correlation", default={})
tracer = trace.get_tracer("harness.agentos")
meter = metrics.get_meter("harness.agentos")
run_counter = meter.create_counter("harness.runs")
work_counter = meter.create_counter("harness.work_items")
recovery_counter = meter.create_counter("harness.recoveries")
lease_expiry_counter = meter.create_counter("harness.lease_expiries")
_configure_lock = threading.Lock()
_configured = False


def configure(service_name: str, otlp_endpoint: str = "") -> None:
    """Install SDK providers once; export through OTLP when configured."""
    global _configured
    with _configure_lock:
        if _configured:
            return
        _configure_locked(service_name, otlp_endpoint)
        _configured = True


def _configure_locked(service_name: str, otlp_endpoint: str) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    resource = Resource.create({"service.name": service_name})
    trace_provider = TracerProvider(resource=resource)
    metric_readers = []
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            trace_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces"))
            )
            metric_readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/metrics")
                )
            )
        except ImportError as exc:
            raise RuntimeError("OTLP endpoint configured but OTLP exporter is not installed") from exc
    trace.set_tracer_provider(trace_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=metric_readers))


@contextmanager
def correlated_span(name: str, **identifiers: str | None) -> Iterator[Any]:
    attributes = {key: value for key, value in identifiers.items() if value is not None}
    token = _correlation.set({key: str(value) for key, value in attributes.items()})
    try:
        with tracer.start_as_current_span(name, attributes=attributes) as span:
            yield span
    finally:
        _correlation.reset(token)


def correlation_fields() -> dict[str, str]:
    return dict(_correlation.get())
