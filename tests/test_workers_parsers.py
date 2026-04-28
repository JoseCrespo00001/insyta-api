"""Parser tests (EQUIP-62).

Use the 3 CSV fixtures: happy (10 conv), dirty (10 conv with BOM/tz/emoji),
big (200 conv). The dirty fixture is the meaningful one — it must NOT crash
and it must surface warnings cleanly.
"""

from __future__ import annotations

from datetime import timezone
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
            assert conv.started_at.tzinfo == timezone.utc

    def test_dirty_csv_does_not_crash(self):
        parser = get_parser("wati")
        convs = list(parser(_read("wati_dirty_10.csv")))
        assert len(convs) == 10
        # Emoji preserved verbatim.
        assert any(
            "🚀" in m.content or "🎉" in m.content for c in convs for m in c.messages
        )
        # All timestamps coerced to UTC.
        for c in convs:
            for m in c.messages:
                assert m.timestamp.tzinfo == timezone.utc

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
        csv_text = (
            "conversation_id,role,content,timestamp\n"
            "x,user,hi,2026-04-01T10:00:00Z\n"
        )
        path = tmp_path / "custom.csv"
        path.write_text(csv_text)
        parser = get_parser("custom_sdk")
        convs = list(parser(path.read_bytes()))
        assert len(convs) == 1
        assert convs[0].platform == "custom_sdk"
