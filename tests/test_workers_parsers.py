"""Parser tests (EQUIP-62).

Use the 3 CSV fixtures: happy (10 conv), dirty (10 conv with BOM/tz/emoji),
big (200 conv). The dirty fixture is the meaningful one — it must NOT crash
and it must surface warnings cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.workers.parsers import get_parser

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestWatiParser:
    def test_happy_10_yields_10_conversations(self):
        parser = get_parser("wati")
        convs = list(parser(_read("wati_happy_10.csv")))
        assert len(convs) == 10
        for conv in convs:
            assert conv.external_id.startswith("conv-h")
            assert conv.platform == "wati"
            assert len(conv.messages) == 3
            assert conv.started_at is not None
            assert conv.started_at.tzinfo == UTC

    def test_dirty_csv_does_not_crash(self):
        parser = get_parser("wati")
        convs = list(parser(_read("wati_dirty_10.csv")))
        assert len(convs) == 10
        # Emoji preserved verbatim.
        assert any("🚀" in m.content or "🎉" in m.content for c in convs for m in c.messages)
        # All timestamps coerced to UTC.
        for c in convs:
            for m in c.messages:
                assert m.timestamp.tzinfo == UTC

    def test_dirty_csv_strips_bom(self):
        """File starts with UTF-8 BOM; the parser must not treat it as a column."""
        parser = get_parser("wati")
        convs = list(parser(_read("wati_dirty_10.csv")))
        # If BOM weren't stripped, conv ids would start with the BOM byte
        # sequence and length 0 — we'd get 0 conversations.
        assert len(convs) == 10

    def test_big_200_yields_200_conversations(self):
        parser = get_parser("wati")
        convs = list(parser(_read("wati_big_200.csv")))
        assert len(convs) == 200
        # 200 conv * 5 msg = 1000 messages total.
        total_messages = sum(len(c.messages) for c in convs)
        assert total_messages == 1000

    def test_messages_sorted_by_timestamp(self):
        parser = get_parser("wati")
        convs = list(parser(_read("wati_big_200.csv")))
        for conv in convs:
            ts = [m.timestamp for m in conv.messages]
            assert ts == sorted(ts)


class TestWhatsAppParser:
    """Cubre los dos dialectos de export .txt: iOS (corchetes) y Android (guión)."""

    def test_ios_fixture_regression(self):
        parser = get_parser("whatsapp")
        convs = list(parser(_read("samples_jocha_export.txt")))
        assert len(convs) == 1
        conv = convs[0]
        assert conv.platform == "whatsapp"
        assert conv.external_id.startswith("wa-")
        assert conv.contact_name == "JOCHA"
        assert len(conv.messages) == 75  # una línea = un mensaje en el fixture
        # Primer remitente (SAMPLES ROPA) = negocio → assistant.
        assert conv.messages[0].role == "assistant"
        assert all(m.timestamp.tzinfo == UTC for m in conv.messages)

    def test_ios_multiline_continuation(self):
        text = (
            "[22/03/2025, 06:11:02] Negocio: Hola\n"
            "sigue el mensaje\n"
            "\n"
            "y más\n"
            "[22/03/2025, 06:12:00] Cliente: Ok\n"
        )
        convs = list(get_parser("whatsapp")(text.encode()))
        assert len(convs) == 1
        msgs = convs[0].messages
        assert len(msgs) == 2
        # El strip() por línea del parser colapsa las líneas en blanco
        # intermedias (comportamiento pre-existente, se mantiene intacto).
        assert msgs[0].content == "Hola\nsigue el mensaje\ny más"
        assert msgs[1].content == "Ok"

    def test_android_es_ar_24h_multiline(self):
        text = (
            "22/3/2025 14:05 - Soporte Acme: Hola, ¿en qué te ayudo?\n"
            "22/3/2025 14:06 - Cliente: Tengo un problema\n"
            "con mi pedido\n"
            "22/3/2025 14:07 - Soporte Acme: Lo reviso\n"
        )
        convs = list(get_parser("whatsapp")(text.encode()))
        assert len(convs) == 1
        msgs = convs[0].messages
        assert len(msgs) == 3
        # 22 > 12 → DD/MM; 14:05 ART = 17:05 UTC.
        assert msgs[0].timestamp == datetime(2025, 3, 22, 17, 5, tzinfo=UTC)
        assert msgs[0].role == "assistant"
        assert msgs[1].content == "Tengo un problema\ncon mi pedido"
        assert convs[0].contact_name == "Cliente"

    def test_android_en_us_ampm_mmdd(self):
        text = (
            "3/22/25, 6:11 PM - Support: Hi there\n"
            "3/22/25, 6:12 PM - John: I need help\n"
            "3/23/25, 12:05 AM - John: are you there?\n"
            "3/23/25, 12:30 PM - Support: Sure\n"
        )
        convs = list(get_parser("whatsapp")(text.encode()))
        assert len(convs) == 1
        msgs = convs[0].messages
        assert len(msgs) == 4
        # Segundo campo 22 > 12 → MM/DD; 6:11 PM ART = 21:11 UTC.
        assert msgs[0].timestamp == datetime(2025, 3, 22, 21, 11, tzinfo=UTC)
        # 12:05 AM = 00:05 ART = 03:05 UTC (medianoche, no mediodía).
        assert msgs[2].timestamp == datetime(2025, 3, 23, 3, 5, tzinfo=UTC)
        # 12:30 PM = 12:30 ART = 15:30 UTC.
        assert msgs[3].timestamp == datetime(2025, 3, 23, 15, 30, tzinfo=UTC)

    def test_android_es_ampm_con_puntos(self):
        # Variante de locale es: "p. m." con puntos y espacio.
        text = "22/3/2025, 6:11 p. m. - Soporte: Hola\n22/3/2025, 6:12 p. m. - Cliente: Buenas\n"
        convs = list(get_parser("whatsapp")(text.encode()))
        msgs = convs[0].messages
        assert len(msgs) == 2
        # 6:11 p. m. = 18:11 ART = 21:11 UTC.
        assert msgs[0].timestamp == datetime(2025, 3, 22, 21, 11, tzinfo=UTC)

    def test_android_caracteres_invisibles(self):
        # U+200E/U+200F al inicio de línea, U+202F antes de AM/PM (export real).
        text = (
            "\u200e3/22/25, 6:11\u202fPM - Support: Hi\n\u200f3/22/25, 6:12\u202fPM - John: Hello\n"
        )
        convs = list(get_parser("whatsapp")(text.encode("utf-8")))
        assert len(convs) == 1
        assert len(convs[0].messages) == 2
        assert convs[0].messages[0].content == "Hi"

    def test_android_system_message_intercalado(self):
        text = (
            "22/3/2025 14:04 - Los mensajes y las llamadas están cifrados de "
            "extremo a extremo. Nadie fuera de este chat, ni siquiera WhatsApp, "
            "puede leerlos ni escucharlos.\n"
            "22/3/2025 14:05 - Soporte: Hola\n"
            "22/3/2025 14:06 - Cambió tu código de seguridad\n"
            "continuación del aviso de sistema\n"
            "22/3/2025 14:07 - Cliente: Buenas\n"
        )
        convs = list(get_parser("whatsapp")(text.encode()))
        msgs = convs[0].messages
        # Los system messages (y su continuación) se descartan; no ensucian
        # el mensaje anterior ni se toman como remitente/agente.
        assert [m.content for m in msgs] == ["Hola", "Buenas"]
        assert msgs[0].role == "assistant"  # Soporte = primer remitente real
        assert convs[0].contact_name == "Cliente"

    def test_android_fecha_ambigua_asume_ddmm(self):
        text = "5/3/2025 10:00 - A: hola\n5/3/2025 10:01 - B: chau\n"
        convs = list(get_parser("whatsapp")(text.encode()))
        # Ambos campos ≤ 12 → DD/MM (locale AR): 5 de marzo, 10:00 ART = 13:00 UTC.
        assert convs[0].messages[0].timestamp == datetime(2025, 3, 5, 13, 0, tzinfo=UTC)

    def test_sin_headers_no_yield(self):
        convs = list(get_parser("whatsapp")(b"esto no es un export\nde whatsapp\n"))
        assert convs == []


class TestUnknownPlatform:
    def test_get_parser_raises_for_unknown(self):
        import pytest

        with pytest.raises(KeyError):
            get_parser("unknown_platform")


class TestRespondioParser:
    def test_minimal_csv(self, tmp_path):
        csv_text = (
            "chat_id,sender_type,message,sent_at\n"
            "abc,customer,Hola,2026-04-01T10:00:00Z\n"
            "abc,agent,Hola que tal,2026-04-01T10:00:30Z\n"
        )
        path = tmp_path / "respondio.csv"
        path.write_text(csv_text)
        parser = get_parser("respondio")
        convs = list(parser(path.read_bytes()))
        assert len(convs) == 1
        assert convs[0].external_id == "abc"
        assert convs[0].platform == "respondio"
        roles = [m.role for m in convs[0].messages]
        assert roles == ["user", "assistant"]


class TestCustomSDKParser:
    def test_uses_wati_format_with_custom_sdk_label(self, tmp_path):
        csv_text = "conversation_id,role,content,timestamp\nx,user,hi,2026-04-01T10:00:00Z\n"
        path = tmp_path / "custom.csv"
        path.write_text(csv_text)
        parser = get_parser("custom_sdk")
        convs = list(parser(path.read_bytes()))
        assert len(convs) == 1
        assert convs[0].platform == "custom_sdk"
