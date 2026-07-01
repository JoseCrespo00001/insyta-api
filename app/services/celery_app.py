"""Celery application factory.

Single source of truth for the Celery configuration. Workers, schedulers, and
test fixtures all import `celery_app` from here.

Reliability defaults:
- `acks_late=True`: a task is only acked once it returns. If a worker crashes
  mid-task the broker re-queues it. Critical for paid LLM calls — we'd rather
  occasionally double-charge (idempotent dedupe handles that) than silently
  lose evaluations.
- `task_reject_on_worker_lost=True`: same intent for worker SIGKILL.
- `worker_prefetch_multiplier=1`: don't pre-grab work; pair with acks_late so
  a crashed worker only loses the *one* task it was actively running.
"""

from __future__ import annotations

import logging

from celery import Celery

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# Tracing OTel→Phoenix por worker desactivado por ahora. Para reactivar,
# descomentar el import de arriba y este bloque:
# from celery.signals import worker_process_init
#
# @worker_process_init.connect
# def _init_worker_tracing(**_kwargs) -> None:
#     """Activa el tracing OTel→Phoenix en cada proceso worker (idempotente).
#
#     Los spans de evaluate_conversation/run_audit sólo se exportan si el worker
#     inicializó el TracerProvider; sin esto quedaban como NoOp.
#     """
#     from app.observability import init_tracing
#
#     init_tracing()


def _build_celery() -> Celery:
    settings = get_settings()
    app = Celery(
        "insyta",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=[
            "app.workers.processor",
            "app.workers.evaluator",
            "app.workers.audit",
        ],
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_default_queue="default",
        task_track_started=True,
        result_expires=3600,
    )
    # No periodic tasks for now — ingestion is CSV-driven and audits are
    # user-triggered. (Live/real-time scheduling is deferred to a later sprint.)
    app.conf.beat_schedule = {}
    return app


celery_app: Celery = _build_celery()
