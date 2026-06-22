#!/usr/bin/env python
"""Refresca material fuente para el skill file de Langflow.

NO reescribe `app/llm/knowledge/langflow.md` automáticamente (eso requiere
criterio): baja la doc/changelog oficial a `langflow-docs-raw.md` para que lo
revises y vuelques los cambios al skill file curado. El auditor recarga el skill
file en caliente, así que no hace falta reiniciar.

Uso:
    uv run python scripts/update_langflow_knowledge.py
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "app" / "llm" / "knowledge"
RAW_OUT = KNOWLEDGE_DIR / "langflow-docs-raw.md"

# Fuentes candidatas (best-effort). Las que respondan se concatenan al raw.
SOURCES = [
    ("docs · llms-full.txt", "https://docs.langflow.org/llms-full.txt"),
    ("docs · llms.txt", "https://docs.langflow.org/llms.txt"),
    (
        "github · releases",
        "https://api.github.com/repos/langflow-ai/langflow/releases?per_page=5",
    ),
]

MAX_BYTES = 600_000  # corte por fuente para no escribir megas


def _fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "insyta-langflow-sync"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(MAX_BYTES).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"  ✗ {url}: {exc}", file=sys.stderr)
        return None


def main() -> int:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()
    parts = [f"# Langflow docs (raw) — fetched {stamp}\n"]
    ok = 0
    for label, url in SOURCES:
        print(f"→ {label}: {url}")
        body = _fetch(url)
        if body:
            ok += 1
            parts.append(f"\n\n=== {label} :: {url} ===\n\n{body}")
            print(f"  ✓ {len(body)} chars")

    if ok == 0:
        print(
            "\nNo se pudo bajar ninguna fuente (¿sin red?). El skill file curado "
            "sigue vigente; editalo a mano si hace falta.",
            file=sys.stderr,
        )
        return 1

    RAW_OUT.write_text("".join(parts), encoding="utf-8")
    print(f"\nGuardado: {RAW_OUT}")
    print(
        "Revisá ese raw y volcá los cambios relevantes a "
        "app/llm/knowledge/langflow.md (subí 'version' y 'updated' en el "
        "frontmatter). El auditor lo toma en caliente."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
