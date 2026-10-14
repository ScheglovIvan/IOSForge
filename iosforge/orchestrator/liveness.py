"""Is a Celery task of a job still running or waiting? (read-only probe for job recovery)

The admin asks this before it resets a stuck job or re-opens its scope. Two sources
are combined:

* every worker's ``inspect().active/reserved/scheduled`` answer: a matching task there
  is *live* (a prefork master answers control messages even while its child runs a
  task, so a worker that does not answer is not executing one);
* a read-only peek into the Redis broker: messages still waiting in a queue
  (``LRANGE``) are *queued*; entries of the ``unacked`` hash without a live twin are
  *orphaned* — the message of a killed task that Redis would redeliver after the
  visibility timeout. Orphans do not block a reset; the run epoch turns their
  redelivery into a no-op.

A task matches when its name is one of :data:`STATE_TASKS` and the job id appears in
its argument representation. The probe never writes to the broker.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from iosforge.common.logging import get_logger
from iosforge.common.queue import QUEUE_NAMES
from iosforge.orchestrator.recovery import RecoveryError

log = get_logger("orchestrator.liveness")

STATE_TASKS: frozenset[str] = frozenset(
    {
        "iosforge.run_job",
        "iosforge.scope_gate",
        "iosforge.build_swiftui",
        "iosforge.rework_swiftui",
        "iosforge.run_xcode_delivery",
    }
)
XCODE_QUEUE = "xcode"
BROKER_QUEUES: tuple[str, ...] = (*QUEUE_NAMES, XCODE_QUEUE)
UNACKED_KEY = "unacked"


@dataclass(frozen=True, slots=True)
class TaskRef:
    """One task of the job seen by the probe: where it is and in which form."""

    task_id: str
    name: str
    where: str
    kind: str


@dataclass(frozen=True, slots=True)
class JobTasks:
    """What the probe found for a job.

    ``live`` — active / reserved / scheduled on a worker; ``queued`` — waiting in a
    broker queue; ``orphaned`` — delivered but unacknowledged with no live twin;
    ``workers_replied`` — hostnames that answered the inspect broadcast.
    """

    live: tuple[TaskRef, ...] = ()
    queued: tuple[TaskRef, ...] = ()
    orphaned: tuple[TaskRef, ...] = ()
    workers_replied: tuple[str, ...] = ()

    @property
    def busy(self) -> bool:
        """True when a task of the job is running or waiting to run."""
        return bool(self.live or self.queued)


TaskProbe = Callable[[str], JobTasks]


class Inspector(Protocol):
    """The part of ``celery.app.control.Inspect`` the probe uses."""

    def active(self) -> Mapping[str, Any] | None: ...

    def reserved(self) -> Mapping[str, Any] | None: ...

    def scheduled(self) -> Mapping[str, Any] | None: ...


class BrokerPeek(Protocol):
    """The read-only part of a Redis client the probe uses."""

    def lrange(self, name: str, start: int, end: int) -> list[Any]: ...

    def hvals(self, name: str) -> list[Any]: ...


def probe_job_tasks(
    job_id: str,
    *,
    timeout: float = 2.0,
    inspector: Inspector | None = None,
    broker: BrokerPeek | None = None,
) -> JobTasks:
    """Find the job's pipeline tasks on the workers and in the broker.

    The sources are read in the direction a message travels — broker queues, then
    the workers' scheduled, reserved and active lists — so a task that moves on while
    the probe runs is seen at a later step instead of slipping between two reads. The
    ``unacked`` hash is read last and only classifies orphans. One inspect object with
    ``timeout`` serves all three worker calls, so the probe takes at most about
    ``3 * timeout``.

    Raises :class:`RecoveryError` (``unverifiable``) when the broker cannot be
    reached, because then nothing can be said about the job's tasks.
    """
    try:
        inspect = inspector if inspector is not None else _default_inspector(timeout)
        peek = broker if broker is not None else _default_broker(timeout)
        queued = [
            ref
            for queue in BROKER_QUEUES
            for ref in _queued_refs(peek.lrange(queue, 0, -1), queue, job_id)
        ]
        live, replied = _live_tasks(inspect, job_id)
        live_ids = {ref.task_id for ref in live}
        orphaned = [
            ref
            for ref in _unacked_refs(peek.hvals(UNACKED_KEY), job_id)
            if ref.task_id not in live_ids
        ]
    except RecoveryError:
        raise
    except Exception as exc:
        log.warning("liveness.unverifiable", job_id=job_id, error=str(exc))
        raise RecoveryError(
            "unverifiable", f"the task queue cannot be checked ({type(exc).__name__}: {exc})"
        ) from exc
    return JobTasks(
        live=tuple(live),
        queued=tuple(queued),
        orphaned=tuple(orphaned),
        workers_replied=tuple(sorted(replied)),
    )


def _default_inspector(timeout: float) -> Inspector:
    from iosforge.common.queue import celery_app

    inspector: Inspector = celery_app.control.inspect(timeout=timeout)
    return inspector


class _RedisPeek:
    """Synchronous read-only view of the broker's Redis (LRANGE / HVALS only)."""

    def __init__(self, timeout: float) -> None:
        import redis

        from iosforge.common.config import get_settings

        self._client = redis.Redis.from_url(
            get_settings().redis_url, socket_timeout=timeout, socket_connect_timeout=timeout
        )

    def lrange(self, name: str, start: int, end: int) -> list[Any]:
        return cast("list[Any]", self._client.lrange(name, start, end))

    def hvals(self, name: str) -> list[Any]:
        return cast("list[Any]", self._client.hvals(name))


