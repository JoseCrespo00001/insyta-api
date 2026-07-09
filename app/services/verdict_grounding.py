"""V2 (Prompt 4/4): post-validador de grounding del veredicto del judge.

El judge a veces cita en su JUSTIFICACIÓN un código/precio/dato concreto que NO
aparece ni en la conversación evaluada ni en las fuentes de verdad — es decir,
alucina en su propia explicación (caso real detectado: "LOC_D073"). Cuando eso
pasa NO exponemos el dato como hecho: bajamos la confianza del veredicto (la
severidad, que aguas abajo baja la `confidence` del rubric vía map_eval_to_rubric)
y lo marcamos para revisión.

Alcance deliberado: "dato concreto verificable" = **códigos** (mezcla de
mayúsculas + dígitos: LOC_D073, MI-88888, BIENVENIDO25, VERANO70, OVERRIDE-2026)
y **precios** ($45.990, $1). Palabras sueltas o promos solo-letras (ENVIOGRATIS,
DAN) no se chequean para no generar falsos positivos. Corre in-process; nunca
manda contenido a un LLM.
"""

from __future__ import annotations

import re

# Código/dato: token alfanumérico en mayúsculas con AL MENOS un dígito y una letra.
_CODE_RE = re.compile(r"[A-Z0-9]{2,}(?:[_\-][A-Z0-9]+)*")
# Precio: $ seguido de dígitos (con separadores de miles/decimales).
_PRICE_RE = re.compile(r"\$\s?\d[\d.,]*")


def _norm(s: str | None) -> str:
    """Normaliza para comparar: sin espacios, minúsculas."""
    return re.sub(r"\s+", "", (s or "").lower())


def cited_tokens(note: str | None) -> list[str]:
    """Códigos y precios concretos citados en la justificación (dedup, en orden)."""
    codes = [
        t
        for t in _CODE_RE.findall(note or "")
        if len(t) >= 4 and any(c.isdigit() for c in t) and any(c.isalpha() for c in t)
    ]
    prices = _PRICE_RE.findall(note or "")
    seen: set[str] = set()
    out: list[str] = []
    for t in [*codes, *prices]:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def ungrounded_tokens(note: str | None, evidence: str | None, sources: str | None) -> list[str]:
    """Tokens citados en `note` que NO aparecen ni en `evidence` (la conversación
    evaluada) ni en `sources` (las fuentes de verdad). Comparación normalizada."""
    haystack = _norm(evidence) + "␟" + _norm(sources)
    return [t for t in cited_tokens(note) if _norm(t) and _norm(t) not in haystack]


# Sufijo con el que marcamos una justificación de baja confianza (visible en el reporte).
UNVERIFIED_TAG = "⚠ dato no verificado"

_SEVERITY_DOWNGRADE = {"critica": "media", "alta": "media"}


def _downgrade_severity(severity: str | None) -> str | None:
    """Un veredicto que cita un dato inexistente no puede sostener severidad alta."""
    return _SEVERITY_DOWNGRADE.get(severity or "", severity)


def guard_verdicts(verdicts: list, evidence: str | None, sources: str | None):
    """Aplica el guard a una lista de veredictos del judge. Para cada veredicto cuya
    justificación cite un dato ausente: baja la severidad (→ baja la confianza aguas
    abajo) y anexa el tag de "no verificado" a la nota. NO descarta el veredicto: lo
    deja marcado para revisión, en vez de exponer el dato como hecho.

    Devuelve `(veredictos_ajustados, n_marcados, seqs_marcados)`. Puro: no toca DB.
    """
    adjusted: list = []
    flagged_seqs: list[int] = []
    for v in verdicts:
        ung = ungrounded_tokens(getattr(v, "note", None), evidence, sources)
        if ung and getattr(v, "note", None):
            flagged_seqs.append(getattr(v, "seq", -1))
            new_note = f"{v.note} [{UNVERIFIED_TAG}: {', '.join(ung)}]"[:400]
            adjusted.append(
                v.model_copy(
                    update={
                        "note": new_note,
                        "severity": _downgrade_severity(getattr(v, "severity", None)),
                    }
                )
            )
        else:
            adjusted.append(v)
    return adjusted, len(flagged_seqs), flagged_seqs


__all__ = [
    "UNVERIFIED_TAG",
    "cited_tokens",
    "guard_verdicts",
    "ungrounded_tokens",
]
