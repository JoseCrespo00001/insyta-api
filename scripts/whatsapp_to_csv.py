"""Convierte un export de WhatsApp (.txt) al CSV que Insyta ingiere.

Formato de entrada (una línea por mensaje; líneas siguientes sin corchete se
concatenan al mensaje anterior — mensajes multilínea):

    [22/03/2025, 06:11:02] SAMPLES ROPA: ¡Hola! ...
    [22/03/2025, 17:06:55] JOCHA: #14763

Salida CSV (columnas que espera el parser de Insyta):

    conversation_id, contact_name, role, content, timestamp

- role: el remitente == --agent  -> "assistant"; cualquier otro -> "user".
- timestamp: DD/MM/YYYY, HH:MM:SS  ->  ISO 8601 con el offset dado (default -03:00).

Uso:
    uv run python scripts/whatsapp_to_csv.py export.txt salida.csv \\
        --agent "SAMPLES ROPA" --conversation-id samples-jocha-001 \\
        --contact-name "Jose Crespo"
"""

from __future__ import annotations

import argparse
import csv
import re

LINE_RE = re.compile(
    r"^\[(\d{1,2})/(\d{1,2})/(\d{4}),?\s+(\d{1,2}):(\d{2}):(\d{2})\]\s+([^:]+?):\s?(.*)$"
)


def convert(
    text: str,
    *,
    agent: str,
    conversation_id: str,
    contact_name: str,
    offset: str,
) -> list[dict]:
    rows: list[dict] = []
    for raw in text.splitlines():
        m = LINE_RE.match(raw)
        if not m:
            # Continuación del mensaje anterior (multilínea).
            if rows and raw.strip():
                rows[-1]["content"] += "\n" + raw.rstrip()
            continue
        dd, mm, yyyy, hh, mi, ss, sender, content = m.groups()
        ts = f"{yyyy}-{int(mm):02d}-{int(dd):02d}T{int(hh):02d}:{mi}:{ss}{offset}"
        role = "assistant" if sender.strip() == agent.strip() else "user"
        rows.append(
            {
                "conversation_id": conversation_id,
                "contact_name": contact_name,
                "role": role,
                "content": content,
                "timestamp": ts,
            }
        )
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--agent", required=True, help="Remitente que es el bot/negocio")
    p.add_argument("--conversation-id", required=True)
    p.add_argument("--contact-name", default="")
    p.add_argument("--offset", default="-03:00")
    args = p.parse_args()

    with open(args.input, encoding="utf-8") as fh:
        text = fh.read()
    rows = convert(
        text,
        agent=args.agent,
        conversation_id=args.conversation_id,
        contact_name=args.contact_name,
        offset=args.offset,
    )
    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "conversation_id",
                "contact_name",
                "role",
                "content",
                "timestamp",
            ],
        )
        w.writeheader()
        w.writerows(rows)
    n_user = sum(1 for r in rows if r["role"] == "user")
    n_assistant = len(rows) - n_user
    print(
        f"OK: {len(rows)} mensajes ({n_user} user / {n_assistant} assistant) -> {args.output}"
    )


if __name__ == "__main__":
    main()
