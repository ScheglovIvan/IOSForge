"""What the Scope tab shows for an approved scope: the decision next to the app as built.

The approved ``scope.json`` is the operator's original decision and is never rewritten.
Later scope extensions (``Add screens``) are separate operator decisions: each request
is recorded in ``source_app_metadata["scope_extensions"]`` and a finished rework round
records the screens the stored app really contains in the built scope
(``sources/swiftui_scope.json``). This module overlays both on the decision so the tab
lists what was approved, what was added later and what is in the app now.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from iosforge.common.logging import get_logger
from iosforge.mvp.scope_models import ScopeDecision
from iosforge.storage.client import ArtifactStorage

log = get_logger("admin.scope_view")

EXTENSIONS_KEY = "scope_extensions"
MAX_EXTENSIONS = 20
MAX_SCREENS_PER_REQUEST = 50


@dataclass(frozen=True)
class ScreenStatus:
    """How one screen of the decision stands in the app."""

    label: str
    tone: str


@dataclass(frozen=True)
class ScopeView:
    """The decision overlaid with the operator's extensions and the built scope."""

    built: list[str] | None
    statuses: dict[str, ScreenStatus] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    extensions: list[dict[str, Any]] = field(default_factory=list)


def approved_ids(scope: ScopeDecision) -> list[str]:
    """Screens the approval itself selects (same rule as ``feasibility.resolve_scope_ids``)."""
    every = [s.screen_id for s in scope.screens]
    if scope.scope_mode != "core":
        return every
    included = [s.screen_id for s in scope.screens if s.include]
    return included or every


def extensions(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The recorded ``Add screens`` requests, oldest first (malformed entries dropped)."""
    raw = (meta or {}).get(EXTENSIONS_KEY)
    if not isinstance(raw, list):
        return []
    return [
        {
            "screens": [str(s) for s in entry.get("screens") or []],
            "by": str(entry.get("by") or ""),
            "at": str(entry.get("at") or ""),
        }
        for entry in raw
        if isinstance(entry, dict)
    ]


def record_extension(
    meta: dict[str, Any] | None, screens: list[str], actor: str, at: datetime
) -> dict[str, Any]:
    """A copy of ``meta`` with one more ``Add screens`` request (bounded history)."""
    updated = dict(meta or {})
    entry = {
        "screens": list(dict.fromkeys(screens))[:MAX_SCREENS_PER_REQUEST],
        "by": actor,
        "at": at.isoformat(),
    }
    updated[EXTENSIONS_KEY] = [*extensions(meta), entry][-MAX_EXTENSIONS:]
    return updated


def drop_extension(meta: dict[str, Any], at: str) -> dict[str, Any]:
    """A copy of ``meta`` without the newest ``Add screens`` request recorded ``at``."""
    raw = meta.get(EXTENSIONS_KEY)
    if not isinstance(raw, list):
        return dict(meta)
    entries = list(raw)
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if isinstance(entry, dict) and entry.get("at") == at:
            del entries[index]
            break
    return {**meta, EXTENSIONS_KEY: entries}


def read_built(storage: ArtifactStorage, job_id: str) -> list[str] | None:
    """Screen ids of the stored app (None when no build recorded them or unreadable)."""
    from iosforge.worker.swiftui_tasks import built_scope_key

    key = built_scope_key(job_id)
    try:
        if not storage.exists(key):
            return None
        data = json.loads(storage.get(key))
        return [str(i) for i in data.get("screens", [])]
    except Exception as exc:
        log.warning("scope_view.built_scope_unreadable", job_id=job_id, error=str(exc))
        return None


def _status(
    screen_id: str, approved: set[str], built: set[str] | None, requested: set[str], running: bool
) -> ScreenStatus | None:
    in_app = built is not None and screen_id in built
    if in_app and screen_id in approved:
        return ScreenStatus("built", "success")
    if in_app:
        return ScreenStatus("added in rework", "info")
    if screen_id in requested:
        label = "requested · round running" if running else "requested · not built"
        return ScreenStatus(label, "warning")
    if built is not None and screen_id in approved:
        return ScreenStatus("not in the app", "danger")
    return None


def build(
    scope: ScopeDecision,
    built: list[str] | None,
    requests: list[dict[str, Any]],
    *,
    running: bool,
) -> ScopeView:
    """Overlay ``built`` and the extension ``requests`` on the approved ``scope``."""
    approved = set(approved_ids(scope))
    built_set = set(built) if built is not None else None
    requested = {sid for entry in requests for sid in entry["screens"]}
    listed = [s.screen_id for s in scope.screens]
    statuses: dict[str, ScreenStatus] = {}
    for sid in listed:
        status = _status(sid, approved, built_set, requested, running)
        if status is not None:
            statuses[sid] = status
    in_app = built or []
    added = [sid for sid in in_app if sid not in approved]
    pending = [sid for sid in dict.fromkeys(requested) if sid not in (built_set or set())]
    known = set(listed)
    unlisted = [sid for sid in dict.fromkeys([*in_app, *sorted(requested)]) if sid not in known]
    return ScopeView(
        built=built,
        statuses=statuses,
        added=added,
        pending=sorted(pending),
        unlisted=unlisted,
        extensions=requests,
    )


def load(
    storage: ArtifactStorage,
    job_id: str,
    meta: dict[str, Any] | None,
    scope: object,
    *,
    running: bool,
) -> ScopeView | None:
    """The Scope tab overlay of an approved ``scope`` (None for anything else)."""
    if not isinstance(scope, ScopeDecision) or scope.status != "approved":
        return None
    return build(scope, read_built(storage, job_id), extensions(meta), running=running)
