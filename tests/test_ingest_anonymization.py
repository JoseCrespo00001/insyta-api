"""AUD-0.1: el ingest popula content_anonymized (PII nunca cruda al LLM)."""

from __future__ import annotations

import uuid

from app.workers import processor


class _DTO:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content
        self.timestamp = None


class _FakeSession:
    def __init__(self) -> None:
        self.stmt = None

    async def execute(self, stmt):
        self.stmt = stmt
        return None


async def test_persist_messages_populates_content_anonymized():
    phone = "+5491122334455"
    session = _FakeSession()
    dtos = [
        _DTO("user", f"mi telefono es {phone}"),
        _DTO("assistant", "gracias, te contacto"),
    ]
    n = await processor._persist_messages(
        session,
        conversation_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        dtos=dtos,
    )
    assert n == 2

    params = session.stmt.compile().params
    anon_values = [v for k, v in params.items() if k.startswith("content_anonymized")]
    assert len(anon_values) == 2
    # El teléfono crudo no aparece en ningún content_anonymized; hay un token [KIND_..].
    joined = "\n".join(str(v) for v in anon_values)
    assert phone not in joined
    assert "[" in joined and "]" in joined
