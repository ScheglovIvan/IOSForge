"""Structural verification units of the Vision Judge (pure functions).

Covers the byte-size blank detector (:func:`compliance.blank_screens`), the
corrective task expansion (:func:`compliance.diffs_to_tasks`) and the composite
structural gate in ``compliance._refine``. Every unit reads only files under a
:class:`RunPaths` built from ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import compliance
from iosforge.mvp.compliance import ComplianceWeights
from iosforge.mvp.paths import RunPaths

_WEIGHTS = ComplianceWeights(structure=0.5, coverage=0.3, flows=0.2, divergence=0.0)


def _write_screens(paths: RunPaths, screens: list[dict[str, Any]]) -> None:
    paths.screens_json.write_text(json.dumps({"screens": screens}))


# --------------------------------------------------------------------------- #
# nav_audit
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# blank_screens
# --------------------------------------------------------------------------- #


def test_blank_screens_flags_small_png_only(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    (paths.generated_screens_dir / "small.png").write_bytes(b"x" * 10)
    (paths.generated_screens_dir / "big.png").write_bytes(b"x" * 500)

    flagged = compliance.blank_screens(paths, max_bytes=100)

    assert flagged == [{"id": "small", "bytes": 10, "reason": "near_uniform"}]


# --------------------------------------------------------------------------- #
# diffs_to_tasks (structural expansion + backward compat)
# --------------------------------------------------------------------------- #


def _structural_report() -> dict[str, Any]:
    return {
        "threshold": 0.95,
        "screens": [{"id": "0001", "score": 0.40, "diffs": ["button missing"]}],
        "structural": {
            "missing_screens": [{"id": "settings", "expected_route": "/settings"}],
            "blank_screens": [{"id": "blankid", "bytes": 5, "reason": "near_uniform"}],
            "dead_links": [{"target": "/nope", "file": "home.dart", "kind": "go"}],
            "missing_edges": [{"from": "home", "to": "detail", "via_element": "card"}],
        },
    }


def test_diffs_to_tasks_emits_structural_task_types(tmp_path: Path) -> None:
    out = compliance.diffs_to_tasks(_structural_report(), iteration=2)
    by_id = {t["id"]: t for t in out["tasks"]}

    assert by_id["fix-2-0001"]["type"] == "fix"
    assert by_id["add-2-settings"]["type"] == "add_screen"
    assert by_id["blank-2-blankid"]["type"] == "fix_blank"
    assert by_id["deadlink-2-0"]["type"] == "fix_dead_link"
    assert by_id["edge-2-0"]["type"] == "add_edge"


def test_diffs_to_tasks_without_structural_is_unchanged() -> None:
    report = {
        "threshold": 0.95,
        "screens": [
            {"id": "0000", "score": 0.99, "diffs": []},
            {"id": "0001", "score": 0.40, "diffs": ["button missing"]},
        ],
    }
    out = compliance.diffs_to_tasks(report, iteration=1)

    assert [t["id"] for t in out["tasks"]] == ["fix-1-0001"]
    assert all(t["type"] == "fix" for t in out["tasks"])


# --------------------------------------------------------------------------- #
# _refine composite structural gate (all_closed / max_iterations)
# --------------------------------------------------------------------------- #


def _run_refine_with_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reports: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    max_iterations: int,
) -> dict[str, Any]:
    paths = RunPaths.create(tmp_path)
    report_seq = iter(reports)
    audit_seq = iter(audits)
    monkeypatch.setattr(compliance, "evaluate", lambda *a, **k: next(report_seq))
    monkeypatch.setattr(compliance, "diffs_to_tasks", lambda r, i: {"tasks": []})
    monkeypatch.setattr(compliance, "apply_corrective", lambda *a, **k: paths.xcode_app)

    return compliance._refine(
        paths,
        lambda: None,
        threshold=0.80,
        soft_floor=0.80,
        max_iterations=max_iterations,
        weights=_WEIGHTS,
        timeout=1,
        per_screen=True,
        audit=lambda: next(audit_seq),
    )


def _empty_structural(ok: bool, *, missing: bool = False) -> dict[str, Any]:
    return {
        "ok": ok,
        "missing_screens": [{"id": "x", "expected_route": "/x"}] if missing else [],
        "blank_screens": [],
        "dead_links": [],
        "missing_edges": [],
    }


def test_refine_all_closed_when_visual_and_structural_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_refine_with_audit(
        tmp_path,
        monkeypatch,
        reports=[
            {"compliance_score": 0.90, "screens": [{"id": "a", "score": 0.90}]},
            {"compliance_score": 0.92, "screens": [{"id": "a", "score": 0.92}]},
        ],
        audits=[
            _empty_structural(False, missing=True),
            _empty_structural(True),
        ],
        max_iterations=3,
    )

    assert report["stop_reason"] == "all_closed"
    assert report["structural"]["ok"] is True


def test_refine_max_iterations_when_gaps_stay_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_refine_with_audit(
        tmp_path,
        monkeypatch,
        reports=[{"compliance_score": 0.90, "screens": [{"id": "a", "score": 0.90}]}],
        audits=[_empty_structural(False, missing=True)],
        max_iterations=1,
    )

    assert report["stop_reason"] == "max_iterations"
    assert report["structural"]["ok"] is False
