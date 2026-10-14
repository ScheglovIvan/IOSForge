"""The Celery/Redis liveness probe used before a job reset (fake inspect + fake Redis)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from iosforge.orchestrator.liveness import BROKER_QUEUES, JobTasks, probe_job_tasks
from iosforge.orchestrator.recovery import RecoveryError

JOB = "4b6f0c3e-1111-2222-3333-444455556666"
OTHER = "9d9d9d9d-1111-2222-3333-444455556666"


class _Inspect:
    def __init__(
        self,
        active: dict[str, Any] | None = None,
        reserved: dict[str, Any] | None = None,
        scheduled: dict[str, Any] | None = None,
    ) -> None:
        self._active, self._reserved, self._scheduled = active, reserved, scheduled

    def active(self) -> dict[str, Any] | None:
        return self._active

    def reserved(self) -> dict[str, Any] | None:
        return self._reserved

    def scheduled(self) -> dict[str, Any] | None:
        return self._scheduled


class _Redis:
    def __init__(
        self, queues: dict[str, list[bytes]] | None = None, unacked: list[bytes] | None = None
    ) -> None:
        self.queues, self.unacked = queues or {}, unacked or []
        self.calls: list[str] = []

    def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        self.calls.append(f"lrange {name}")
        return self.queues.get(name, [])

    def hvals(self, name: str) -> list[bytes]:
        self.calls.append(f"hvals {name}")
        return self.unacked if name == "unacked" else []


def _message(task: str, job_id: str, task_id: str) -> dict[str, Any]:
    return {
        "body": "e30=",
        "headers": {
            "task": task,
            "id": task_id,
            "argsrepr": f"['{job_id}']",
            "kwargsrepr": "{'epoch': 1}",
        },
        "properties": {"delivery_tag": task_id},
    }


def _request(task: str, job_id: str, task_id: str) -> dict[str, Any]:
    return {"id": task_id, "name": task, "args": [job_id], "kwargs": {}, "hostname": "w"}


def test_idle_job_is_not_busy() -> None:
    broker = _Redis()
    tasks = probe_job_tasks(
        JOB,
        inspector=_Inspect(active={"codegen@mac": []}, reserved={"codegen@mac": []}),
        broker=broker,
    )
    assert tasks == JobTasks(workers_replied=("codegen@mac",))
    assert not tasks.busy
    assert "lrange xcode" in broker.calls and "lrange codegen" in broker.calls
    assert "hvals unacked" in broker.calls


def test_active_reserved_and_scheduled_tasks_are_live() -> None:
    inspect = _Inspect(
        active={"xcode@mac": [_request("iosforge.build_swiftui", JOB, "a")]},
        reserved={"codegen@mac": [_request("iosforge.run_job", OTHER, "b")]},
        scheduled={
            "codegen@mac": [{"eta": "x", "request": _request("iosforge.run_job", JOB, "c")}]
        },
    )
    tasks = probe_job_tasks(JOB, inspector=inspect, broker=_Redis())
    assert [(t.task_id, t.kind, t.where) for t in tasks.live] == [
        ("c", "scheduled", "codegen@mac"),
        ("a", "active", "xcode@mac"),
    ]
    assert tasks.busy
    assert tasks.workers_replied == ("codegen@mac", "xcode@mac")


def test_tasks_outside_the_state_set_are_ignored() -> None:
    inspect = _Inspect(active={"codegen@mac": [_request("iosforge.generate_app_icon", JOB, "x")]})
    raw = json.dumps(_message("iosforge.generate_store_assets", JOB, "y")).encode()
    tasks = probe_job_tasks(JOB, inspector=inspect, broker=_Redis(queues={"codegen": [raw]}))
    assert not tasks.busy and tasks.orphaned == ()


def test_waiting_messages_are_queued() -> None:
    raw = json.dumps(_message("iosforge.build_swiftui", JOB, "q")).encode()
    other = json.dumps(_message("iosforge.build_swiftui", OTHER, "r")).encode()
    broker = _Redis(queues={"xcode": [other, raw, b"not json"]})
    tasks = probe_job_tasks(JOB, inspector=_Inspect(), broker=broker)
    assert [(t.task_id, t.where, t.kind) for t in tasks.queued] == [("q", "xcode", "queued")]
    assert tasks.busy


def test_unacked_without_a_live_twin_is_orphaned() -> None:
    dead = json.dumps([_message("iosforge.build_swiftui", JOB, "dead"), "", "xcode"]).encode()
    running = json.dumps([_message("iosforge.run_job", JOB, "run"), "", "codegen"]).encode()
    inspect = _Inspect(active={"codegen@mac": [_request("iosforge.run_job", JOB, "run")]})
    tasks = probe_job_tasks(JOB, inspector=inspect, broker=_Redis(unacked=[dead, running]))
    assert [(t.task_id, t.where) for t in tasks.orphaned] == [("dead", "xcode")]
    assert [t.task_id for t in tasks.live] == ["run"]


def test_orphans_alone_do_not_block() -> None:
    dead = json.dumps([_message("iosforge.scope_gate", JOB, "dead"), "", "codegen"]).encode()
    tasks = probe_job_tasks(JOB, inspector=_Inspect(), broker=_Redis(unacked=[dead]))
    assert not tasks.busy and len(tasks.orphaned) == 1


def test_unreachable_redis_is_unverifiable() -> None:
    class _Down(_Redis):
        def lrange(self, name: str, start: int, end: int) -> list[bytes]:
            raise ConnectionError("redis down")

    with pytest.raises(RecoveryError) as err:
        probe_job_tasks(JOB, inspector=_Inspect(), broker=_Down())
    assert err.value.code == "unverifiable"
    assert "redis down" in err.value.message


def test_failing_inspect_is_unverifiable() -> None:
    class _Broken(_Inspect):
        def active(self) -> dict[str, Any] | None:
            raise OSError("broker connection refused")

    with pytest.raises(RecoveryError) as err:
        probe_job_tasks(JOB, inspector=_Broken(), broker=_Redis())
    assert err.value.code == "unverifiable"


def test_reads_follow_the_message_path() -> None:
    journal: list[str] = []

    class _JournalInspect(_Inspect):
        def active(self) -> dict[str, Any] | None:
            journal.append("active")
            return None

        def reserved(self) -> dict[str, Any] | None:
            journal.append("reserved")
            return None

        def scheduled(self) -> dict[str, Any] | None:
            journal.append("scheduled")
            return None

    class _JournalRedis(_Redis):
        def lrange(self, name: str, start: int, end: int) -> list[bytes]:
            journal.append("lrange")
            return []

        def hvals(self, name: str) -> list[bytes]:
            journal.append("hvals")
            return []

    probe_job_tasks(JOB, inspector=_JournalInspect(), broker=_JournalRedis())
    queues = len(BROKER_QUEUES)
    assert journal == ["lrange"] * queues + ["scheduled", "reserved", "active", "hvals"]


def test_a_task_taken_from_the_queue_before_inspect_is_live_not_orphaned() -> None:
    unacked = json.dumps([_message("iosforge.build_swiftui", JOB, "m"), "", "xcode"]).encode()

    class _Worker(_Inspect):
        def active(self) -> dict[str, Any] | None:
            return {"xcode@mac": [_request("iosforge.build_swiftui", JOB, "m")]}

    tasks = probe_job_tasks(JOB, inspector=_Worker(), broker=_Redis(unacked=[unacked]))
    assert [t.task_id for t in tasks.live] == ["m"]
    assert tasks.orphaned == () and tasks.busy