def _default_broker(timeout: float) -> BrokerPeek:
    return _RedisPeek(timeout)


def _live_tasks(inspect: Inspector, job_id: str) -> tuple[list[TaskRef], set[str]]:
    refs: list[TaskRef] = []
    replied: set[str] = set()
    answers: list[tuple[str, Mapping[str, Any] | None]] = []
    answers.append(("scheduled", inspect.scheduled()))
    answers.append(("reserved", inspect.reserved()))
    answers.append(("active", inspect.active()))
    for kind, answer in answers:
        for worker, entries in (answer or {}).items():
            replied.add(str(worker))
            for entry in entries or []:
                request = entry.get("request", entry) if isinstance(entry, Mapping) else None
                if isinstance(request, Mapping) and _matches(
                    request.get("name"), _text(request), job_id
                ):
                    refs.append(
                        TaskRef(
                            str(request.get("id") or ""), str(request["name"]), str(worker), kind
                        )
                    )
    return refs, replied


def _text(request: Mapping[str, Any]) -> str:
    return " ".join(
        str(request.get(key, "")) for key in ("args", "kwargs", "argsrepr", "kwargsrepr")
    )


def _matches(name: object, args_text: str, job_id: str) -> bool:
    return isinstance(name, str) and name in STATE_TASKS and job_id in args_text


def _decode(raw: object) -> Any:  # noqa: ANN401 - broker payloads are arbitrary JSON
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _message_ref(message: object, where: str, kind: str, job_id: str) -> TaskRef | None:
    if not isinstance(message, Mapping):
        return None
    headers = message.get("headers")
    if not isinstance(headers, Mapping):
        return None
    if not _matches(headers.get("task"), _text(headers), job_id):
        return None
    return TaskRef(str(headers.get("id") or ""), str(headers["task"]), where, kind)


def _queued_refs(raws: Iterable[object], queue: str, job_id: str) -> list[TaskRef]:
    refs = (_message_ref(_decode(raw), queue, "queued", job_id) for raw in raws)
    return [ref for ref in refs if ref is not None]


def _unacked_refs(raws: Iterable[object], job_id: str) -> list[TaskRef]:
    refs: list[TaskRef] = []
    for raw in raws:
        payload = _decode(raw)
        if not isinstance(payload, list) or not payload:
            continue
        where = str(payload[2]) if len(payload) > 2 else UNACKED_KEY
        ref = _message_ref(payload[0], where, "unacked", job_id)
        if ref is not None:
            refs.append(ref)
    return refs


__all__ = [
    "BROKER_QUEUES",
    "STATE_TASKS",
    "BrokerPeek",
    "Inspector",
    "JobTasks",
    "TaskProbe",
    "TaskRef",
    "probe_job_tasks",
]
