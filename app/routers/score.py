"""Score agregado por proyecto.

Separado de `conversations.py` (era un endpoint de lectura que conceptualmente es
una métrica de dashboard). Promedio 0-100 de las Evaluation del proyecto.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.models import Evaluation, Project

router = APIRouter(prefix="/api/v1", tags=["score"])


class ProjectScoreResponse(BaseModel):
    project_public_id: str
    score: int | None = Field(description="0-100 average; null if no evaluations yet")
    evaluation_count: int


@router.get(
    "/projects/{project_public_id}/score",
    response_model=ProjectScoreResponse,
)
async def get_project_score(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> ProjectScoreResponse:
    project_id = (
        await session.execute(
            select(Project.id).where(Project.public_id == project_public_id)
        )
    ).scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")

    row = (
        await session.execute(
            select(
                func.avg(Evaluation.score).label("avg_score"),
                func.count(Evaluation.id).label("count"),
            ).where(Evaluation.project_id == project_id, Evaluation.score.isnot(None))
        )
    ).one()
    avg = int(round(row.avg_score)) if row.avg_score is not None else None
    return ProjectScoreResponse(
        project_public_id=project_public_id,
        score=avg,
        evaluation_count=int(row.count or 0),
    )
