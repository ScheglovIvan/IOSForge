"""Phase 4 iOS refine: Swift nav audit, corrective rounds, loop wiring (toolchain mocked)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import compliance, ios_pipeline, simulator, swiftui_gen, xcode
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_prompts import components_prompt, corrective_prompt
from iosforge.mvp.swiftui_scaffold import build_plan

SPEC: dict[str, Any] = {
    "app_name": "Speaker Test",
    "screens": [
        {"id": "0000", "name": "Splash / Launch", "navigates_to": ["0011"]},
        {"id": "0001", "name": "Paywall - Pro", "navigates_to": ["0011"]},
        {"id": "0011", "name": "Home", "route": "/", "navigates_to": ["0003", "0001", "0008"]},
        {"id": "0003", "name": "Settings", "navigates_to": ["0004"]},
        {"id": "0004", "name": "Language", "navigates_to": ["0003"]},
        {"id": "0008", "name": "Instructions"},
    ],
    "navigation": {"type": "stack", "map": [], "deep_links": []},
    "permissions": [],
}


def _view(paths: RunPaths, sid: str, body: str) -> None:
    folder = paths.xcode_app / "App" / "Features" / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"Screen{sid}View.swift").write_text(f"struct Screen{sid}View: View {{\n{body}\n}}\n")


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path / "runs")
    rp.app_spec_json.write_text(json.dumps(SPEC))
    swiftui_gen.write_scaffold(rp.xcode_app, SPEC, app_name="Speaker Test", bundle_id="com.ex.s")
    rp.generated_screens_json.write_text(
        json.dumps({"screens": [{"id": s["id"]} for s in SPEC["screens"]]})
    )
    return rp


def test_nav_audit_accepts_every_wiring_style(paths: RunPaths) -> None:
    _view(paths, "0000", "func go() { router.finishOnboarding() }")
    _view(paths, "0001", "func close() { router.dismiss() }")
    _view(
        paths, "0011", "func a() { router.show(.s0003); router.show(.s0001); router.show(.s0008) }"
    )
    _view(paths, "0003", "func a() { router.show(.s0004) }")
    _view(paths, "0004", "func save() { router.dismiss() }")
    report = compliance.nav_audit_ios(paths)
    assert report == {"missing_screens": [], "dead_links": [], "missing_edges": [], "ok": True}


def test_nav_audit_reports_unwired_edges_and_unrendered_screens(paths: RunPaths) -> None:
    _view(paths, "0011", '// router.show(.s0003)\\nlet t = "router.show(.s0001)"')
    paths.generated_screens_json.write_text(json.dumps({"screens": [{"id": "0011"}]}))
    report = compliance.nav_audit_ios(paths)
    missing = {(e["from"], e["to"]) for e in report["missing_edges"]}
    assert {("0011", "0003"), ("0011", "0001"), ("0011", "0008"), ("0003", "0004")} <= missing
    assert [m["id"] for m in report["missing_screens"]] == ["0000", "0001", "0003", "0004", "0008"]
    assert not report["ok"]


def test_apply_corrective_routes_swiftui_apps(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(swiftui_gen, "correct", lambda p, tasks, **k: seen.append(tasks) or [])
    tasks = {"tasks": [{"id": "fix-1-0011", "type": "fix", "screens": ["0011"], "diffs": ["x"]}]}
    assert compliance.apply_corrective(paths, tasks) == paths.xcode_app
    assert seen == [tasks["tasks"]]


def test_correct_runs_one_sandbox_per_screen(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[tuple[str, str]] = []

    def run(workspace: Path, prompt: str, **_: Any) -> int:
        sid = "0011" if "screen `0011`" in prompt else "0003"
        prompts.append((sid, prompt))
        view = workspace / f"xcode_app/App/Features/{sid}/Screen{sid}View.swift"
        view.write_text(f"struct Screen{sid}View: View {{ // fixed\\n}}\\n")
        (workspace / "xcode_app/App/Theme/Theme.swift").write_text(
            "enum Theme { static let x = 1 }\\n"
        )
        return 0

    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", run)
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    tasks = [
        {"id": "fix-1-0011", "type": "fix", "screens": ["0011"], "diffs": ["Card too tall"]},
        {"id": "diverge-1-0011", "type": "diverge", "screens": ["0011"]},
        {
            "id": "edge-1-0",
            "type": "add_edge",
            "from": "0003",
            "to": "0004",
            "via_element": "Language row",
        },
        {"id": "x", "type": "fix", "screens": ["9999"]},
    ]

    runs = swiftui_gen.correct(paths, tasks)

    assert sorted(r.task for r in runs) == ["fix-0003", "fix-0011"]
    combined = dict(prompts)
    assert (
        "Card too tall" in combined["0011"]
        and "LOOKS TOO MUCH like the original" in combined["0011"]
    )
    assert "`router.show(.s0004)` (screen 0004)" in combined["0003"]
    assert "// fixed" in (paths.xcode_app / "App/Features/0011/Screen0011View.swift").read_text()
    assert "x = 1" not in (paths.xcode_app / "App/Theme/Theme.swift").read_text()


def test_correct_refuses_a_non_compiling_result(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", lambda *a, **k: 0)
    monkeypatch.setattr(
        swiftui_gen,
        "ensure_compiles",
        lambda *a, **k: (swiftui_gen.GateCheck(["App/X.swift:1:1: error: boom"]), []),
    )
    with pytest.raises(RuntimeError, match="non-compiling"):
        swiftui_gen.correct(paths, [{"id": "f", "type": "fix", "screens": ["0011"]}])


def test_identity_from_project(paths: RunPaths) -> None:
    identity = swiftui_gen.identity_from_project(paths.xcode_app)
    assert identity.app_name == "Speaker Test" and identity.bundle_id == "com.ex.s"


def test_refine_ios_merges_never_drawn_frames_into_blank_screens(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    for sid in ("0000", "0001", "0011", "0003", "0004", "0008"):
        calls = "router.show(.s0003); router.show(.s0001); router.show(.s0008); router.show(.s0004)"
        _view(paths, sid, f"func a() {{ {calls}; router.dismiss(); router.finishOnboarding() }}")
    monkeypatch.setattr(compliance, "build_ios", lambda p, **k: Path("/tmp/App.app"))

    def render(p: RunPaths, env: Any, *, app: Path) -> dict[str, Any]:
        shots = [
            {
                "id": s["id"],
                "screenshot": f"generated_screens/{s['id']}.png",
                "blank": s["id"] == "0008",
            }
            for s in SPEC["screens"]
        ]
        for shot in shots:
            (p.run_dir / shot["screenshot"]).write_bytes(b"x" * 9000)
        p.generated_screens_json.write_text(json.dumps({"screens": shots}))
        return {"screens": shots}

    monkeypatch.setattr(compliance, "render_generated_ios", render)
    monkeypatch.setattr(
        compliance,
        "evaluate",
        lambda p, it, **k: {"compliance_score": 0.95, "divergence": 0.7, "screens": [
            {"id": s["id"], "score": 0.95, "divergence": 0.7} for s in SPEC["screens"]]},
    )  # fmt: skip
    applied: list[Any] = []
    monkeypatch.setattr(compliance, "apply_corrective", lambda p, t, **k: applied.append(t))

    report = compliance.refine_ios_until_complete(
        paths, simulator.SimEnvironment(udid="U", locale="en-US"),
        threshold=0.8, soft_floor=0.8, max_iterations=2,
        weights=compliance.ComplianceWeights(0.4, 0.3, 0.1, 0.2, 0.4), blank_max_bytes=6000,
    )  # fmt: skip

    assert report["structural"]["blank_screens"] == [{"id": "0008", "reason": "never_drawn"}]
    assert report["stop_reason"] == "max_iterations"
    blank_tasks = [t for t in applied[0]["tasks"] if t["type"] == "fix_blank"]
    assert [t["screens"] for t in blank_tasks] == [["0008"]]


def test_prompts_for_tab_labels_and_corrections() -> None:
    plan = build_plan(
        {
            "screens": [
                {"id": "a", "components": [{"type": "tab_bar", "data": "Home (active) / More"}]},
                {"id": "b", "components": [{"type": "tab_bar", "data": "Home / More (active)"}]},
            ],
            "navigation": {"type": "tab_bar_with_stack", "map": []},
        }
    )
    assert "never `tab.title` verbatim" in components_prompt(plan, prompters=[])
    entry = plan.entries[0]
    text = corrective_prompt(
        [{"type": "fix_blank"}, {"type": "add_edge", "to": "b", "to_case": "sB"}],
        entry,
        prompters=[],
    )
    assert "renders blank" in text and "`router.show(.sB)` (screen b)" in text
    assert "App/Features/a/" in text


def test_judge_prompt_keeps_diffs_actionable() -> None:
    assert 'no "no fix needed"' in compliance.JUDGE_PROMPT


def test_pipeline_cli_exits_2_without_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    assert ios_pipeline.main(["--run-dir", "/tmp/x", "--udid", "U"]) == 2


def test_planned_screens_without_a_capture_are_rendered_but_not_judged(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = ["0000", "0001", "0011"]
    paths.screens_json.write_text(json.dumps({"screens": [{"id": sid} for sid in captured]}))
    (paths.xcode_app / "project.yml").write_text(
        "name: SpeakerTest\ntargets:\n  SpeakerTest:\n    settings:\n      base:\n"
        "        PRODUCT_BUNDLE_IDENTIFIER: com.ex.s\n"
    )
    rendered: list[str] = []
    monkeypatch.setattr(simulator, "require_toolchain", lambda: None)
    monkeypatch.setattr(simulator, "pin_environment", lambda env: None)
    monkeypatch.setattr(xcode, "install", lambda udid, app: None)

    def render(env: Any, bundle: str, sid: str, out: Path) -> simulator.StableShot:
        rendered.append(sid)
        return simulator.StableShot(out, 2, 0.0)

    monkeypatch.setattr(simulator, "render_screen", render)
    result = compliance.render_generated_ios(
        paths, simulator.SimEnvironment(udid="U", locale="en-US"), app=Path("/tmp/App.app")
    )

    planned = [e.screen_id for e in build_plan(SPEC).entries]
    assert rendered[:3] == captured and sorted(rendered) == sorted(set(captured) | set(planned))
    assert compliance.nav_audit_ios(paths)["missing_screens"] == []
    pairs = compliance.match_screens(
        json.loads(paths.screens_json.read_text())["screens"], result["screens"]
    )
    assert [oid for oid, _ in pairs] == captured


def test_judge_workspace_holds_only_judged_renders(paths: RunPaths) -> None:
    paths.screens_json.write_text(json.dumps({"screens": [{"id": "0000"}, {"id": "0011"}]}))
    paths.screens_dir.mkdir(parents=True, exist_ok=True)
    paths.generated_screens_dir.mkdir(parents=True, exist_ok=True)
    for sid in ("0000", "0011"):
        (paths.screens_dir / f"{sid}.png").write_bytes(b"o")
    for sid in ("0000", "0011", "0008"):
        (paths.generated_screens_dir / f"{sid}.png").write_bytes(b"g")
    compliance._prepare_judge_workspace(paths)
    judged = sorted(p.name for p in (paths.claude_ws / "generated_screens").glob("*.png"))
    assert judged == ["0000.png", "0011.png"]


def test_render_ids_fall_back_to_originals_without_a_plan(paths: RunPaths) -> None:
    paths.screens_json.write_text(json.dumps({"screens": [{"id": "0000"}]}))
    paths.app_spec_json.write_text(json.dumps({"screens": []}))
    assert compliance._render_ids(paths) == ["0000"]
