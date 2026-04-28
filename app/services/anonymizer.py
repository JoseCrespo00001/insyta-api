"""PII anonymizer (EQUIP-63).

Two-layer pipeline:
  1. Regex pass for high-precision Argentine entities (phone +54, email,
     credit-card-like 13-19 digits, DNI 7-8 digits in DNI context).
  2. Presidio NER (spaCy `es_core_news_md`) for PERSON, LOCATION, DATE, etc.

Each finding is replaced with a *reversible* token of the form `[KIND_AABB]`
where AABB is the first 4 chars of a SHA1(secret + original). The mapping
(`token -> original`) is returned so downstream code can de-anonymize when
needed (e.g. customer support tickets).

Design notes:
- Presidio is heavy; we lazy-load on first call so unit tests not exercising
  NER stay fast. If spaCy/Presidio aren't installed (e.g. CI minimal image)
  the anonymizer degrades gracefully to regex-only and logs a warning.
- The regex layer runs first because Presidio sometimes mis-classifies a long
  digit run as an `IBAN_CODE`; we want our explicit Argentine regex to win.
- Order numbers (4-6 digit shopping refs) are **not** anonymized — those are
  business data, not PII.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from threading import Lock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex layer — high-precision PII patterns.
# ---------------------------------------------------------------------------
PHONE_AR = re.compile(
    r"""(?<!\w)
        (?:\+?54[\s\-\.]?)?            # optional country code
        (?:9[\s\-\.]?)?                # mobile prefix
        (?:\(?\d{2,4}\)?[\s\-\.]?)     # area code
        \d{3,4}[\s\-\.]?\d{4}          # subscriber
        (?!\w)
    """,
    re.VERBOSE,
)
EMAIL = re.compile(r"(?<!\w)[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}(?!\w)")
# Credit-card-like: 13-19 digit groups separated by space/dash. Tighter than
# raw `\d{13,19}` (which would eat phone numbers); must look like grouped
# blocks of 4 digits, the canonical card layout, AND pass Luhn.
CREDIT_CARD = re.compile(r"(?<!\w)\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{1,7}(?!\w)")
# DNI: "DNI 12345678" or "DNI: 12.345.678".
DNI = re.compile(r"(?i)\bdni[\s:]*\.?[\s:]*((?:\d[\.\s]?){7,8})")


def _luhn_check(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13:
        return False
    checksum = 0
    parity = len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        checksum += n
    return checksum % 10 == 0


@dataclass
class AnonymizationResult:
    text: str
    replacements: dict[str, str] = field(default_factory=dict)

    def reverse(self, anonymized: str) -> str:
        """Replace every token in `anonymized` with the original value."""
        out = anonymized
        for token, original in self.replacements.items():
            out = out.replace(token, original)
        return out


_TOKEN_SALT = "insyta-anon-v1"


def _short_hash(text: str) -> str:
    return (
        hashlib.sha1((_TOKEN_SALT + text).encode("utf-8"), usedforsecurity=False)
        .hexdigest()[:4]
        .upper()
    )


def _make_token(kind: str, original: str) -> str:
    return f"[{kind}_{_short_hash(original)}]"


# ---------------------------------------------------------------------------
# Presidio (lazy).
# ---------------------------------------------------------------------------
_presidio_lock = Lock()
_presidio_engine = None
_presidio_unavailable = False


def _get_presidio():
    global _presidio_engine, _presidio_unavailable
    if _presidio_engine is not None or _presidio_unavailable:
        return _presidio_engine
    with _presidio_lock:
        if _presidio_engine is not None or _presidio_unavailable:
            return _presidio_engine
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "es", "model_name": "es_core_news_md"}],
                }
            )
            nlp_engine = provider.create_engine()
            _presidio_engine = AnalyzerEngine(
                nlp_engine=nlp_engine, supported_languages=["es"]
            )
            logger.info("[ANONYMIZER] Presidio engine loaded (es_core_news_md)")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ANONYMIZER] Presidio unavailable, falling back to regex-only: %s",
                exc,
            )
            _presidio_unavailable = True
    return _presidio_engine


# Map Presidio entity_type -> our token kind. We deliberately exclude
# DATE_TIME and PHONE_NUMBER:
#   - DATE_TIME: spaCy es model produces too many false positives (greetings,
#     interjections) at any reasonable threshold.
#   - PHONE_NUMBER: covered by the high-precision Argentine regex above.
PRESIDIO_KIND_MAP = {
    "PERSON": "NAME",
    "LOCATION": "LOC",
    "EMAIL_ADDRESS": "EMAIL",
    "CREDIT_CARD": "CARD",
    "IBAN_CODE": "IBAN",
    "IP_ADDRESS": "IP",
    "URL": "URL",
}
PRESIDIO_MIN_SCORE = 0.7

# spaCy `es_core_news_md` overfits on capitalized Spanish words at the start
# of sentences and tags common greetings / interjections / pronouns as PERSON.
# These are never PII; skip any Presidio span whose normalized chunk matches.
PRESIDIO_STOP_WORDS = frozenset(
    {
        "hola",
        "buenas",
        "buenos",
        "chau",
        "gracias",
        "ok",
        "si",
        "no",
        "tal vez",
        "perfecto",
        "genial",
        "dale",
        "listo",
        "bien",
        "che",
        "bueno",
        "señor",
        "señora",
        "sr",
        "sra",
        "usted",
        "ustedes",
        "vos",
    }
)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
def anonymize(text: str) -> AnonymizationResult:
    """Anonymize PII in `text`. Returns text + token->original mapping.

    The same original string maps to the same token across the document, so
    reversal is unambiguous. Tokens use a stable SHA1-based suffix so two runs
    over the same input produce identical output (helps with caching downstream
    LLM calls — same anonymized prompt = same cache hit).
    """
    if not text:
        return AnonymizationResult(text=text)

    replacements: dict[str, str] = {}

    def _sub(kind: str):
        def _do(match: re.Match[str]) -> str:
            original = match.group(0)
            token = _make_token(kind, original)
            replacements[token] = original
            return token

        return _do

    # Layer 1: regex (high precision, run first). Order matters:
    #   EMAIL -> CARD (Luhn-validated) -> PHONE -> DNI
    out = text
    out = EMAIL.sub(_sub("EMAIL"), out)

    def _card_repl(match: re.Match[str]) -> str:
        candidate = match.group(0)
        if not _luhn_check(candidate):
            return candidate
        token = _make_token("CARD", candidate)
        replacements[token] = candidate
        return token

    out = CREDIT_CARD.sub(_card_repl, out)
    out = PHONE_AR.sub(_sub("PHONE"), out)

    # DNI is special — capture group, not whole match.
    def _dni_repl(match: re.Match[str]) -> str:
        digits = match.group(1)
        token = _make_token("DNI", digits)
        replacements[token] = digits
        return match.group(0).replace(digits, token)

    out = DNI.sub(_dni_repl, out)

    # Layer 2: Presidio NER on the regex-redacted text. Skipping if engine
    # unavailable keeps tests fast in environments without spaCy models.
    engine = _get_presidio()
    if engine is not None:
        try:
            results = engine.analyze(
                text=out,
                language="es",
                entities=list(PRESIDIO_KIND_MAP.keys()),
                score_threshold=PRESIDIO_MIN_SCORE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ANONYMIZER] Presidio analyze failed: %s", exc)
            results = []

        # Compute the set of character ranges that already hold a regex token
        # so Presidio cannot retag them (e.g. flag the inner `EMAIL_AAAA` of
        # a bracket as a LOCATION).
        token_ranges: list[tuple[int, int]] = []
        for token in replacements:
            idx = 0
            while True:
                idx = out.find(token, idx)
                if idx == -1:
                    break
                token_ranges.append((idx, idx + len(token)))
                idx += len(token)

        def _overlaps_token(s: int, e: int) -> bool:
            return any(not (e <= ts or s >= te) for ts, te in token_ranges)

        # Apply spans right-to-left so substitution offsets remain valid.
        spans = sorted(
            (
                (r.start, r.end, r.entity_type, r.score)
                for r in results
                if r.entity_type in PRESIDIO_KIND_MAP
            ),
            key=lambda x: -x[0],
        )
        for start, end, etype, _score in spans:
            if _overlaps_token(start, end):
                continue
            chunk = out[start:end]
            if "[" in chunk or "]" in chunk:
                continue
            if chunk.strip().lower() in PRESIDIO_STOP_WORDS:
                continue
            kind = PRESIDIO_KIND_MAP.get(etype, etype)
            token = _make_token(kind, chunk)
            replacements[token] = chunk
            out = out[:start] + token + out[end:]

    return AnonymizationResult(text=out, replacements=replacements)
