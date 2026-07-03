"""Pydantic schemas for LLM I/O."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RubricDimension(BaseModel):
    """Una dimensión de la rúbrica juzgada por el LLM (A1..F4).

    Regla de evidencia (rúbrica §0.3 y §4): si no hay turn_id que la justifique,
    el score es null (no cuenta) — no se permite score sin evidencia.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=8)  # p.ej. "A1", "E4"
    score: int | None = Field(default=None, ge=1, le=5)
    turn_id: int | None = None
    justificacion: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def _score_requires_evidence(self) -> RubricDimension:
        # Sin turn_id → score nulo (no cero). Elimina el 80% de alucinaciones del juez.
        if self.turn_id is None:
            object.__setattr__(self, "score", None)
        return self


class RubricResponse(BaseModel):
    """Salida del judge por conversación según la rúbrica completa (§9).

    NO incluye score_final/score_bruto: esos los computa `rubric_scoring` de
    forma determinista a partir de `dimensiones` + `veto_flags`.
    """

    model_config = ConfigDict(extra="forbid")

    dimensiones: list[RubricDimension] = Field(default_factory=list)
    veto_flags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sentimiento_trayectoria: list[Literal["positivo", "neutral", "negativo"]] = Field(
        default_factory=list
    )
    fraude_flags: list[str] = Field(default_factory=list)
    resumen: str = Field(default="", max_length=2000)
    requiere_revision_humana: bool = False


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

    model_config = ConfigDict(arbitrary_types_allowed=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    model: str = ""
    latency_ms: int = 0
    phoenix_span_id: str | None = None
