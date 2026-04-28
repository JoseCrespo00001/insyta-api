"""Scheduler tests (EQUIP-102).

We test the agent-config helper and verify the Beat schedule entry exists.
End-to-end run-the-task tests would require a live DB + worker; we keep those
out of unit tests and rely on the integration layer to exercise them.
"""

from __future__ import annotations

from app.services.celery_app import celery_app
from app.workers.scheduler import DEFAULT_IDLE_MINUTES, _idle_minutes_for


class _FakeAgent:
    def __init__(self, config: dict | None) -> None:
        self.config = config


class TestIdleMinutesConfig:
    def test_default_when_no_config(self):
        assert _idle_minutes_for(_FakeAgent(None)) == DEFAULT_IDLE_MINUTES

    def test_default_when_key_missing(self):
        assert _idle_minutes_for(_FakeAgent({"foo": "bar"})) == DEFAULT_IDLE_MINUTES

    def test_custom_value_honored(self):
        assert _idle_minutes_for(_FakeAgent({"completion_idle_minutes": 10})) == 10

    def test_string_value_coerced(self):
        assert _idle_minutes_for(_FakeAgent({"completion_idle_minutes": "45"})) == 45

    def test_invalid_value_falls_back_to_default(self):
        assert (
            _idle_minutes_for(_FakeAgent({"completion_idle_minutes": "not-a-number"}))
            == DEFAULT_IDLE_MINUTES
        )

    def test_zero_clamped_to_one(self):
        assert _idle_minutes_for(_FakeAgent({"completion_idle_minutes": 0})) == 1


class TestBeatSchedule:
    def test_beat_entry_registered(self):
        schedule = celery_app.conf.beat_schedule
        assert "close-stale-conversations" in schedule
        entry = schedule["close-stale-conversations"]
        assert entry["task"] == "app.workers.scheduler.close_stale_conversations"

    def test_task_registered_in_celery_registry(self):
        assert "app.workers.scheduler.close_stale_conversations" in celery_app.tasks
