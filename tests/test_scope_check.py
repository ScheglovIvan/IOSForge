"""Scope-gate predicate (task 3a): a selection is accepted iff codegen can plan its navigation."""

from __future__ import annotations

import copy
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import feasibility, scope_check, swiftui_gen
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.scope_check import ScopeProblem, format_problems, navigation_problems
from iosforge.mvp.scope_models import FeasibilityReport, ScopeCounts, ScopeDecision, ScreenScope
from iosforge.mvp.spec_contract import SpecValidationError
from iosforge.mvp.swiftui_scaffold import build_plan
from iosforge.storage.client import build_key
from tests.test_feasibility import _spec_three_screens
from tests.test_swiftui_navigation import _tab_spec
from tests.test_swiftui_scaffold import _spec as _stack_fixture
from tests.test_swiftui_scaffold import _tabbed


def _valid(screens: list[dict[str, Any]], navigation: dict[str, Any]) -> dict[str, Any]:
    spec = _spec_three_screens()
    spec["screens"] = [{"purpose": "x", **s} for s in screens]
    spec["navigation"] = {"deep_links": [], "map": [], **navigation}
    spec["requirements"][0]["screens"] = []
    spec["screen_count"] = len(screens)
    spec["provenance"]["screen_count"] = len(screens)
    return spec


def _dogo(*, direct: bool = False, middle: str = "Profile") -> dict[str, Any]:
    edges = [
        {"from": "0011", "to": "0005", "via": "avatar"},
        {"from": "0005", "to": "0002", "via": "gear"},
    ]
    if direct:
        edges.append({"from": "0011", "to": "0002", "via": "settings"})
    return _valid(
        [
            {"id": "0011", "name": "Home"},
            {"id": "0005", "name": middle},
            {"id": "0002", "name": "Settings"},
        ],
        {"type": "tab_bar", "tabs": [{"screen_id": "0011", "title": "Home"}], "map": edges},
    )


def _from_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    return _valid(fixture["screens"], fixture["navigation"])


def _scope(spec: dict[str, Any], excluded: set[str], *, mode: str = "core") -> ScopeDecision:
    screens = [
        ScreenScope(
            screen_id=str(s["id"]), name=s["name"], include=s["id"] not in excluded, reason="x"
        )
        for s in spec["screens"]
    ]
    return ScopeDecision(
        status="proposed",
        scope_mode=mode,  # type: ignore[arg-type]
        screens=screens,
        feasibility=FeasibilityReport(overall_verdict="native", summary="ok"),
        counts=ScopeCounts.from_screens(screens),
        proposed_at=datetime.now(UTC),
    )


def test_excluded_intermediary_blocks_with_a_verified_suggestion() -> None:
    spec = _dogo()
    problems = navigation_problems(spec, _scope(spec, {"0005"}))
    assert len(problems) == 1
    problem = problems[0]
    assert problem.kind == "unreachable"
    assert problem.screen_ids == ["0002"]
    assert problem.suggest_include == ["0005"]
    assert "0002" in problem.message
    assert "'Profile' (0005)" in problem.message and "0011 → 0005 → 0002" in problem.message
    assert "any tab ('Home' 0011)" in problem.message


def test_included_intermediary_passes() -> None:
    spec = _dogo()
    assert navigation_problems(spec, _scope(spec, set())) == []


def test_direct_edge_survives_the_excluded_intermediary() -> None:
    spec = _dogo(direct=True)
    assert navigation_problems(spec, _scope(spec, {"0005"})) == []


def test_modal_intermediary_gets_no_suggestion() -> None:
    spec = _dogo(middle="Filter Sheet")
    problems = navigation_problems(spec, _scope(spec, {"0005"}))
    assert [(p.kind, p.screen_ids, p.suggest_include) for p in problems] == [
        ("unreachable", ["0002"], [])
    ]
    assert "no navigation path leads to it" in problems[0].message


def test_excluded_tab_root_is_a_tabs_problem() -> None:
    spec = _from_fixture(_tab_spec())
    assert navigation_problems(spec, _scope(spec, set())) == []
    problems = navigation_problems(spec, _scope(spec, {"0012"}))
    assert [(p.kind, p.screen_ids, p.suggest_include) for p in problems] == [
        ("tabs", ["0012"], ["0012"])
    ]
    assert "Excluded tab root screens: 'Modes' (0012). Include them." in problems[0].message


