"""Pydantic schemas for LLM I/O."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvaluationResponse(BaseModel):
    """Strict shape we expect Claude/OpenAI to emit."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(..., ge=0, le=100)
    resolution: bool
    satisfaction: int = Field(..., ge=1, le=5)
    tone: Literal["positive", "neutral", "negative"]
    frustration: bool
    escalated: bool
    efficiency: int = Field(..., ge=1, le=5)
    scope_violation: bool
    topic: str = Field(..., min_length=1, max_length=128)
    summary: str = Field(..., min_length=1, max_length=2000)


class LLMUsage(BaseModel):
    """Token + cost accounting for one LLM call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    model: str = ""
    latency_ms: int = 0
