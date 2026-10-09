"""Pydantic models for the SCOPE stage artifact (feasibility + scope decision).

The SCOPE stage sits between ANALYSIS and CODEGEN: it proposes which screens and
flows form the MVP core versus the full clone, bundled with an iOS feasibility
report. The whole decision is one JSON document (``scope.json``) stored in the
versioned MinIO bucket; ``status`` distinguishes the stage-proposed version from
the operator-approved one (see DECISIONS 2026-10-09 "Phase 2: scope-гейт").
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FeasibilityFinding(BaseModel):
    """One iOS-capability finding feeding the advisory feasibility verdict."""

    capability: str
    verdict: Literal["native", "partial", "blocked"]
    note: str
    screens: list[str] = Field(default_factory=list)


class FeasibilityReport(BaseModel):
    """Advisory report on how well the app maps onto native iOS capabilities."""

    overall_verdict: Literal["native", "partial", "blocked"]
    summary: str
    findings: list[FeasibilityFinding] = Field(default_factory=list)


class ScreenScope(BaseModel):
    """Per-screen inclusion decision within a scope proposal."""

    screen_id: str
    name: str
    include: bool
    reason: str
    group: str | None = None


class ScopeCounts(BaseModel):
    """Derived tallies of a scope decision (recomputed from ``screens``)."""

    total: int
    included: int
    excluded: int

    @classmethod
    def from_screens(cls, screens: list[ScreenScope]) -> ScopeCounts:
        """Recompute totals from a list of per-screen decisions."""
        total = len(screens)
        included = sum(1 for screen in screens if screen.include)
        return cls(total=total, included=included, excluded=total - included)


class ScopeDecision(BaseModel):
    """The full SCOPE artifact: screen selection + feasibility + audit stamps."""

    status: Literal["proposed", "approved"]
    scope_mode: Literal["core", "full"]
    screens: list[ScreenScope] = Field(default_factory=list)
    core_flows: list[str] = Field(default_factory=list)
    feasibility: FeasibilityReport
    counts: ScopeCounts
    notes: str = ""
    proposed_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None

    def recount(self) -> None:
        """Refresh ``counts`` from the current ``screens`` list (in place)."""
        self.counts = ScopeCounts.from_screens(self.screens)
