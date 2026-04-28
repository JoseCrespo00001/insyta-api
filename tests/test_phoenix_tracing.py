"""Phoenix tracing tests (EQUIP-66).

We don't hit a real Phoenix collector — the assertions verify that:
  1. `init_tracing()` is idempotent and works without PHOENIX_ENDPOINT.
  2. `llm_span()` opens a real span (not a no-op) once initialized.
  3. `record_llm_usage()` attaches the expected attributes.
  4. `current_span_id()` returns a 16-char hex id inside a span.
"""

from __future__ import annotations

from decimal import Decimal

from app.llm.schemas import LLMUsage
from app.observability.tracing import (
    _NoOpSpan,
    _reset_for_tests,
    current_span_id,
    init_tracing,
    llm_span,
    record_llm_usage,
)


def setup_function(_):
    _reset_for_tests()


def teardown_function(_):
    _reset_for_tests()


class TestInit:
    def test_init_without_endpoint_does_not_raise(self, monkeypatch):
        monkeypatch.delenv("PHOENIX_ENDPOINT", raising=False)
        init_tracing()  # should not raise

    def test_init_is_idempotent(self):
        init_tracing()
        init_tracing()  # second call should be a no-op


class TestSpan:
    def test_span_outside_init_does_not_crash(self):
        # Without init we either get a real proxy or our _NoOpSpan; both must
        # accept set_attribute without raising and never produce broken state.
        with llm_span("anthropic", "claude-haiku-4-5-20251001") as span:
            usage = LLMUsage(model="m", input_tokens=10)
            record_llm_usage(span, usage)

    def test_span_after_init_returns_real_span(self):
        init_tracing()
        with llm_span("anthropic", "claude-haiku-4-5-20251001") as span:
            assert not isinstance(span, _NoOpSpan)
            sid = current_span_id()
            assert sid is not None
            assert len(sid) == 16
            int(sid, 16)  # valid hex

    def test_record_usage_sets_attributes(self):
        init_tracing()
        usage = LLMUsage(
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=0,
            cost_usd=Decimal("0.001"),
            model="claude-haiku-4-5-20251001",
            latency_ms=42,
        )
        with llm_span("anthropic", usage.model) as span:
            record_llm_usage(span, usage)
            attrs = getattr(span, "attributes", None) or {}
            # Both real (BoundedAttributes) and dict expose `dict()`.
            attrs = dict(attrs)
            assert attrs.get("llm.input_tokens") == 1000
            assert attrs.get("llm.output_tokens") == 200
            assert attrs.get("llm.cache_hit_rate") == 0.9
            assert attrs.get("llm.cost_usd") == "0.001"

    def test_record_usage_on_noop_is_safe(self):
        # Without init, record_llm_usage on a noop span should not raise.
        usage = LLMUsage(model="m", input_tokens=10)
        with llm_span("x", "m") as span:
            record_llm_usage(span, usage)


class TestSpanIdPersistedOnUsage:
    def test_usage_phoenix_span_id_is_stable_inside_span(self):
        init_tracing()
        with llm_span("anthropic", "claude-haiku-4-5-20251001"):
            sid_a = current_span_id()
            sid_b = current_span_id()
        assert sid_a == sid_b
        assert sid_a is not None
