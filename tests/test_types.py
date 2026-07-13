"""Tests for base domain enums (T-1.3, SPEC §5.1)."""

from __future__ import annotations

import json

from pydantic import BaseModel

from iosforge.common.types import JobState, Stage


def test_job_state_members() -> None:
    expected = {
        "QUEUED": "queued",
        "DISCOVERY": "discovery",
        "ACQUISITION": "acquisition",
        "WALKTHROUGH": "walkthrough",
        "ANALYSIS": "analysis",
        "CODEGEN": "codegen",
        "VERIFY": "verify",
        "GITHUB_UPLOAD": "github_upload",
        "CODEMAGIC_INTEGRATION": "codemagic_integration",
        "DELIVERY": "delivery",
        "DONE": "done",
        "NEEDS_INPUT": "needs_input",
        "RETRYABLE": "retryable",
        "FAILED": "failed",
        "CANCELLED": "cancelled",
    }
    assert {m.name: m.value for m in JobState} == expected


def test_stage_members() -> None:
    expected = {
        "DISCOVERY": "discovery",
        "ACQUISITION": "acquisition",
        "WALKTHROUGH": "walkthrough",
        "ANALYSIS": "analysis",
        "CODEGEN": "codegen",
        "VERIFY": "verify",
        "GITHUB_UPLOAD": "github_upload",
        "CODEMAGIC_INTEGRATION": "codemagic_integration",
        "DELIVERY": "delivery",
    }
    assert {m.name: m.value for m in Stage} == expected


def test_stage_values_subset_of_job_state() -> None:
    job_values = {s.value for s in JobState}
    assert {s.value for s in Stage} <= job_values


def test_str_enum_behaviour() -> None:
    assert JobState.QUEUED == "queued"
    assert Stage.DISCOVERY == "discovery"


def test_pydantic_serialization() -> None:
    class _Model(BaseModel):
        state: JobState
        stage: Stage

    model = _Model(state=JobState.WALKTHROUGH, stage=Stage.WALKTHROUGH)
    dumped = model.model_dump_json()
    assert json.loads(dumped) == {"state": "walkthrough", "stage": "walkthrough"}

    parsed = _Model.model_validate_json('{"state": "done", "stage": "delivery"}')
    assert parsed.state is JobState.DONE
    assert parsed.stage is Stage.DELIVERY
