"""OpenTelemetry tracing wired to the Phoenix collector (EQUIP-66).

Initialization is idempotent — calling `init_tracing()` twice is a no-op.
Tests can call `_reset_for_tests()` to roll back state between tests.

Why a custom thin wrapper instead of `phoenix.otel.register()`:
  - The Phoenix python package pulls in heavy ML libs we don't need.
  - We want to fall back to a NoOp tracer when `PHOENIX_ENDPOINT` isn't set
    (dev/test) without raising at startup.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from app.core.config import get_settings

logger = logging.getLogger(__name__)


_INITIALIZED = False
_TRACER = None  # type: ignore[var-annotated]


def init_tracing(service_name: str = "insyta-api") -> None:
    """Boot OpenTelemetry SDK + OTLP HTTP exporter pointing at Phoenix.

    If `PHOENIX_ENDPOINT` is empty we install the NoOpTracerProvider so
    application code can call `llm_span()` unconditionally.
    """
    global _INITIALIZED, _TRACER
    if _INITIALIZED:
        return

    settings = get_settings()
    endpoint = (settings.phoenix_endpoint or "").strip()

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # pragma: no cover
        logger.warning("[TRACING] OpenTelemetry SDK missing; tracing disabled")
        _INITIALIZED = True
        return

    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("[TRACING] OTLP exporter -> %s", endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[TRACING] failed to wire OTLP exporter (%s); using local-only tracer",
                exc,
            )
    else:
        logger.info("[TRACING] PHOENIX_ENDPOINT not set; tracing in-memory only")

    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("insyta.llm")
    _INITIALIZED = True


def _reset_for_tests() -> None:
    """Test helper — clears module state so tests can re-init cleanly."""
    global _INITIALIZED, _TRACER
    _INITIALIZED = False
    _TRACER = None


def current_span_id() -> str | None:
    """Return the current span id as 16-char hex, or None if no active span."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return None
        return format(ctx.span_id, "016x")
    except Exception:  # pragma: no cover
        return None


@contextmanager
def llm_span(provider: str, model: str) -> Iterator[object]:
    """Context manager that opens a span around an LLM call.

    The caller can `record_llm_usage(usage)` inside the block to attach token
    counts and cost. If tracing isn't initialized we yield a no-op object so
    application code stays the same.
    """
    if _TRACER is None:
        yield _NoOpSpan()
        return
    with _TRACER.start_as_current_span(
        f"llm.{provider}.evaluate",
        attributes={"llm.provider": provider, "llm.model": model},
    ) as span:
        yield span


def record_llm_usage(span: object, usage) -> None:
    """Attach LLMUsage attributes to a span."""
    if span is None or isinstance(span, _NoOpSpan):
        return
    set_attr = getattr(span, "set_attribute", None)
    if set_attr is None:
        return
    total_input = max(1, usage.input_tokens)
    cache_rate = (usage.cache_read_input_tokens or 0) / total_input
    set_attr("llm.input_tokens", usage.input_tokens)
    set_attr("llm.output_tokens", usage.output_tokens)
    set_attr("llm.cache_read_input_tokens", usage.cache_read_input_tokens)
    set_attr("llm.cache_creation_input_tokens", usage.cache_creation_input_tokens)
    set_attr("llm.cache_hit_rate", round(cache_rate, 4))
    set_attr("llm.cost_usd", str(usage.cost_usd))
    set_attr("llm.latency_ms", usage.latency_ms)
    set_attr("llm.model", usage.model)


class _NoOpSpan:
    def set_attribute(self, *_args, **_kwargs) -> None:
        return None

    def get_span_context(self):
        return _NoOpContext()


class _NoOpContext:
    is_valid = False
    span_id = 0
