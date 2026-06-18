"""Optimization Loop — manual one-shot pattern detector (Día 4-6, EQUIP TP4).

Loads the last N evaluations for a project, groups them by sub-agent slug,
calls Sonnet to detect failure patterns in each group, and persists the results
as `Improvement` rows (status=pending).

Usage via CLI:
    uv run python -m app.cli.run_optimization_loop --project-id pyme_maria --limit 500

The LLM call uses AsyncAnthropic directly (not LLMRouter.evaluate) because the
router's schema is hard-wired to `EvaluationResponse`. The pricing constants
mirror those in `app/llm/router.py` (2026-Q1 list prices for Sonnet claude-sonnet-4-6).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Conversation, Evaluation, Improvement, Project

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sonnet pricing (2026-Q1 list, USD per 1M tokens).
# claude-sonnet-4-6 approximates the claude-sonnet-4-5 / 3-7 pricing band.
# ---------------------------------------------------------------------------
_SONNET_INPUT_PER_M = Decimal("3.00")
_SONNET_OUTPUT_PER_M = Decimal("15.00")
_SONNET_MODEL = "claude-sonnet-4-6"

# Default filesystem root for prompts. Overridable via env var
# INSYTA_PROMPTS_ROOT so CI / different machines don't need the pyme-maria-agent
# repo cloned at a fixed path.
_DEFAULT_PROMPTS_ROOT = Path(
    os.environ.get(
        "INSYTA_PROMPTS_ROOT",
        str(Path(__file__).parents[3] / "../pyme-maria-agent/agents/prompts"),
    )
).resolve()

_SYSTEM_PROMPT_TEMPLATE = """\
Sos un analista de calidad de agentes LLM. Te paso {n_convs} conversaciones \
donde el sub-agente '{sub_agent}' falló (score < 60). Cada conversación incluye \
su resumen, topic, y fragmentos del diálogo.

Tu trabajo:
1. Identificá 1-2 PATRONES recurrentes de fallo (no temas aislados, sino patrones).
2. Para cada patrón:
   - causa raíz concreta (qué instrucción o ausencia de instrucción lo provoca)
   - 2-3 fragmentos textuales de convs ejemplo (citas literales del diálogo)
   - IDs de las convs que exhiben el patrón
   - propuesta ESPECÍFICA de cambio al system prompt (prompt_before → prompt_after)
   - impacto estimado: "alto" | "medio" | "bajo"
3. NO sugerencias genéricas tipo "ser más amable" o "responder más rápido".
   Cambios ESPECÍFICOS con reglas accionables para el agent.
4. JSON estricto, sin texto fuera del JSON. Sin markdown fences.

