"""Celery app, stage queues, retry/backoff policy and dead-letter sink (T-2.1).

This module is the canonical worker entrypoint::

    uv run celery -A iosforge.common.queue worker -Q discovery
    uv run celery -A iosforge.common.queue worker -Q codegen --concurrency=1

Design (SPEC §5, §8):

* **Stage queues** — each pipeline stage gets its own named queue so узкие
  этапы масштабируются независимо. ``ANALYSIS`` and ``CODEGEN`` share the single
  ``codegen`` queue, which is meant to be served by the Claude Code worker with
  ``--concurrency=1`` (that is a *worker* CLI flag, not code — see STACK §18).
* **Retry policy** — :class:`PipelineTask` is a reusable base task with
  exponential backoff + jitter (``autoretry_for`` / ``retry_backoff``). Stage
  tasks subclass it (``base=PipelineTask``) instead of re-declaring retry knobs.
* **Dead-letter** — when retries are exhausted, the base task's ``on_failure``
  hook writes a diagnostic record (job / stage / reason / traceback / task id)
  into :class:`DeadLetter`. The default sink is a namespaced Redis list; this is
  the integration point for the persistent ``AuditLog`` / dead-letter DB model
  in **T-2.2** (see TODO below) — no DB model is touched here.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

from celery import Celery, Task  # type: ignore[import-untyped]

from iosforge.common.config import get_settings
from iosforge.common.logging import get_logger
from iosforge.common.types import Stage

if TYPE_CHECKING:
    from collections.abc import Mapping

_log = get_logger("queue")

# --- Stage -> queue routing ------------------------------------------------- #
# ANALYSIS and CODEGEN both run on the Claude Code worker queue (concurrency=1).
STAGE_QUEUES: dict[Stage, str] = {
    Stage.DISCOVERY: "discovery",
    Stage.ACQUISITION: "acquisition",
    Stage.WALKTHROUGH: "walkthrough",
    Stage.ANALYSIS: "codegen",
    Stage.CODEGEN: "codegen",
    Stage.DELIVERY: "delivery",
}

#: Ordered, de-duplicated list of physical queue names a worker can serve.
QUEUE_NAMES: tuple[str, ...] = tuple(dict.fromkeys(STAGE_QUEUES.values()))

# Retry policy defaults (overridable per-task) — exponential backoff + jitter.
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 5  # seconds, base of the exponential schedule
DEFAULT_RETRY_BACKOFF_MAX = 600  # cap a single backoff delay at 10 minutes


def queue_for_stage(stage: Stage | str) -> str:
    """Return the named queue that serves ``stage``.

    Accepts either a :class:`Stage` or its string value so callers (orchestrator,
    admin) can route without importing the enum.
    """
    key = Stage(stage) if not isinstance(stage, Stage) else stage
    return STAGE_QUEUES[key]


@dataclasses.dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    """Diagnostics for a task that exhausted its retries (a "poison" task).

    Carries enough context to triage without re-running: the Celery ``task_id``
    and ``task_name``, the pipeline ``job_id`` / ``stage`` (when resolvable), the
    failure ``reason`` and the ``traceback``.
    """

    task_id: str
    task_name: str
    reason: str
    job_id: str | None = None
    stage: str | None = None
    traceback: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the record."""
        return dataclasses.asdict(self)


class DeadLetter:
    """Sink for poison tasks after retries are exhausted.

    The default implementation appends a JSON record to a namespaced Redis list
    and logs it structurally. It deliberately swallows sink errors: a dead-letter
    write must never mask the original task failure.

    TODO(T-2.2): persist records to the ``AuditLog`` / dead-letter DB model and
    surface them in the admin monitoring view. This Redis list is the interim
    transport and the stable integration point.
    """

    REDIS_KEY = "iosforge:deadletter"

    @staticmethod
    def record(record: DeadLetterRecord) -> None:
        """Persist a dead-letter record (Redis list) and log it."""
        payload = record.as_dict()
        _log.error("task.dead_letter", **payload)
        try:
            client = _redis_client()
            client.rpush(DeadLetter.REDIS_KEY, json.dumps(payload))
        except Exception:  # noqa: BLE001 - sink must not mask the real failure
            _log.warning("task.dead_letter.sink_unavailable", task_id=record.task_id)


