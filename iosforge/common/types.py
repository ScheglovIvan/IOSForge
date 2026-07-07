"""Base domain types (Pydantic / enums).

Only types and enums live here — no DB logic (that is T-2.2). The ``JobState``
state machine matrix and persistence are implemented later in the orchestrator
and db layers (SPEC §5.1).
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    """Lifecycle state of a Job (SPEC §5.1 / STACK state machine)."""

    QUEUED = "queued"
    DISCOVERY = "discovery"
    ACQUISITION = "acquisition"
    WALKTHROUGH = "walkthrough"
    ANALYSIS = "analysis"
    CODEGEN = "codegen"
    GITHUB_UPLOAD = "github_upload"
    DELIVERY = "delivery"
    DONE = "done"
    NEEDS_INPUT = "needs_input"
    RETRYABLE = "retryable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(StrEnum):
    """A pipeline stage that produces an artifact (SPEC §5.2–5.6).

    Maps to Celery queue names; a subset of :class:`JobState` (the terminal and
    branching states are not stages).
    """

    DISCOVERY = "discovery"
    ACQUISITION = "acquisition"
    WALKTHROUGH = "walkthrough"
    ANALYSIS = "analysis"
    CODEGEN = "codegen"
    GITHUB_UPLOAD = "github_upload"
    DELIVERY = "delivery"
