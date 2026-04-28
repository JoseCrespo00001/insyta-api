"""Observability layer (tracing + metrics).

`tracing.init_tracing()` should be called once at app startup (FastAPI
lifespan + Celery worker boot). Each LLM call wraps execution in a span via
`tracing.llm_span(provider, model)`. The span attributes capture token
counts, cost, cache hit rate, and latency — the dimensions the optimization
loop needs to spot cost spikes per tenant.
"""

from app.observability.tracing import (
    current_span_id,
    init_tracing,
    llm_span,
    record_llm_usage,
)

__all__ = ["current_span_id", "init_tracing", "llm_span", "record_llm_usage"]
