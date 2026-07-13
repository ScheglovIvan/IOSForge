"""Tests for Stage VERIFY — structural web verification (pure functions).

Covers the file-static navigation audit (:func:`compliance.nav_audit`), the
byte-size blank detector (:func:`compliance.blank_screens`), the corrective
task/prompt expansion (:func:`compliance.diffs_to_tasks` /
``compliance._corrective_prompt``) and the composite structural gate in
``compliance._refine``.

No Flutter/Chromium/emulator here — every unit reads only files under a
:class:`RunPaths` built from ``tmp_path`` (``flutter_app/lib/**.dart``,
``screens.json``, ``generated_screens/``).
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


def _write_dart(paths: RunPaths, relpath: str, content: str) -> None:
    """Write a Dart source under ``flutter_app/lib/<relpath>``."""
    dest = paths.flutter_app / "lib" / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")


def _write_screens(paths: RunPaths, screens: list[dict[str, Any]]) -> None:
    paths.screens_json.write_text(json.dumps({"screens": screens}))


# --------------------------------------------------------------------------- #
# nav_audit
# --------------------------------------------------------------------------- #


def test_nav_audit_all_clean(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_dart(
        paths,
        "core/router/app_router.dart",
        (
            "final router = GoRouter(routes: [\n"
            "  GoRoute(path: '/', name: 'home', builder: (c, s) => const HomeScreen()),\n"
            "  GoRoute(path: '/detail', builder: (c, s) => const DetailScreen()),\n"
            "  GoRoute(path: '/screen/:id', builder: (c, s) => const PreviewScreen()),\n"
            "]);\n"
        ),
    )
    _write_dart(
        paths,
        "features/home/home_screen.dart",
        "class HomeScreen extends StatelessWidget {\n"
        "  void tap(BuildContext context) { context.go('/detail'); }\n"
        "}\n",
    )
    _write_screens(
        paths,
        [
            {"id": "home", "route": "/", "navigates_to": [{"to": "detail", "via_element": "card"}]},
            {"id": "detail", "route": "/detail", "navigates_to": []},
        ],
    )

    report = compliance.nav_audit(paths)

    assert report["ok"] is True
    assert report["missing_screens"] == []
    assert report["dead_links"] == []
    assert report["missing_edges"] == []


def test_nav_audit_missing_screen(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_dart(
        paths,
        "core/router/app_router.dart",
        (
            "final router = GoRouter(routes: [\n"
            "  GoRoute(path: '/', builder: (c, s) => const HomeScreen()),\n"
            "  GoRoute(path: '/detail', builder: (c, s) => const DetailScreen()),\n"
            "]);\n"
        ),
    )
    _write_screens(
        paths,
        [
            {"id": "home", "route": "/", "navigates_to": []},
            {"id": "detail", "route": "/detail", "navigates_to": []},
            {"id": "settings", "route": "/settings", "navigates_to": []},
        ],
    )

    report = compliance.nav_audit(paths)

    assert report["ok"] is False
    assert {"id": "settings", "expected_route": "/settings"} in report["missing_screens"]
    missing_ids = {m["id"] for m in report["missing_screens"]}
    assert "home" not in missing_ids
    assert "detail" not in missing_ids


def test_nav_audit_dead_link_and_parametric_coverage(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_dart(
        paths,
        "core/router/app_router.dart",
        (
            "final router = GoRouter(routes: [\n"
            "  GoRoute(path: '/', builder: (c, s) => const HomeScreen()),\n"
            "  GoRoute(path: '/screen/:id', builder: (c, s) => const PreviewScreen()),\n"
            "]);\n"
        ),
    )
    _write_dart(
        paths,
        "features/home/home_screen.dart",
        "class HomeScreen extends StatelessWidget {\n"
        "  void a(BuildContext context) { context.go('/nope'); }\n"
        "  void b(BuildContext context) { context.go('/screen/42'); }\n"
        "}\n",
    )
    _write_screens(paths, [{"id": "home", "route": "/", "navigates_to": []}])

    report = compliance.nav_audit(paths)

    targets = {d["target"] for d in report["dead_links"]}
    assert "/nope" in targets
    assert "/screen/42" not in targets


def test_nav_audit_missing_edge_and_unlocatable_source_skipped(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_dart(
        paths,
        "core/router/app_router.dart",
        (
            "final router = GoRouter(routes: [\n"
            "  GoRoute(path: '/', builder: (c, s) => const HomeScreen()),\n"
            "  GoRoute(path: '/detail', builder: (c, s) => const DetailScreen()),\n"
            "]);\n"
        ),
    )
    _write_dart(
        paths,
        "features/home/home_screen.dart",
        "class HomeScreen extends StatelessWidget {\n  Widget build() => const SizedBox();\n}\n",
    )
    _write_screens(
        paths,
        [
            {"id": "home", "route": "/", "navigates_to": [{"to": "detail", "via_element": "card"}]},
            {"id": "detail", "route": "/detail", "navigates_to": []},
            {"id": "ghost", "route": "/ghost", "navigates_to": [{"to": "detail"}]},
        ],
    )

    report = compliance.nav_audit(paths)

    edge_from = {(e["from"], e["to"]) for e in report["missing_edges"]}
    assert ("home", "detail") in edge_from
    assert not any(e["from"] == "ghost" for e in report["missing_edges"])


def test_nav_audit_parametric_spec_route_not_missing(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_dart(
        paths,
        "core/router/app_router.dart",
        (
            "final router = GoRouter(routes: [\n"
            "  GoRoute(path: '/', builder: (c, s) => const HomeScreen()),\n"
            "  GoRoute(path: '/item/:id', builder: (c, s) => const ItemScreen()),\n"
            "]);\n"
        ),
    )
    _write_screens(
        paths,
        [
            {"id": "home", "route": "/", "navigates_to": []},
            {"id": "item", "route": "/item/:id", "navigates_to": []},
        ],
    )

    report = compliance.nav_audit(paths)

    assert report["missing_screens"] == []
    assert report["ok"] is True


def test_nav_audit_query_and_interpolation_targets_not_dead(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_dart(
        paths,
        "core/router/app_router.dart",
        (
            "final router = GoRouter(routes: [\n"
            "  GoRoute(path: '/', builder: (c, s) => const HomeScreen()),\n"
            "  GoRoute(path: '/detail', builder: (c, s) => const DetailScreen()),\n"
            "]);\n"
        ),
    )
    _write_dart(
        paths,
        "features/home/home_screen.dart",
        "class HomeScreen extends StatelessWidget {\n"
        "  void a(BuildContext context) { context.go('/detail?tab=1'); }\n"
        "  void b(BuildContext context) { context.go('/item/$id'); }\n"
        "}\n",
    )
    _write_screens(paths, [{"id": "home", "route": "/", "navigates_to": []}])

    report = compliance.nav_audit(paths)

    targets = {d["target"] for d in report["dead_links"]}
    assert "/detail?tab=1" not in targets
    assert not any("$" in t for t in targets)
    assert report["dead_links"] == []


def test_nav_audit_named_route_resolution(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_dart(
        paths,
        "core/router/app_router.dart",
        (
            "final router = GoRouter(routes: [\n"
            "  GoRoute(name: 'home', path: '/', builder: (c, s) => const HomeScreen()),\n"
            "]);\n"
        ),
    )
    _write_dart(
        paths,
        "features/menu/menu_screen.dart",
        "class MenuScreen extends StatelessWidget {\n"
        "  void go(BuildContext context) { context.goNamed('home'); }\n"
        "}\n",
    )
    _write_screens(paths, [{"id": "home", "route": "/", "navigates_to": []}])

    report = compliance.nav_audit(paths)

    targets = {d["target"] for d in report["dead_links"]}
    assert "home" not in targets


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
# _corrective_prompt (one branch per assert)
# --------------------------------------------------------------------------- #


def test_corrective_prompt_add_screen() -> None:
    prompt = compliance._corrective_prompt(
        {"type": "add_screen", "screens": ["settings"], "expected_route": "/settings"}
    )
    assert "MISSING screen" in prompt
    assert "settings" in prompt
    assert "/settings" in prompt


def test_corrective_prompt_fix_blank() -> None:
    prompt = compliance._corrective_prompt({"type": "fix_blank", "screens": ["blankid"]})
    assert "BLANK screen" in prompt
    assert "blankid" in prompt


def test_corrective_prompt_fix_dead_link() -> None:
    prompt = compliance._corrective_prompt(
        {"type": "fix_dead_link", "target": "/nope", "file": "home.dart"}
    )
    assert "DEAD navigation link" in prompt
    assert "/nope" in prompt
    assert "home.dart" in prompt


def test_corrective_prompt_add_edge() -> None:
    prompt = compliance._corrective_prompt(
        {"type": "add_edge", "from": "home", "to": "detail", "via_element": "card"}
    )
    assert "MISSING navigation edge" in prompt
    assert "home" in prompt
    assert "detail" in prompt
    assert "card" in prompt


def test_corrective_prompt_default_visual() -> None:
    prompt = compliance._corrective_prompt(
        {"type": "fix", "screens": ["0001"], "diffs": ["button missing"]}
    )
    assert "correcting the LAYOUT of one screen" in prompt
    assert "0001" in prompt
    assert "generated_screens/0001.png" in prompt


def test_corrective_prompts_differ_across_types() -> None:
    prompts = {
        compliance._corrective_prompt({"type": "add_screen", "screens": ["s"]}),
        compliance._corrective_prompt({"type": "fix_blank", "screens": ["s"]}),
        compliance._corrective_prompt({"type": "fix_dead_link", "target": "/x", "file": "f"}),
        compliance._corrective_prompt({"type": "add_edge", "from": "a", "to": "b"}),
        compliance._corrective_prompt({"type": "fix", "screens": ["s"], "diffs": []}),
    }
    assert len(prompts) == 5


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
    monkeypatch.setattr(compliance, "apply_corrective", lambda *a, **k: paths.flutter_app)

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
