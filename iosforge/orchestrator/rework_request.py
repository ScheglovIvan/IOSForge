"""The operator's rework round as recorded in the job metadata (admin writes, Mac worker obeys).

Each Apply rework / Add screens stores one request with a fresh id under ``REQUEST_KEY``
and the Celery message carries that id. Only the message of the current request may
run: queueing again replaces the id, so a message still waiting in the broker becomes a
no-op. The worker marks the request ``started`` when it claims the job, or ``declined``
with the reason when it does not run it. A message without an id (queued before ids
existed) is never stale.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

REQUEST_KEY = "rework_request"
REASON_LIMIT = 300


def current(meta: object) -> dict[str, Any] | None:
    """The job's current request (None when absent or recorded without an id)."""
    entry = meta.get(REQUEST_KEY) if isinstance(meta, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
        return entry
    return None


def new_request(
    meta: dict[str, Any] | None, *, summary: str, actor: str, at: datetime
) -> tuple[dict[str, Any], str]:
    """A copy of ``meta`` whose request is a new one, and the id to send with the message."""
    request_id = uuid.uuid4().hex
    updated = dict(meta or {})
    updated[REQUEST_KEY] = {
        "id": request_id,
        "summary": summary,
        "by": actor,
        "at": at.isoformat(),
    }
    return updated, request_id


def is_stale(meta: object, request_id: str | None) -> bool:
    """True when a message queued for ``request_id`` was replaced by a newer request."""
    if request_id is None:
        return False
    entry = current(meta)
    return entry is None or entry["id"] != request_id


def mark(meta: object, request_id: str, **fields: object) -> dict[str, Any] | None:
    """A copy of ``meta`` with ``fields`` set on request ``request_id`` (None: not current)."""
    if not isinstance(meta, dict) or is_stale(meta, request_id):
        return None
    updated = dict(meta)
    updated[REQUEST_KEY] = {**meta[REQUEST_KEY], **fields}
    return updated


def withdraw(meta: object, request_id: str) -> dict[str, Any] | None:
    """A copy of ``meta`` without the request ``request_id`` (None if it is not current)."""
    if not isinstance(meta, dict) or is_stale(meta, request_id):
        return None
    return {key: value for key, value in meta.items() if key != REQUEST_KEY}
