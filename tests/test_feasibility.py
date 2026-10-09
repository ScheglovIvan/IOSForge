"""Tests for the SCOPE stage: deterministic ``apply_scope`` prune + ``propose``.

``apply_scope`` is the load-bearing half (no model): it trims the App Spec to the
included screens and must leave NO dangling cross-references, so the pruned spec
re-passes the v3 contract. ``propose`` is exercised with the ``claude`` CLI stubbed
out (``_run_claude`` monkeypatched to drop a ``scope_proposal.json``), and the MinIO
save/load roundtrip uses an in-memory storage fake — no network / no subprocess.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.mvp import feasibility, spec_contract
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.scope_models import (
    FeasibilityReport,
    ScopeCounts,
    ScopeDecision,
    ScreenScope,
)
from iosforge.storage.client import ArtifactRef


def _spec_three_screens() -> dict[str, Any]:
    """A valid v3 spec with a third screen that the scope will drop."""
    return {
        "spec_version": spec_contract.SPEC_VERSION,
        "provenance": spec_contract.build_provenance(
            generator="iosforge/test",
            generated_at="2026-10-09T00:00:00Z",
            source_crawl_sha256="abc123",
            screen_count=3,
        ),
        "app_name": "Todo",
        "package": "com.example.todo",
        "app_type": "productivity",
        "one_liner": "A todo app",
        "description": "Manage tasks",
        "how_it_works": "Add and complete tasks",
        "target_audience": "everyone",
        "platforms": ["ios"],
        "market_research": {"similar_apps": [], "sources": []},
        "business_logic": {"summary": "tasks"},
        "screen_count": 3,
        "screens": [
            {"id": "0000", "name": "Home", "purpose": "list", "navigates_to": ["0001", "0002"]},
            {"id": "0001", "name": "Add", "purpose": "create", "navigates_to": ["0000"]},
            {"id": "0002", "name": "Onboarding", "purpose": "intro", "navigates_to": ["0000"]},
        ],
        "requirements": [
            {
                "id": "REQ-add-task",
                "type": "event_driven",
                "text": "When the user taps Add, the system shall open the Add screen.",
                "priority": "must",
                "screens": ["0000", "0001", "0002"],
                "acceptance": ["Given Home, when Add tapped, then Add screen shown"],
                "source": "observed",
            }
        ],
        "design_tokens": {"color": {"primary": {"$value": "#3366FF", "$type": "color"}}},
        "navigation": {
            "type": "stack",
            "deep_links": [],
            "map": [
                {"from": "0000", "to": "0001", "via": "Add"},
                {"from": "0000", "to": "0002", "via": "Intro"},
            ],
        },
        "content": {"data_model": [], "content_to_seed": []},
        "monetization": {"model": "free"},
        "backend": {"backend_needed": False, "admin_panel_needed": False},
        "permissions": [],
        "integrations": [],
        "cross_cutting": {"localization": ["en"]},
        "analysis_quality": {
            "assumptions": [],
            "open_questions": [],
            "coverage_gaps": [],
            "confidence": {"overall": "high"},
        },
        "acceptance_criteria": ["can add a task"],
    }


def _scope(
    *,
    mode: str = "core",
    include: dict[str, bool] | None = None,
) -> ScopeDecision:
    include = include or {"0000": True, "0001": True, "0002": False}
    screens = [
        ScreenScope(screen_id=sid, name=sid, include=inc, reason="x")
        for sid, inc in include.items()
    ]
    return ScopeDecision(
        status="approved",
        scope_mode=mode,  # type: ignore[arg-type]
        screens=screens,
        feasibility=FeasibilityReport(overall_verdict="native", summary="ok"),
        counts=ScopeCounts.from_screens(screens),
        proposed_at=datetime.now(UTC),
    )


def _write_spec(paths: RunPaths, spec: dict[str, Any]) -> None:
    paths.app_spec_json.write_text(json.dumps(spec))


def _read_spec(paths: RunPaths) -> dict[str, Any]:
    return json.loads(paths.app_spec_json.read_text())


# --------------------------------------------------------------------------- #
# apply_scope — deterministic prune
# --------------------------------------------------------------------------- #


def test_apply_scope_drops_excluded_screens(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    feasibility.apply_scope(paths, _scope())
    spec = _read_spec(paths)
    ids = {s["id"] for s in spec["screens"]}
    assert ids == {"0000", "0001"}


def test_apply_scope_cleans_dangling_navigates_to(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    feasibility.apply_scope(paths, _scope())
    spec = _read_spec(paths)
    home = next(s for s in spec["screens"] if s["id"] == "0000")
    assert home["navigates_to"] == ["0001"]  # 0002 removed


def test_apply_scope_drops_navigation_edges_to_dropped(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    feasibility.apply_scope(paths, _scope())
    spec = _read_spec(paths)
    edges = spec["navigation"]["map"]
    assert edges == [{"from": "0000", "to": "0001", "via": "Add"}]


def test_apply_scope_drops_tabs_of_dropped_screens(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    spec = _spec_three_screens()
    spec["navigation"]["tabs"] = [
        {"screen_id": "0000", "title": "Home"},
        {"screen_id": "0002", "title": "Intro"},
        {"screen_id": "0001", "title": "Add"},
    ]
    _write_spec(paths, spec)
    feasibility.apply_scope(paths, _scope())
    tabs = _read_spec(paths)["navigation"]["tabs"]
    assert tabs == [{"screen_id": "0000", "title": "Home"}, {"screen_id": "0001", "title": "Add"}]
    spec_contract.validate_spec(_read_spec(paths))


def test_apply_scope_leaves_tabless_spec_without_tabs(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    feasibility.apply_scope(paths, _scope())
    assert "tabs" not in _read_spec(paths)["navigation"]


def test_apply_scope_cleans_requirement_screens(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    feasibility.apply_scope(paths, _scope())
    spec = _read_spec(paths)
    assert spec["requirements"][0]["screens"] == ["0000", "0001"]


def test_apply_scope_recomputes_screen_counts(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    feasibility.apply_scope(paths, _scope())
    spec = _read_spec(paths)
    assert spec["screen_count"] == 2
    assert spec["provenance"]["screen_count"] == 2


def test_apply_scope_output_revalidates(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    feasibility.apply_scope(paths, _scope())
    # No dangling cross-references remain: the contract accepts the pruned spec.
    spec_contract.validate_spec(_read_spec(paths))


def test_apply_scope_full_mode_is_noop(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    original = _spec_three_screens()
    _write_spec(paths, original)
    feasibility.apply_scope(paths, _scope(mode="full"))
    assert _read_spec(paths) == original


def test_apply_scope_empty_include_fails_open(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    original = _spec_three_screens()
    _write_spec(paths, original)
    none_included = _scope(include={"0000": False, "0001": False, "0002": False})
    feasibility.apply_scope(paths, none_included)
    # fail-open: an empty core keeps the whole spec untouched rather than wiping it.
    assert _read_spec(paths) == original


def test_apply_scope_missing_scope_json_is_noop(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    original = _spec_three_screens()
    _write_spec(paths, original)
    assert not paths.scope_json.exists()
    feasibility.apply_scope(paths, None)
    assert _read_spec(paths) == original


def test_apply_scope_reads_scope_json_when_none(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    paths.scope_json.write_text(_scope().model_dump_json())
    feasibility.apply_scope(paths, None)
    spec = _read_spec(paths)
    assert {s["id"] for s in spec["screens"]} == {"0000", "0001"}


# --------------------------------------------------------------------------- #
# propose — claude CLI stubbed
# --------------------------------------------------------------------------- #


def _proposal_payload() -> dict[str, Any]:
    return {
        "scope_mode": "core",
        "screens": [
            {
                "screen_id": "0000",
                "name": "Home",
                "include": True,
                "reason": "core",
                "group": "core",
            },
            {
                "screen_id": "0001",
                "name": "Add",
                "include": True,
                "reason": "core",
                "group": "core",
            },
            {
                "screen_id": "0002",
                "name": "Onboarding",
                "include": False,
                "reason": "walkthrough",
                "group": "onboarding",
            },
        ],
        "core_flows": ["add a task"],
        "feasibility": {
            "overall_verdict": "partial",
            "summary": "mostly native",
            "findings": [
                {
                    "capability": "widgets",
                    "verdict": "blocked",
                    "note": "no api",
                    "screens": ["0002"],
                }
            ],
        },
        "notes": "lean core",
    }


def test_propose_parses_stubbed_claude_output(tmp_path: Path, monkeypatch: Any) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    payload = _proposal_payload()

    def _fake_run(prompt: str, workdir: Path, timeout: int, tools: str | None = None) -> None:
        (workdir / "scope_proposal.json").write_text(json.dumps(payload))

    monkeypatch.setattr(feasibility, "_run_claude", _fake_run)

    decision = feasibility.propose(paths, settings=Settings())

    assert decision.status == "proposed"
    assert decision.scope_mode == "core"
    assert decision.counts.total == 3
    assert decision.counts.included == 2
    assert decision.counts.excluded == 1
    assert decision.feasibility.overall_verdict == "partial"
    assert decision.feasibility.findings[0].verdict == "blocked"
    assert decision.core_flows == ["add a task"]
    # propose mirrors the decision into the run dir for the deterministic prune step.
    assert ScopeDecision.model_validate_json(paths.scope_json.read_text()).counts.included == 2


def test_propose_coerces_unknown_verdict_to_partial(tmp_path: Path, monkeypatch: Any) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    payload = _proposal_payload()
    payload["feasibility"]["overall_verdict"] = "who-knows"

    monkeypatch.setattr(
        feasibility,
        "_run_claude",
        lambda *a, **k: (a[1] / "scope_proposal.json").write_text(json.dumps(payload)),
    )
    decision = feasibility.propose(paths, settings=Settings())
    assert decision.feasibility.overall_verdict == "partial"


# --------------------------------------------------------------------------- #
# save_scope / load_scope — storage roundtrip
# --------------------------------------------------------------------------- #


class _MemStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.versions: list[str] = []

    def put(self, key: str, data: bytes, content_type: str | None = None) -> ArtifactRef:
        self.objects[key] = data
        vid = f"v{len(self.versions) + 1}"
        self.versions.append(vid)
        return ArtifactRef(
            bucket="b", key=key, version_id=vid, size=len(data), content_type=content_type or ""
        )

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return self.objects[key]


def test_scope_key_is_job_scoped() -> None:
    assert feasibility.scope_key("JOB-1") == "jobs/JOB-1/scope/scope.json"


def test_save_then_load_roundtrip() -> None:
    storage = _MemStorage()
    scope = _scope()
    ref = feasibility.save_scope(storage, "job-1", scope)  # type: ignore[arg-type]
    assert ref.version_id == "v1"
    loaded = feasibility.load_scope(storage, "job-1")  # type: ignore[arg-type]
    assert loaded == scope


def test_apply_scope_does_not_mutate_caller_scope(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    _write_spec(paths, _spec_three_screens())
    scope = _scope()
    before = copy.deepcopy(scope.model_dump())
    feasibility.apply_scope(paths, scope)
    assert scope.model_dump() == before