Schema exacto:
{{
  "patterns": [
    {{
      "pattern": "string — nombre corto del patrón",
      "root_cause": "string — causa raíz en el prompt actual",
      "conv_examples": ["conv_public_id_1", "conv_public_id_2"],
      "sample_excerpts": ["fragmento 1", "fragmento 2"],
      "prompt_before": "string — fragmento del prompt actual que causa el problema",
      "prompt_after": "string — propuesta de reemplazo/adición al prompt",
      "impact_estimate": "alto"
    }}
  ]
}}
"""


# ---------------------------------------------------------------------------
# Public schemas
# ---------------------------------------------------------------------------


class OptimizationLoopInput(BaseModel):
    project_public_id: str = Field(..., min_length=1)
    limit: int = Field(default=500, ge=10, le=2000)
    sub_agents: list[str] = Field(
        default=["productos", "pedidos", "devoluciones", "pagos"]
    )
    prompt_version_label: str = Field(default="v1")
    dry_run: bool = False

    @field_validator("sub_agents")
    @classmethod
    def validate_sub_agents_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("sub_agents must not be empty")
        return v


class PatternDetected(BaseModel):
    sub_agent: str
    pattern: str
    root_cause: str
    affected_conv_ids: list[str]
    prompt_before: str
    prompt_after: str
    impact_estimate: str
    sample_excerpts: list[str]


class OptimizationLoopOutput(BaseModel):
    project_public_id: str
    started_at: datetime
    completed_at: datetime
    sub_agents_analyzed: int
    patterns: list[PatternDetected]
    sonnet_cost_usd: float
    improvements_persisted_ids: list[str]


# ---------------------------------------------------------------------------
# Internal: raw Sonnet call (schema-free, not using LLMRouter)
# ---------------------------------------------------------------------------


class _SonnetPatternResponse(BaseModel):
    patterns: list[_RawPattern]


class _RawPattern(BaseModel):
    pattern: str
    root_cause: str
    conv_examples: list[str] = Field(default_factory=list)
    sample_excerpts: list[str] = Field(default_factory=list)
    prompt_before: str = ""
    prompt_after: str
    impact_estimate: str = "medio"

    @field_validator("impact_estimate")
    @classmethod
    def normalize_impact(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("alto", "medio", "bajo"):
            return "medio"
        return v


def _calc_sonnet_cost(input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * _SONNET_INPUT_PER_M / Decimal("1000000")
        + Decimal(output_tokens) * _SONNET_OUTPUT_PER_M / Decimal("1000000")
    ).quantize(Decimal("0.000001"))


def _load_prompt_file(sub_agent: str, version_label: str) -> str:
    """Load prompt text from filesystem. Returns fallback string on miss."""
    path = _DEFAULT_PROMPTS_ROOT / version_label / f"{sub_agent}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning(
        "[LOOP] prompt file not found: %s — using fallback placeholder", path
    )
    return f"system prompt {version_label} not found for {sub_agent}"


async def _call_sonnet(
    *,
    sub_agent: str,
    eval_summaries: list[dict],
    prompt_version_label: str,
    anthropic_client,
) -> tuple[list[_RawPattern], Decimal]:
    """Call Sonnet with the pattern-detection prompt. Retries x2 on parse failure."""
    system_content = _SYSTEM_PROMPT_TEMPLATE.format(
        sub_agent=sub_agent, n_convs=len(eval_summaries)
    )
    user_content = json.dumps({"evaluations": eval_summaries}, ensure_ascii=False)

    total_cost = Decimal("0")
    last_exc: Exception | None = None

    for attempt in range(3):
        try:
            resp = await anthropic_client.messages.create(
                model=_SONNET_MODEL,
                max_tokens=4096,
                system=system_content,
                messages=[{"role": "user", "content": user_content}],
            )
            text = "".join(
                block.text
                for block in resp.content
                if getattr(block, "type", "") == "text"
            ).strip()

            input_tokens = getattr(resp.usage, "input_tokens", 0) or 0
            output_tokens = getattr(resp.usage, "output_tokens", 0) or 0
            total_cost += _calc_sonnet_cost(input_tokens, output_tokens)

            # Strip accidental markdown fences
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:].lstrip()

            payload = json.loads(text)
            parsed = _SonnetPatternResponse(**payload)
            logger.info(
                "[LOOP] sub_agent=%s attempt=%d patterns=%d cost=%s",
                sub_agent,
                attempt + 1,
                len(parsed.patterns),
                total_cost,
            )
            return parsed.patterns, total_cost
        except (json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "[LOOP] sub_agent=%s attempt=%d parse error: %s",
                sub_agent,
                attempt + 1,
                exc,
            )

    raise RuntimeError(
        f"[LOOP] Sonnet returned unparseable JSON for {sub_agent} after 3 attempts: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _resolve_project(session: AsyncSession, public_id: str) -> Project:
    result = await session.execute(
        select(Project).where(Project.public_id == public_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise ValueError(f"project not found: public_id={public_id!r}")
    return project


async def _load_failed_evals(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    limit: int,
    score_threshold: int = 60,
) -> list[dict]:
    """Return up to `limit` evaluations with score < score_threshold for one agent.

    Joins Conversation to get conv.public_id for referencing in prompt output.
    Joins Message is skipped for performance — eval.summary carries enough context.
    """
    result = await session.execute(
        select(Evaluation, Conversation)
        .join(Conversation, Evaluation.conversation_id == Conversation.id)
        .where(
            Evaluation.project_id == project_id,
            Evaluation.agent_id == agent_id,
            Evaluation.score < score_threshold,
            Evaluation.score.is_not(None),
        )
        .order_by(Evaluation.evaluated_at.desc())
        .limit(limit)
    )
    rows = result.all()
    return [
        {
            "conv_public_id": conv.public_id,
            "score": eval_.score,
            "topic": eval_.topic,
            "summary": eval_.summary,
            "frustration": eval_.frustration,
            "resolution": eval_.resolution,
            "scope_violation": eval_.scope_violation,
            "tone": eval_.tone,
        }
        for eval_, conv in rows
    ]


async def _persist_pattern(
    session: AsyncSession,
    *,
    project: Project,
    sub_agent: str,
    raw: _RawPattern,
    prompt_before: str,
    prompt_version_label: str,
) -> str:
    public_id = f"imp_{uuid.uuid4().hex[:16]}"
    improvement = Improvement(
        id=uuid.uuid4(),
        public_id=public_id,
        project_id=project.id,
        org_id=project.org_id,
        agent_slug=sub_agent,
        pattern=raw.pattern,
        root_cause=raw.root_cause,
        prompt_before=prompt_before,
        prompt_after=raw.prompt_after,
        impact_estimate=raw.impact_estimate,
        affected_conv_ids=raw.conv_examples,
        sample_excerpts=raw.sample_excerpts,
        prompt_version_label=prompt_version_label,
        status="pending",
    )
    session.add(improvement)
    await session.flush()
    return public_id


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_optimization_loop(
    input_: OptimizationLoopInput, db: AsyncSession
) -> OptimizationLoopOutput:
    """Detect failure patterns per sub-agent and persist as Improvement rows.

    `db` must be an open AsyncSession. The caller is responsible for committing
    or rolling back — this function calls `flush()` but not `commit()` so that
    dry_run callers can rollback cleanly.
    """
    logger.info(
        "[LOOP] starting project=%s limit=%d sub_agents=%s dry_run=%s",
        input_.project_public_id,
        input_.limit,
        input_.sub_agents,
        input_.dry_run,
    )
    started_at = datetime.now(timezone.utc)

    project = await _resolve_project(db, input_.project_public_id)

    # Build Anthropic client (reuses key from settings, same pattern as AnthropicProvider)
    from anthropic import AsyncAnthropic

    from app.core.config import get_settings

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured — cannot run loop")
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    total_cost = Decimal("0")
    all_patterns: list[PatternDetected] = []
    persisted_ids: list[str] = []
    sub_agents_analyzed = 0

    for sub_agent in input_.sub_agents:
        logger.info("[LOOP] processing sub_agent=%s", sub_agent)

        # Resolve agent by slug
        agent_result = await db.execute(
            select(Agent).where(
                Agent.project_id == project.id,
                Agent.slug == sub_agent,
            )
        )
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            logger.warning(
                "[LOOP] sub_agent=%s: no agent with slug=%r in project=%s — skipping",
                sub_agent,
                sub_agent,
                input_.project_public_id,
            )
            continue

        failed_evals = await _load_failed_evals(
            db,
            project_id=project.id,
            agent_id=agent.id,
            limit=input_.limit,
        )

        if len(failed_evals) < 10:
            logger.warning(
                "[LOOP] sub_agent=%s: only %d failed evals (< 10) — skipping",
                sub_agent,
                len(failed_evals),
            )
            continue

        prompt_before = _load_prompt_file(sub_agent, input_.prompt_version_label)

        if input_.dry_run:
            logger.info(
                "[LOOP] dry_run=True — skipping Sonnet call for sub_agent=%s "
                "(%d evals would be sent)",
                sub_agent,
                len(failed_evals),
            )
            sub_agents_analyzed += 1
            continue

        raw_patterns, call_cost = await _call_sonnet(
            sub_agent=sub_agent,
            eval_summaries=failed_evals,
            prompt_version_label=input_.prompt_version_label,
            anthropic_client=anthropic_client,
        )
        total_cost += call_cost
        sub_agents_analyzed += 1

        for raw in raw_patterns:
            # Use prompt_before from Sonnet if provided, else from filesystem
            effective_prompt_before = raw.prompt_before or prompt_before

            imp_id = await _persist_pattern(
                db,
                project=project,
                sub_agent=sub_agent,
                raw=raw,
                prompt_before=effective_prompt_before,
                prompt_version_label=input_.prompt_version_label,
            )
            persisted_ids.append(imp_id)
            all_patterns.append(
                PatternDetected(
                    sub_agent=sub_agent,
                    pattern=raw.pattern,
                    root_cause=raw.root_cause,
                    affected_conv_ids=raw.conv_examples,
                    prompt_before=effective_prompt_before,
                    prompt_after=raw.prompt_after,
                    impact_estimate=raw.impact_estimate,
                    sample_excerpts=raw.sample_excerpts,
                )
            )

    if not input_.dry_run and persisted_ids:
        await db.commit()
        logger.info(
            "[LOOP] committed %d improvements for project=%s",
            len(persisted_ids),
            input_.project_public_id,
        )

    completed_at = datetime.now(timezone.utc)
    output = OptimizationLoopOutput(
        project_public_id=input_.project_public_id,
        started_at=started_at,
        completed_at=completed_at,
        sub_agents_analyzed=sub_agents_analyzed,
        patterns=all_patterns,
        sonnet_cost_usd=float(total_cost),
        improvements_persisted_ids=persisted_ids,
    )
    logger.info(
        "[LOOP] done project=%s sub_agents_analyzed=%d patterns=%d cost_usd=%.6f",
        input_.project_public_id,
        sub_agents_analyzed,
        len(all_patterns),
        float(total_cost),
    )
    return output
