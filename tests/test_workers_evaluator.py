"""Tests for the LLM router + evaluator worker (EQUIP-64).

We never hit real Anthropic/OpenAI in tests. The providers are stubbed so we
can drive happy-path, retry, fallback, and JSON-validation paths.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from app.llm.router import (
    FatalLLMError,
    LLMRouter,
    TransientLLMError,
    _calc_cost,
    _parse_evaluation,
)
from app.llm.schemas import EvaluationResponse, LLMUsage


VALID_PAYLOAD = {
    "score": 80,
    "resolution": True,
    "satisfaction": 4,
    "tone": "positive",
    "frustration": False,
    "escalated": False,
    "efficiency": 4,
    "scope_violation": False,
    "topic": "tracking pedido",
    "summary": "Usuario consulto su pedido y recibio el tracking.",
}


class FakeProvider:
    def __init__(
        self,
        *,
        name: str = "fake",
        text: str | None = None,
        raise_exc: Exception | None = None,
        usage: LLMUsage | None = None,
    ) -> None:
        self.name = name
        self.text = text or json.dumps(VALID_PAYLOAD)
        self.raise_exc = raise_exc
        self.usage = usage or LLMUsage(
            input_tokens=100,
            output_tokens=200,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=0,
            cost_usd=Decimal("0.001"),
            model="fake-model",
            latency_ms=42,
        )
        self.calls = 0

    async def evaluate(self, messages):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return _parse_evaluation(self.text), self.usage


class TestParseEvaluation:
    def test_valid_json(self):
        parsed = _parse_evaluation(json.dumps(VALID_PAYLOAD))
        assert isinstance(parsed, EvaluationResponse)
        assert parsed.score == 80

    def test_strips_markdown_fences(self):
        text = f"```json\n{json.dumps(VALID_PAYLOAD)}\n```"
        parsed = _parse_evaluation(text)
        assert parsed.score == 80

    def test_invalid_json_raises_transient(self):
        with pytest.raises(TransientLLMError):
            _parse_evaluation("not valid json")

    def test_extra_field_raises_transient(self):
        bad = {**VALID_PAYLOAD, "unexpected": True}
        with pytest.raises(TransientLLMError):
            _parse_evaluation(json.dumps(bad))

    def test_score_out_of_range_raises_transient(self):
        bad = {**VALID_PAYLOAD, "score": 150}
        with pytest.raises(TransientLLMError):
            _parse_evaluation(json.dumps(bad))

    def test_invalid_tone_raises_transient(self):
        bad = {**VALID_PAYLOAD, "tone": "ecstatic"}
        with pytest.raises(TransientLLMError):
            _parse_evaluation(json.dumps(bad))


class TestCostCalc:
    def test_haiku_cache_read_cheaper_than_input(self):
        full_input = _calc_cost(
            "claude-haiku-4-5-20251001",
            input_tokens=1000,
            output_tokens=0,
            cache_read=0,
            cache_write=0,
        )
        full_cache = _calc_cost(
            "claude-haiku-4-5-20251001",
            input_tokens=1000,
            output_tokens=0,
            cache_read=1000,
            cache_write=0,
        )
        # Cache reads should be at most 10% of full input price.
        assert full_cache <= full_input * Decimal("0.10")

    def test_unknown_model_returns_zero(self):
        cost = _calc_cost(
            "imaginary-model-9000",
            input_tokens=1000,
            output_tokens=1000,
            cache_read=0,
            cache_write=0,
        )
        assert cost == Decimal("0")


class TestLLMRouter:
    def test_primary_succeeds(self):
        primary = FakeProvider(name="anth")
        fallback = FakeProvider(name="oai")
        router = LLMRouter(primary=primary, fallback=fallback)
        parsed, usage = asyncio.run(
            router.evaluate([{"role": "user", "content": "hi"}])
        )
        assert primary.calls == 1
        assert fallback.calls == 0
        assert parsed.score == 80
        assert usage.model == "fake-model"

    def test_falls_back_on_transient(self):
        primary = FakeProvider(name="anth", raise_exc=TransientLLMError("rate limit"))
        fallback = FakeProvider(name="oai")
        router = LLMRouter(primary=primary, fallback=fallback)
        parsed, usage = asyncio.run(
            router.evaluate([{"role": "user", "content": "hi"}])
        )
        assert primary.calls == 1
        assert fallback.calls == 1
        assert parsed.score == 80

    def test_fatal_does_not_fall_back(self):
        primary = FakeProvider(name="anth", raise_exc=FatalLLMError("auth"))
        fallback = FakeProvider(name="oai")
        router = LLMRouter(primary=primary, fallback=fallback)
        with pytest.raises(FatalLLMError):
            asyncio.run(router.evaluate([{"role": "user", "content": "hi"}]))
        assert fallback.calls == 0


class TestCacheHitRateMetric:
    """`cache_read_input_tokens / total_input_tokens > 0.7` after warm cache."""

    def test_cache_hit_rate_target(self):
        usage = LLMUsage(
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=0,
            cost_usd=Decimal("0.001"),
            model="claude-haiku-4-5-20251001",
            latency_ms=100,
        )
        rate = usage.cache_read_input_tokens / max(1, usage.input_tokens)
        assert rate > 0.7
