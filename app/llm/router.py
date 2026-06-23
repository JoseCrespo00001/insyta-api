"""LLM provider router with fallback (EQUIP-64).

`LLMRouter.evaluate(messages)` returns a parsed `EvaluationResponse` plus
`LLMUsage`. It tries Anthropic Haiku first (with prompt caching) and falls
back to OpenAI gpt-4.1-mini on rate-limit / 5xx. Each call is wrapped so the
caller never sees provider-specific exceptions.

Pricing constants are 2026-Q1 list prices. Update if Anthropic / OpenAI ship
new tiers. We track the *real* token counts returned by each provider so the
cost calculation reflects cache hits.
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any, Protocol

from pydantic import ValidationError

from app.llm.prompts.eval_v1 import (
    PROMPT_VERSION,
    SYSTEM_PROMPT_V1,
    build_user_prompt,
)
from app.llm.schemas import EvaluationResponse, LLMUsage
from app.observability.tracing import current_span_id, llm_span, record_llm_usage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider pricing (USD per 1M tokens; 2026-Q1 list).
# ---------------------------------------------------------------------------
PRICING = {
    "claude-haiku-4-5-20251001": {
        "input": Decimal("1.00"),
        "cache_write": Decimal("1.25"),
        "cache_read": Decimal("0.10"),
        "output": Decimal("5.00"),
    },
    "gpt-4.1-mini": {
        "input": Decimal("0.40"),
        "cache_write": Decimal("0.40"),
        "cache_read": Decimal("0.10"),
        "output": Decimal("1.60"),
    },
}


class TransientLLMError(Exception):
    """429 / 5xx — caller should fall back."""


class FatalLLMError(Exception):
    """Auth / quota / hard fail — do not retry, no fallback recovers it."""


class LLMProvider(Protocol):
    name: str

    async def evaluate(
        self, messages: list[dict[str, str]], context: str | None = None
    ) -> tuple[EvaluationResponse, LLMUsage]: ...


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
class AnthropicProvider:
    name = "anthropic"
    model = "claude-haiku-4-5-20251001"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            from app.llm.credentials import get_anthropic_key

            key = get_anthropic_key()
            if not key:
                raise FatalLLMError("ANTHROPIC_API_KEY not set")
            self._client = AsyncAnthropic(api_key=key)
        return self._client

    async def evaluate(
        self, messages: list[dict[str, str]], context: str | None = None
    ) -> tuple[EvaluationResponse, LLMUsage]:
        client = self._get_client()
        from anthropic import APIError, APIStatusError, RateLimitError

        user_prompt = build_user_prompt(messages, context)
        with llm_span(self.name, self.model) as span:
            started = time.monotonic()
            try:
                resp = await client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT_V1,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_prompt}],
                )
            except RateLimitError as exc:
                raise TransientLLMError(f"anthropic rate limit: {exc}") from exc
            except APIStatusError as exc:
                if exc.status_code and 500 <= exc.status_code < 600:
                    raise TransientLLMError(f"anthropic 5xx: {exc}") from exc
                raise FatalLLMError(f"anthropic {exc.status_code}: {exc}") from exc
            except APIError as exc:
                raise TransientLLMError(f"anthropic api error: {exc}") from exc

            latency_ms = int((time.monotonic() - started) * 1000)
            text = "".join(
                block.text
                for block in resp.content
                if getattr(block, "type", "") == "text"
            )
            usage = self._build_usage(resp, latency_ms)
            usage.phoenix_span_id = current_span_id()
            record_llm_usage(span, usage)
            return _parse_evaluation(text), usage

    def _build_usage(self, resp: Any, latency_ms: int) -> LLMUsage:
        u = resp.usage
        input_tokens = getattr(u, "input_tokens", 0) or 0
        output_tokens = getattr(u, "output_tokens", 0) or 0
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        cost = _calc_cost(
            self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
        )
        return LLMUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
            cost_usd=cost,
            model=self.model,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# OpenAI fallback
# ---------------------------------------------------------------------------
class OpenAIProvider:
    name = "openai"
    model = "gpt-4.1-mini"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            from app.llm.credentials import get_openai_key

            key = get_openai_key()
            if not key:
                raise FatalLLMError("OPENAI_API_KEY not set")
            self._client = AsyncOpenAI(api_key=key)
        return self._client

    async def evaluate(
        self, messages: list[dict[str, str]], context: str | None = None
    ) -> tuple[EvaluationResponse, LLMUsage]:
        client = self._get_client()
        from openai import APIError, APIStatusError, RateLimitError

        user_prompt = build_user_prompt(messages, context)
        with llm_span(self.name, self.model) as span:
            started = time.monotonic()
            try:
                resp = await client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT_V1},
                        {"role": "user", "content": user_prompt},
                    ],
                )
            except RateLimitError as exc:
                raise TransientLLMError(f"openai rate limit: {exc}") from exc
            except APIStatusError as exc:
                if exc.status_code and 500 <= exc.status_code < 600:
                    raise TransientLLMError(f"openai 5xx: {exc}") from exc
                raise FatalLLMError(f"openai {exc.status_code}: {exc}") from exc
            except APIError as exc:
                raise TransientLLMError(f"openai api error: {exc}") from exc

            latency_ms = int((time.monotonic() - started) * 1000)
            text = resp.choices[0].message.content or ""
            usage = self._build_usage(resp, latency_ms)
            usage.phoenix_span_id = current_span_id()
            record_llm_usage(span, usage)
            return _parse_evaluation(text), usage

    def _build_usage(self, resp: Any, latency_ms: int) -> LLMUsage:
        u = resp.usage
        input_tokens = getattr(u, "prompt_tokens", 0) or 0
        output_tokens = getattr(u, "completion_tokens", 0) or 0
        cached = 0
        if hasattr(u, "prompt_tokens_details"):
            cached = getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0
        cost = _calc_cost(
            self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cached,
            cache_write=0,
        )
        return LLMUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cached,
            cache_creation_input_tokens=0,
            cost_usd=cost,
            model=self.model,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _calc_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> Decimal:
    rates = PRICING.get(model)
    if rates is None:
        return Decimal("0")
    fresh_input = max(0, input_tokens - cache_read - cache_write)
    cost = (
        (Decimal(fresh_input) * rates["input"])
        + (Decimal(cache_write) * rates["cache_write"])
        + (Decimal(cache_read) * rates["cache_read"])
        + (Decimal(output_tokens) * rates["output"])
    ) / Decimal("1000000")
    return cost.quantize(Decimal("0.000001"))


def _parse_evaluation(text: str) -> EvaluationResponse:
    """Parse strict JSON, raising TransientLLMError on bad shape so the
    worker's retry-with-fallback loop kicks in."""
    text = text.strip()
    if text.startswith("```"):
        # Defensive: strip accidental markdown fences.
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransientLLMError(f"LLM returned non-JSON: {exc}") from exc
    try:
        return EvaluationResponse(**payload)
    except ValidationError as exc:
        raise TransientLLMError(f"LLM JSON failed schema: {exc}") from exc


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
class LLMRouter:
    """Tries primary -> fallback. Logs every attempt for Phoenix."""

    def __init__(
        self,
        primary: LLMProvider | None = None,
        fallback: LLMProvider | None = None,
    ) -> None:
        self.primary = primary or AnthropicProvider()
        self.fallback = fallback or OpenAIProvider()
        self.prompt_version = PROMPT_VERSION

    async def evaluate(
        self, messages: list[dict[str, str]], context: str | None = None
    ) -> tuple[EvaluationResponse, LLMUsage]:
        try:
            return await self.primary.evaluate(messages, context)
        except TransientLLMError as exc:
            logger.warning(
                "[LLM_ROUTER] %s failed, falling back to %s: %s",
                self.primary.name,
                self.fallback.name,
                exc,
            )
            return await self.fallback.evaluate(messages, context)