def test_stack_only_app_names_the_home_screen() -> None:
    fixture = _stack_fixture()
    fixture["screens"][3]["navigates_to"] = ["0011", "0014"]
    fixture["screens"].append({"id": "0014", "name": "Details"})
    spec = _from_fixture(fixture)
    problems = navigation_problems(spec, _scope(spec, {"0013"}))
    assert [(p.kind, p.screen_ids, p.suggest_include) for p in problems] == [
        ("unreachable", ["0014"], ["0013"])
    ]
    assert "the home screen 'Home - Test Hub' (0011)" in problems[0].message
    assert "tab" not in problems[0].message


@pytest.mark.parametrize("mode", ["full", "core"])
def test_full_mode_or_empty_include_checks_the_whole_spec(mode: str) -> None:
    spec = _dogo()
    excluded = {"0005"} if mode == "full" else {"0011", "0005", "0002"}
    assert navigation_problems(spec, _scope(spec, excluded, mode=mode)) == []
    assert navigation_problems(spec, None) == []


def test_invalid_scoped_spec_is_a_spec_problem() -> None:
    spec = _dogo()
    spec["requirements"] = [{"id": "R", "type": "ubiquitous"}]
    problems = navigation_problems(spec, _scope(spec, {"0005"}))
    assert [p.kind for p in problems] == ["spec"]


def _equivalence_cases() -> list[dict[str, Any]]:
    return [
        _dogo(),
        _dogo(direct=True),
        _dogo(middle="Filter Sheet"),
        _from_fixture(_tab_spec()),
        _from_fixture(_tabbed()),
        _from_fixture(_stack_fixture()),
    ]


@pytest.mark.parametrize("spec", _equivalence_cases())
def test_predicate_matches_codegen_planning(spec: dict[str, Any]) -> None:
    ids = [str(s["id"]) for s in spec["screens"]]
    for size in range(len(ids) + 1):
        for excluded in itertools.combinations(ids, size):
            scope = _scope(spec, set(excluded))
            try:
                build_plan(
                    feasibility.scoped_spec(spec, feasibility.resolve_scope_ids(spec, scope))
                )
                plans = True
            except (ValueError, SpecValidationError):
                plans = False
            assert (navigation_problems(spec, scope) == []) is plans, excluded


@pytest.mark.parametrize("ids", [["0011", "0002"], ["0011", "0005", "0002"], []])
def test_scoped_spec_matches_what_scope_to_writes(tmp_path: Path, ids: list[str]) -> None:
    spec = _dogo()
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(spec))
    swiftui_gen.scope_to(paths, ids)
    assert json.loads(paths.app_spec_json.read_text()) == feasibility.scoped_spec(spec, ids)


def test_scoped_spec_does_not_mutate_the_full_spec() -> None:
    spec = _dogo()
    before = copy.deepcopy(spec)
    feasibility.scoped_spec(spec, ["0011", "0002"])
    assert spec == before


def test_resolve_scope_ids_branches() -> None:
    spec = _dogo()
    every = ["0011", "0005", "0002"]
    assert feasibility.resolve_scope_ids(spec, None) == every
    assert feasibility.resolve_scope_ids(spec, _scope(spec, {"0005"}, mode="full")) == every
    assert feasibility.resolve_scope_ids(spec, _scope(spec, set(every))) == every
    assert feasibility.resolve_scope_ids(spec, _scope(spec, {"0005"})) == ["0011", "0002"]


class _Storage:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return self.blobs[key]


def test_load_full_spec_reads_the_stored_app_spec() -> None:
    key = build_key(job_id="j1", kind="app_spec", name="app_spec.json")
    spec = _dogo()
    storage = _Storage({key: json.dumps(spec).encode()})
    assert scope_check.load_full_spec(storage, "j1") == spec  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        scope_check.load_full_spec(_Storage({key: b"[]"}), "j1")  # type: ignore[arg-type]


def test_format_problems_lists_messages() -> None:
    problems = [
        ScopeProblem("unreachable", ["0002"], "first", []),
        ScopeProblem("tabs", [], "second", []),
    ]
    assert format_problems(problems) == "- first\n- second"
    assert format_problems([]) == ""
