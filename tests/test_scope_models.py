"""Unit tests for the SCOPE-stage Pydantic models (Phase 2 scope-gate).

Covers the derived tallies (``recount`` / ``ScopeCounts.from_screens``), JSON
roundtripping and the ``Literal`` constraints that keep a malformed decision from
ever reaching storage (see DECISIONS 2026-10-09 "Phase 2").
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from iosforge.mvp.scope_models import (
    FeasibilityFinding,
    FeasibilityReport,
    ScopeCounts,
    ScopeDecision,
    ScreenScope,
)


def _screens() -> list[ScreenScope]:
    return [
        ScreenScope(screen_id="a", name="A", include=True, reason="core"),
        ScreenScope(screen_id="b", name="B", include=True, reason="core"),
        ScreenScope(screen_id="c", name="C", include=False, reason="onboarding"),
    ]


def _decision(screens: list[ScreenScope] | None = None) -> ScopeDecision:
    screens = _screens() if screens is None else screens
    return ScopeDecision(
        status="proposed",
        scope_mode="core",
        screens=screens,
        core_flows=["browse"],
        feasibility=FeasibilityReport(
            overall_verdict="partial",
            summary="mostly native",
            findings=[
                FeasibilityFinding(
                    capability="widgets", verdict="blocked", note="no api", screens=["c"]
                )
            ],
        ),
        counts=ScopeCounts.from_screens(screens),
        proposed_at=datetime.now(UTC),
    )


def test_from_screens_tallies() -> None:
    counts = ScopeCounts.from_screens(_screens())
    assert (counts.total, counts.included, counts.excluded) == (3, 2, 1)


def test_from_screens_empty() -> None:
    counts = ScopeCounts.from_screens([])
    assert (counts.total, counts.included, counts.excluded) == (0, 0, 0)


def test_recount_refreshes_after_mutation() -> None:
    decision = _decision()
    assert decision.counts.included == 2
    decision.screens[2].include = True
    decision.recount()
    assert (decision.counts.total, decision.counts.included, decision.counts.excluded) == (3, 3, 0)


def test_json_roundtrip_is_lossless() -> None:
    decision = _decision()
    restored = ScopeDecision.model_validate_json(decision.model_dump_json())
    assert restored == decision
    assert restored.feasibility.findings[0].verdict == "blocked"
    assert restored.counts.included == 2


def test_approved_stamps_roundtrip() -> None:
    decision = _decision()
    decision.status = "approved"
    decision.approved_by = "op"
    decision.approved_at = datetime.now(UTC)
    restored = ScopeDecision.model_validate_json(decision.model_dump_json())
    assert restored.status == "approved"
    assert restored.approved_by == "op"
    assert restored.approved_at == decision.approved_at


def test_invalid_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        FeasibilityReport(overall_verdict="maybe", summary="")  # type: ignore[arg-type]


def test_invalid_finding_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        FeasibilityFinding(capability="x", verdict="unknown", note="")  # type: ignore[arg-type]


def test_invalid_status_rejected() -> None:
    screens = _screens()
    with pytest.raises(ValidationError):
        ScopeDecision(
            status="pending",  # type: ignore[arg-type]
            scope_mode="core",
            screens=screens,
            feasibility=FeasibilityReport(overall_verdict="native", summary=""),
            counts=ScopeCounts.from_screens(screens),
            proposed_at=datetime.now(UTC),
        )


def test_invalid_scope_mode_rejected() -> None:
    screens = _screens()
    with pytest.raises(ValidationError):
        ScopeDecision(
            status="proposed",
            scope_mode="partial",  # type: ignore[arg-type]
            screens=screens,
            feasibility=FeasibilityReport(overall_verdict="native", summary=""),
            counts=ScopeCounts.from_screens(screens),
            proposed_at=datetime.now(UTC),
        )
