"""Tests for the Celery app / queues / retry / dead-letter (T-2.1, SPEC §5, §8).

Unit tests run Celery in eager (in-memory) mode — no real broker is required.
The dead-letter sink is exercised through the base task ``on_failure`` hook so
the "poison task after N retries" path is verified without Redis.
"""

from __future__ import annotations

import pytest
from celery import Celery

from iosforge.common import queue
from iosforge.common.queue import (
    STAGE_QUEUES,
    DeadLetter,
    DeadLetterRecord,
    PipelineTask,
    celery_app,
    make_celery_app,
    queue_for_stage,
)
from iosforge.common.types import Stage


def test_celery_app_uses_redis_url_from_settings() -> None:
    """Broker and result backend come from ``get_settings().redis_url``."""
    from iosforge.common.config import get_settings

    redis_url = get_settings().redis_url
    assert celery_app.conf.broker_url == redis_url
    assert celery_app.conf.result_backend == redis_url


def test_make_celery_app_returns_independent_instance() -> None:
    """The factory builds a configured, distinct Celery instance each call."""
    app = make_celery_app(broker_url="redis://localhost:6379/9")
    assert isinstance(app, Celery)
    assert app is not celery_app
    assert app.conf.broker_url == "redis://localhost:6379/9"


def test_all_pipeline_stages_have_named_queues() -> None:
    """Every pipeline stage maps to its own named queue (SPEC §5)."""
    expected = {
        "discovery",
        "acquisition",
        "walkthrough",
        "codegen",
        "delivery",
    }
    assert expected <= set(STAGE_QUEUES.values())
    declared = {q.name for q in celery_app.conf.task_queues}
    assert expected <= declared


def test_queue_for_stage_maps_codegen_for_analysis_and_codegen() -> None:
    """Both ANALYSIS and CODEGEN run on the single ``codegen`` worker queue."""
    assert queue_for_stage(Stage.DISCOVERY) == "discovery"
    assert queue_for_stage(Stage.ACQUISITION) == "acquisition"
    assert queue_for_stage(Stage.WALKTHROUGH) == "walkthrough"
    assert queue_for_stage(Stage.ANALYSIS) == "codegen"
    assert queue_for_stage(Stage.CODEGEN) == "codegen"
    assert queue_for_stage(Stage.DELIVERY) == "delivery"


@pytest.fixture
def eager_app() -> Celery:
    """A Celery app in eager mode so tasks run synchronously, in-process."""
    app = make_celery_app(broker_url="memory://", result_backend="cache+memory://")
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = False
    return app


def test_pipeline_task_routes_each_stage_through_its_queue(
    eager_app: Celery,
) -> None:
    """A ping task registered per stage carries the stage's queue (DoD)."""

    def _ping() -> str:
        return "pong"

    for stage in Stage:
        task = eager_app.task(
            base=PipelineTask,
            name=f"ping.{stage.value}",
            queue=queue_for_stage(stage),
        )(_ping)
        assert task.queue == queue_for_stage(stage)
        # Eager execution: the ping task completes through its queue config.
        assert task.delay().get() == "pong"


def test_pipeline_task_has_exponential_backoff_retry_policy() -> None:
    """The reusable base task carries autoretry + exponential backoff defaults."""
    assert PipelineTask.retry_backoff is True
    assert PipelineTask.retry_jitter is True
    assert PipelineTask.max_retries >= 1
    assert Exception in PipelineTask.autoretry_for


def test_dead_letter_record_captures_diagnostics() -> None:
    """A dead-letter record carries job/stage/reason/traceback identifiers."""
    record = DeadLetterRecord(
        task_id="abc-123",
        task_name="ping.discovery",
        job_id="job-1",
        stage="discovery",
        reason="boom",
        traceback="Traceback ...",
    )
    payload = record.as_dict()
    assert payload["task_id"] == "abc-123"
    assert payload["job_id"] == "job-1"
    assert payload["stage"] == "discovery"
    assert payload["reason"] == "boom"
    assert "Traceback" in payload["traceback"]


def test_poison_task_lands_in_dead_letter_after_retries(
    eager_app: Celery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task that always fails is recorded in the dead-letter sink (DoD).

    With ``max_retries`` exhausted, the base task's ``on_failure`` hook must
    push exactly one diagnostic record into the dead-letter sink.
    """
    captured: list[DeadLetterRecord] = []
    monkeypatch.setattr(DeadLetter, "record", staticmethod(captured.append))

    @eager_app.task(
        base=PipelineTask,
        name="ping.poison",
        queue=queue_for_stage(Stage.DISCOVERY),
        max_retries=2,
        default_retry_delay=0,
        retry_backoff=False,
        retry_jitter=False,
    )
    def poison(job_id: str) -> None:
        raise ValueError("poisoned")

    # Eager run without propagation: retries recurse in-process until max_retries
    # is exhausted, then on_failure pushes one dead-letter record.
    result = poison.apply(args=("job-42",))
    assert result.failed()
    assert isinstance(result.result, ValueError)

    assert len(captured) == 1
    record = captured[0]
    assert record.job_id == "job-42"
    assert record.stage == "discovery"
    assert "poisoned" in record.reason
    assert record.task_name == "ping.poison"


def test_dead_letter_redis_sink_key_is_namespaced() -> None:
    """The default Redis sink uses a stable, namespaced key (integration point)."""
    assert DeadLetter.REDIS_KEY.startswith("iosforge:")
    assert "dead" in DeadLetter.REDIS_KEY


def test_module_exports_celery_app() -> None:
    """The module-level ``celery_app`` is the canonical worker entrypoint."""
    assert isinstance(queue.celery_app, Celery)
    assert queue.celery_app.main == "iosforge"
