"""Minimal OpenTelemetry setup for durable Runtime spans.

The Runtime owns its durable trace context in MySQL so a replacement process can
continue the same trace.  OTLP export is optional and configured exclusively by
the standard ``OTEL_*`` environment variables.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Iterable

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Link, NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags


_lock = threading.Lock()
_provider: TracerProvider | None = None
_tracer = None


def _timestamp_ns(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1_000_000_000)


def _span_context(trace_id: str, span_id: str) -> SpanContext:
    return SpanContext(
        trace_id=int(trace_id, 16),
        span_id=int(span_id, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )


def configure_telemetry(*, exporter: SpanExporter | None = None, force: bool = False):
    """Configure a process-local tracer.

    With no explicit exporter, OTLP is enabled only when an OTLP endpoint is
    configured.  Tests can inject an in-memory exporter without mutating the
    global OpenTelemetry provider.
    """
    global _provider, _tracer
    with _lock:
        if _tracer is not None and not force:
            return _tracer
        if _provider is not None:
            _provider.shutdown()
        resource = Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME", "myclaw-runtime"),
            "service.version": os.getenv("MYCLAW_RELEASE", "development"),
        })
        provider = TracerProvider(resource=resource)
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        elif os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        _provider = provider
        _tracer = provider.get_tracer("myclaw.runtime")
        return _tracer


def shutdown_telemetry() -> None:
    global _provider, _tracer
    with _lock:
        if _provider is not None:
            _provider.shutdown()
        _provider = None
        _tracer = None


def emit_runtime_span(
    *,
    trace_id: str,
    parent_span_id: str,
    name: str,
    started_at: datetime,
    ended_at: datetime,
    status: str,
    attributes: dict,
    linked_contexts: Iterable[tuple[str, str, dict]] = (),
) -> tuple[str, str]:
    """Emit one span and return its actual trace/span identifiers."""
    parent = NonRecordingSpan(_span_context(trace_id, parent_span_id))
    context = trace.set_span_in_context(parent)
    links = [
        Link(_span_context(linked_trace_id, linked_span_id), link_attributes)
        for linked_trace_id, linked_span_id, link_attributes in linked_contexts
    ]
    span = configure_telemetry().start_span(
        name,
        context=context,
        links=links,
        start_time=_timestamp_ns(started_at),
        attributes=attributes,
    )
    span.set_status(Status(StatusCode.ERROR if status == "error" else StatusCode.OK))
    span.end(end_time=_timestamp_ns(ended_at))
    span_context = span.get_span_context()
    return f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}"
