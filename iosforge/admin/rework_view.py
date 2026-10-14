"""Rework tab state: the operator's last queued round and whether it is still under way.

The phase comes from the request record of :mod:`iosforge.orchestrator.rework_request`
and the job state, never from comparing the admin and Mac clocks. A request is
*queued* until the worker marks it started or declined, *running* while the job is in
``CODEGEN``. A request still waiting after ``stall_after_s`` (both times taken by the
admin) is *stalled*: the single-slot Mac queue may be busy with another job. Queueing
again then replaces the request, and the waiting message is skipped by the worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from iosforge.orchestrator import rework_request

INSTRUCTIONS_PREVIEW = 160
MAX_SCREENS_SHOWN = 12

Phase = Literal["queued", "running", "stalled", "declined"]


@dataclass(frozen=True)
class ReworkRound:
    """What the Rework tab says about the operator's round (``phase`` None: idle)."""

    phase: Phase | None
    summary: str = ""
    by: str = ""
    waited_s: int = 0
    reason: str = ""

    @property
    def busy(self) -> bool:
        """True while a new round must not be queued (one is queued or running)."""
        return self.phase in ("queued", "running")


def summarize(instructions: str, screens: list[str] | None) -> str:
    """A one-line description of a round for the flash and the Rework tab."""
    parts: list[str] = []
    if screens:
        shown = ", ".join(screens[:MAX_SCREENS_SHOWN])
        more = len(screens) - MAX_SCREENS_SHOWN
        parts.append(f"add screens {shown}" + (f" (+{more} more)" if more > 0 else ""))
    text = " ".join(instructions.split())
    if text:
        if len(text) > INSTRUCTIONS_PREVIEW:
            text = text[: INSTRUCTIONS_PREVIEW - 1].rstrip() + "…"
        parts.append(f"“{text}”")
    return "; ".join(parts)


def _requested_at(at: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(at or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def status(
    meta: object,
    *,
    codegen_running: bool,
    now: datetime,
    stall_after_s: int,
) -> ReworkRound:
    """The round state from the job's data alone (no broker probe)."""
    entry = rework_request.current(meta)
    if codegen_running:
        if entry is not None and entry.get("started"):
            return ReworkRound(
                "running", str(entry.get("summary") or ""), str(entry.get("by") or "")
            )
        return ReworkRound("running")
    if entry is None or entry.get("started"):
        return ReworkRound(None)
    summary = str(entry.get("summary") or "")
    by = str(entry.get("by") or "")
    if entry.get("declined"):
        return ReworkRound("declined", summary, by, reason=str(entry["declined"]))
    requested = _requested_at(entry.get("at"))
    if requested is None:
        return ReworkRound(None)
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    waited = max(0, int((current - requested).total_seconds()))
    phase: Phase = "stalled" if waited >= stall_after_s else "queued"
    return ReworkRound(phase, summary, by, waited)
