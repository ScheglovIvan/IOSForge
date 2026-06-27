"""Tests for Stage E (compliance/refinement loop).

Pure units (match_screens, aggregate, diffs_to_tasks) plus evaluate with a mocked
vision-judge and refine_until_compliant with build/render/evaluate monkeypatched
to canned sequences. No real flutter/emulator/adb/claude here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import compliance
from iosforge.mvp.compliance import ComplianceWeights
from iosforge.mvp.paths import RunPaths

_WEIGHTS = ComplianceWeights(visual=0.5, coverage=0.3, flows=0.2)


def test_match_screens_pairs_by_id_with_missing_and_surplus() -> None:
    original = [{"id": "0000"}, {"id": "0001"}, {"id": "0002"}]
    generated = [
        {"id": "0000", "screenshot": "generated_screens/0000.png"},
        {"id": "0002", "screenshot": "generated_screens/0002.png"},
        {"id": "9999", "screenshot": "generated_screens/9999.png"},
    ]
    pairs = compliance.match_screens(original, generated)
    assert pairs == [
        ("0000", "generated_screens/0000.png"),
        ("0001", None),
        ("0002", "generated_screens/0002.png"),
    ]


def test_aggregate_weights_and_pass_status() -> None:
    judge = {
        "screens": [
            {"id": "0000", "score": 1.0, "diffs": []},
            {"id": "0001", "score": 0.9, "diffs": ["minor color"]},
        ],
        "flows_score": 1.0,
    }
    matches = [("0000", "g0"), ("0001", "g1")]
    report = compliance.aggregate(
        judge, matches, weights=_WEIGHTS, threshold=0.95, soft_floor=0.80, iteration=1
    )
    assert report["visual"] == pytest.approx(0.95)
    assert report["coverage"] == pytest.approx(1.0)
    assert report["flows"] == pytest.approx(1.0)
    assert report["compliance_score"] == pytest.approx(0.5 * 0.95 + 0.3 * 1.0 + 0.2 * 1.0)
    assert report["status"] == "pass"


def test_aggregate_missing_screen_penalises_visual_and_coverage() -> None:
    judge = {"screens": [{"id": "0000", "score": 1.0, "diffs": []}], "flows_score": 1.0}
    matches = [("0000", "g0"), ("0001", None)]
    report = compliance.aggregate(
        judge, matches, weights=_WEIGHTS, threshold=0.95, soft_floor=0.80, iteration=2
    )
    assert report["visual"] == pytest.approx(0.5)
    assert report["coverage"] == pytest.approx(0.5)
    assert report["screens"][1] == {
        "id": "0001",
        "generated": None,
        "score": 0.0,
        "diffs": ["screen not rendered in generated app"],
    }


def test_aggregate_status_soft_pass_and_below_floor() -> None:
    matches = [("0000", "g0")]
    soft = compliance.aggregate(
        {"screens": [{"id": "0000", "score": 0.85, "diffs": []}], "flows_score": 0.85},
        matches,
        weights=_WEIGHTS,
        threshold=0.95,
        soft_floor=0.80,
        iteration=1,
    )
    assert soft["status"] == "soft_pass"
    low = compliance.aggregate(
        {"screens": [{"id": "0000", "score": 0.2, "diffs": []}], "flows_score": 0.2},
        matches,
        weights=_WEIGHTS,
        threshold=0.95,
        soft_floor=0.80,
        iteration=1,
    )
    assert low["status"] == "below_floor"


def test_diffs_to_tasks_targets_below_threshold_and_missing() -> None:
    report = {
        "threshold": 0.95,
        "screens": [
            {"id": "0000", "score": 0.99, "diffs": []},
            {"id": "0001", "score": 0.4, "diffs": ["button missing", "wrong title"]},
            {"id": "0002", "score": 0.0, "diffs": ["screen not rendered in generated app"]},
        ],
    }
    out = compliance.diffs_to_tasks(report, iteration=1)
    tasks = out["tasks"]
    assert [t["id"] for t in tasks] == ["fix-1-0001", "fix-1-0002"]
    assert all(t["type"] == "fix" for t in tasks)
    assert tasks[0]["screens"] == ["0001"]
    assert tasks[0]["title"] == "Fix screen 0001: button missing"
    assert tasks[1]["title"].startswith("Fix screen 0002:")


def _make_paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path)
    (rp.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.screens_json.write_text(json.dumps({"screens": [{"id": "0000"}]}))
    (rp.generated_screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.generated_screens_json.write_text(
        json.dumps({"screens": [{"id": "0000", "screenshot": "generated_screens/0000.png"}]})
    )
    return rp


def test_evaluate_uses_judge_json_and_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    judge = {"screens": [{"id": "0000", "score": 0.9, "diffs": ["x"]}], "flows_score": 0.8}

    def _fake_claude(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        (cwd / "judge.json").write_text(json.dumps(judge))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(compliance.subprocess, "run", _fake_claude)

    report = compliance.evaluate(
        rp, 1, weights=_WEIGHTS, threshold=0.95, soft_floor=0.80, history=[]
    )
    assert report["visual"] == pytest.approx(0.9)
    assert report["compliance_score"] == pytest.approx(0.5 * 0.9 + 0.3 * 1.0 + 0.2 * 0.8)
    assert report["status"] == "soft_pass"
    assert rp.selftest_report_json.exists()
    assert json.loads(rp.selftest_report_json.read_text())["iteration"] == 1


def _stub_loop(monkeypatch: pytest.MonkeyPatch, scores: list[float], applied: list[int]) -> None:
    seq = iter(scores)
    monkeypatch.setattr(compliance, "build_apk", lambda paths, **kw: paths.apk)
    monkeypatch.setattr(compliance, "render_generated", lambda paths, **kw: {"screens": []})

    def _fake_evaluate(paths: RunPaths, iteration: int, **kwargs: Any) -> dict[str, Any]:
        score = next(seq)
        status = "pass" if score >= kwargs["threshold"] else "soft_pass"
        return {
            "iteration": iteration,
            "compliance_score": score,
            "status": status,
            "threshold": kwargs["threshold"],
            "screens": [{"id": "0000", "score": score, "diffs": ["fix me"]}],
        }

    def _fake_apply(paths: RunPaths, corrective: dict[str, Any], **kw: Any) -> Path:
        applied.append(len(corrective["tasks"]))
        return paths.flutter_app

    monkeypatch.setattr(compliance, "evaluate", _fake_evaluate)
    monkeypatch.setattr(compliance, "apply_corrective", _fake_apply)


def _run_loop(tmp_path: Path, max_iterations: int) -> dict[str, Any]:
    rp = RunPaths.create(tmp_path)
    rp.screens_json.write_text(json.dumps({"screens": [{"id": "0000"}]}))
    return compliance.refine_until_compliant(
        rp,
        threshold=0.95,
        soft_floor=0.80,
        max_iterations=max_iterations,
        weights=_WEIGHTS,
        avd="mvp",
    )


def test_refine_stops_when_threshold_met(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[int] = []
    _stub_loop(monkeypatch, scores=[0.96], applied=applied)
    report = _run_loop(tmp_path, max_iterations=3)
    assert report["stop_reason"] == "threshold_met"
    assert report["status"] == "pass"
    assert applied == []


def test_refine_feeds_corrective_then_stops_on_max_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied: list[int] = []
    _stub_loop(monkeypatch, scores=[0.50, 0.70, 0.85], applied=applied)
    report = _run_loop(tmp_path, max_iterations=3)
    assert report["stop_reason"] == "max_iterations"
    assert applied == [1, 1]


def test_refine_stops_on_no_improvement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[int] = []
    _stub_loop(monkeypatch, scores=[0.50, 0.505], applied=applied)
    report = _run_loop(tmp_path, max_iterations=5)
    assert report["stop_reason"] == "no_improvement"
    assert applied == [1]


def test_refine_below_floor_returns_report_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compliance, "build_apk", lambda paths, **kw: paths.apk)
    monkeypatch.setattr(compliance, "render_generated", lambda paths, **kw: {"screens": []})
    monkeypatch.setattr(
        compliance,
        "evaluate",
        lambda paths, iteration, **kw: {
            "iteration": iteration,
            "compliance_score": 0.3,
            "status": "below_floor",
            "threshold": kw["threshold"],
            "screens": [],
        },
    )
    monkeypatch.setattr(compliance, "apply_corrective", lambda paths, c, **kw: paths.flutter_app)

    report = _run_loop(tmp_path, max_iterations=1)
    assert report["status"] == "below_floor"
    assert report["stop_reason"] == "max_iterations"