def _redis_client() -> Any:  # noqa: ANN401 - redis client typed as Any (optional dep at type time)
    """Build a Redis client from the configured ``redis_url`` (lazy import)."""
    import redis  # Celery's redis broker dependency; imported lazily.

    return redis.Redis.from_url(get_settings().redis_url)


def _resolve_job_id(args: Any, kwargs: Mapping[str, Any] | None) -> str | None:  # noqa: ANN401
    """Best-effort extraction of ``job_id`` from task call arguments.

    Convention: pipeline tasks take ``job_id`` as the first positional arg or as
    a ``job_id`` keyword. Returns ``None`` if neither is present.
    """
    if kwargs and "job_id" in kwargs:
        return str(kwargs["job_id"])
    if args:
        first = args[0]
        if isinstance(first, (str, int)):
            return str(first)
    return None


class PipelineTask(Task):  # type: ignore[misc]  # celery.Task is untyped (Any)
    """Reusable base task: exponential backoff retries + dead-letter on failure.

    Stage tasks declare ``base=PipelineTask`` and a ``queue=...`` to inherit the
    retry policy and the dead-letter hook. Per-task overrides (e.g. a smaller
    ``max_retries``) still work because these are plain class attributes.
    """

    # autoretry_for + retry_backoff make Celery retry transient failures with an
    # exponentially growing, jittered delay before giving up.
    autoretry_for = (Exception,)
    max_retries = DEFAULT_MAX_RETRIES
    retry_backoff = True
    retry_backoff_max = DEFAULT_RETRY_BACKOFF_MAX
    retry_jitter = True
    # Stage tasks override; default queue keeps standalone tasks routable.
    queue = "discovery"

    def on_failure(
        self,
        exc: BaseException,
        task_id: str,
        args: Any,  # noqa: ANN401 - Celery passes a positional tuple
        kwargs: Mapping[str, Any],
        einfo: Any,  # noqa: ANN401 - celery.app.task.ExceptionInfo
    ) -> None:
        """Route an exhausted task into the dead-letter sink with diagnostics.

        Called by Celery only after retries are exhausted (or when retrying is
        disabled), i.e. exactly once per poison task.
        """
        record = DeadLetterRecord(
            task_id=task_id,
            task_name=self.name or "<unknown>",
            reason=f"{type(exc).__name__}: {exc}",
            job_id=_resolve_job_id(args, kwargs),
            stage=_stage_for_queue(getattr(self, "queue", None)),
            traceback=str(einfo) if einfo is not None else None,
        )
        DeadLetter.record(record)
        super().on_failure(exc, task_id, args, kwargs, einfo)


def _stage_for_queue(queue_name: str | None) -> str | None:
    """Reverse-map a queue name to a representative stage label (diagnostics)."""
    if queue_name is None:
        return None
    for stage, name in STAGE_QUEUES.items():
        if name == queue_name:
            return stage.value
    return queue_name


def _build_task_queues() -> list[Any]:
    """Declare one Celery ``Queue`` per physical stage queue."""
    from kombu import Queue  # type: ignore[import-untyped]

    return [Queue(name) for name in QUEUE_NAMES]


def make_celery_app(
    *,
    broker_url: str | None = None,
    result_backend: str | None = None,
) -> Celery:
    """Build a configured Celery app.

    Broker and result backend default to ``get_settings().redis_url``; both can
    be overridden (tests use an in-memory broker). Stage queues, the default
    :class:`PipelineTask` base and sane serialisation defaults are applied.
    """
    settings = get_settings()
    broker = broker_url or settings.redis_url
    backend = result_backend or settings.redis_url

    app = Celery(
        "iosforge",
        broker=broker,
        backend=backend,
        include=["iosforge.worker.run_job"],
    )
    app.conf.update(
        task_default_queue="discovery",
        task_queues=_build_task_queues(),
        task_routes={
            # Tasks may also set queue= explicitly; routes are the fallback.
        },
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        worker_hijack_root_logger=False,
    )
    return app


#: Canonical module-level app used by ``celery -A iosforge.common.queue``.
celery_app = make_celery_app()


__all__ = [
    "QUEUE_NAMES",
    "STAGE_QUEUES",
    "DeadLetter",
    "DeadLetterRecord",
    "PipelineTask",
    "celery_app",
    "make_celery_app",
    "queue_for_stage",
]
