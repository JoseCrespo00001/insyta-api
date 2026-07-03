"""AUD-3.4: detección de audio/imagen sin transcripción."""

from __future__ import annotations

from app.services.validators.multimodal import find_unprocessed_media


def test_audio_without_transcript_flagged():
    msgs = [
        {"seq": 0, "role": "user", "message_type": "text"},
        {"seq": 1, "role": "user", "message_type": "audio", "media_transcript": None},
    ]
    res = find_unprocessed_media(msgs)
    assert len(res) == 1
    assert res[0].turn_id == 1
    assert res[0].message_type == "audio"


def test_audio_with_transcript_ok():
    msgs = [
        {"seq": 0, "message_type": "audio", "media_transcript": "hola quiero limpieza"},
    ]
    assert find_unprocessed_media(msgs) == []


def test_image_without_transcript_flagged():
    msgs = [{"seq": 3, "message_type": "image"}]
    res = find_unprocessed_media(msgs)
    assert res[0].turn_id == 3
    assert res[0].message_type == "image"


def test_text_never_flagged():
    msgs = [{"seq": 0, "message_type": "text"}, {"seq": 1}]  # default text
    assert find_unprocessed_media(msgs) == []


def test_turn_id_falls_back_to_index():
    msgs = [{"message_type": "audio"}]  # sin seq
    assert find_unprocessed_media(msgs)[0].turn_id == 0
