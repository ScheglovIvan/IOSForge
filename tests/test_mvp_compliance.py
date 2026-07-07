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


def test_snap_stage_dir_only_for_snap_binary() -> None:
    assert compliance._snap_stage_dir("/usr/bin/chromium-real") is None
    staged = compliance._snap_stage_dir("/snap/bin/chromium")
    assert staged is not None and staged.exists()


def test_render_web_moves_staged_shots_out_of_snap_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RunPaths.create(tmp_path / "run")
    paths.screens_json.write_text('{"screens": [{"id": "0000"}, {"id": "0001"}]}')
    web = paths.flutter_app / "build" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<html></html>")

    stage = tmp_path / "snapstage"
    stage.mkdir()
    monkeypatch.setattr(compliance, "_snap_stage_dir", lambda _bin: stage)

    def _fake_run(cmd: list[str], **k: Any) -> Any:
        for arg in cmd:
            if arg.startswith("--screenshot="):
                Path(arg.split("=", 1)[1]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(compliance.subprocess, "run", _fake_run)

    result = compliance.render_generated_web(
        paths, chromium_bin="/snap/bin/chromium", wait_ms=100, window="390,844"
    )
    assert len(result["screens"]) == 2
    # shots were written into the snap stage dir, then moved into generated_screens_dir
    assert (paths.generated_screens_dir / "0000.png").is_file()
    assert (paths.generated_screens_dir / "0001.png").is_file()
    assert not (stage / "0000.png").exists()


def test_refine_per_screen_gate_waits_for_weakest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RunPaths.create(tmp_path / "run")
    # iteration 1: overall high but one screen weak; iteration 2: all >= 0.80
    reports = iter(
        [
            {
                "compliance_score": 0.90,
                "screens": [{"id": "a", "score": 0.95}, {"id": "b", "score": 0.50}],
            },
            {
                "compliance_score": 0.92,
                "screens": [{"id": "a", "score": 0.95}, {"id": "b", "score": 0.85}],
            },
        ]
    )
    prepared: list[int] = []
    monkeypatch.setattr(compliance, "evaluate", lambda *a, **k: next(reports))
    monkeypatch.setattr(compliance, "diffs_to_tasks", lambda r, i: {"tasks": [{"id": "fix"}]})
    monkeypatch.setattr(compliance, "apply_corrective", lambda *a, **k: None)

    report = compliance._refine(
        paths,
        lambda: prepared.append(1),
        threshold=0.80,
        soft_floor=0.80,
        max_iterations=5,
        weights=ComplianceWeights(visual=0.5, coverage=0.3, flows=0.2),
        timeout=1,
        per_screen=True,
    )
    # weakest screen gate: did NOT stop at iteration 1 (min 0.50), stopped at iteration 2 (min 0.85)
    assert len(prepared) == 2
    assert report["stop_reason"] == "all_screens_met"


def test_refine_overall_gate_stops_early(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = RunPaths.create(tmp_path / "run")
    reports = iter(
        [
            {
                "compliance_score": 0.90,
                "screens": [{"id": "a", "score": 0.95}, {"id": "b", "score": 0.50}],
            }
        ]
    )
    prepared: list[int] = []
    monkeypatch.setattr(compliance, "evaluate", lambda *a, **k: next(reports))

    report = compliance._refine(
        paths,
        lambda: prepared.append(1),
        threshold=0.80,
        soft_floor=0.80,
        max_iterations=5,
        weights=ComplianceWeights(visual=0.5, coverage=0.3, flows=0.2),
        timeout=1,
        per_screen=False,
    )
    # overall 0.90 >= 0.80 -> stops immediately despite screen b being 0.50
    assert len(prepared) == 1
    assert report["stop_reason"] == "threshold_met"


def test_refine_freeze_locks_passed_screens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RunPaths.create(tmp_path / "run")
    # iter1: a passes (0.9), b fails (0.5). iter2: judge NOISE drops a to 0.4, b now 0.85.
    reports = iter(
        [
            {
                "compliance_score": 0.7,
                "coverage": 1.0,
                "flows": 0.9,
                "screens": [
                    {"id": "a", "score": 0.9, "diffs": []},
                    {"id": "b", "score": 0.5, "diffs": ["x"]},
                ],
            },
            {
                "compliance_score": 0.6,
                "coverage": 1.0,
                "flows": 0.9,
                "screens": [
                    {"id": "a", "score": 0.4, "diffs": ["regressed"]},
                    {"id": "b", "score": 0.85, "diffs": []},
                ],
            },
        ]
    )
    monkeypatch.setattr(compliance, "evaluate", lambda *a, **k: next(reports))
    monkeypatch.setattr(
        compliance,
        "diffs_to_tasks",
        lambda r, i: {
            "tasks": [
                {"id": f"fix-{i}-{s['id']}"}
                for s in r["screens"]
                if float(s.get("score", 0)) < 0.80
            ]
        },
    )
    fixed: list[str] = []
    monkeypatch.setattr(
        compliance, "apply_corrective", lambda p, c, **k: fixed.extend(t["id"] for t in c["tasks"])
    )

    report = compliance._refine(
        paths,
        lambda: None,
        threshold=0.80,
        soft_floor=0.80,
        max_iterations=5,
        weights=ComplianceWeights(visual=0.5, coverage=0.3, flows=0.2),
        timeout=1,
        per_screen=True,
        freeze_passed=True,
    )
    # 'a' frozen at 0.9 (iter2 noise ignored) -> gate = min(0.9, 0.85) = 0.85 -> all_screens_met
    assert report["stop_reason"] == "all_screens_met"
    # only the failing 'b' was corrected; the passed 'a' was never re-fixed
    assert fixed == ["fix-1-b"]
