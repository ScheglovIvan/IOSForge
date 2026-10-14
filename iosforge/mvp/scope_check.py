"""Scope-gate predicate: would code generation plan navigation for this screen selection?

The check runs exactly what the SwiftUI build runs (DECISIONS 2026-10-15, task 3a): it
resolves the screen ids like the build (:func:`feasibility.resolve_scope_ids`), prunes
the full app spec like ``scope_to`` (:func:`feasibility.scoped_spec`) and plans the
navigation with :func:`swiftui_scaffold.build_plan`. Failures become operator-facing
:class:`ScopeProblem` entries. The selection is never repaired: a suggestion to include
screens is offered only when the same check, re-run with them included, passes.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from iosforge.mvp import feasibility, swiftui_scaffold
from iosforge.mvp.scope_models import ScopeDecision
from iosforge.mvp.spec_contract import SpecValidationError
from iosforge.mvp.swiftui_navigation import (
    NavigationSpecError,
    UnreachableScreensError,
    _successors,
    derive_tabs,
)
from iosforge.storage.client import ArtifactStorage, build_key

ProblemKind = Literal["unreachable", "tabs", "spec"]


@dataclass(frozen=True)
class ScopeProblem:
    """One reason the selection cannot be approved, with the screens it concerns."""

    kind: ProblemKind
    screen_ids: list[str]
    message: str
    suggest_include: list[str]


def load_full_spec(storage: ArtifactStorage, job_id: str) -> dict[str, Any]:
    """The job's stored (unpruned) ``app_spec.json``, as the build hydrates it."""
    raw = storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
    spec = json.loads(raw)
    if not isinstance(spec, dict):
        raise ValueError("stored app_spec.json is not a JSON object")
    return spec


def navigation_problems(
    full_spec: dict[str, Any], scope: ScopeDecision | None
) -> list[ScopeProblem]:
    """Problems code generation would hit for ``scope`` on ``full_spec`` (``[]`` = buildable)."""
    return _problems(full_spec, feasibility.resolve_scope_ids(full_spec, scope), suggest=True)


def format_problems(problems: list[ScopeProblem]) -> str:
    """Problems as one plain-text block, one ``- message`` line each."""
    return "\n".join(f"- {p.message}" for p in problems)


def _problems(full: dict[str, Any], included: list[str], *, suggest: bool) -> list[ScopeProblem]:
    try:
        spec = feasibility.scoped_spec(full, included)
    except SpecValidationError as exc:
        return [_spec_problem(f"The app spec for this selection is invalid: {exc}")]
    try:
        swiftui_scaffold.build_plan(spec)
    except UnreachableScreensError as exc:
        return [
            _unreachable(full, spec, included, orphan, exc.roots, suggest=suggest)
            for orphan in exc.orphans
        ]
    except NavigationSpecError as exc:
        return [_tabs_problem(full, included, exc, suggest=suggest)]
    except ValueError as exc:
        return [_spec_problem(f"Code generation cannot plan this selection: {exc}")]
    return []


def _spec_problem(message: str) -> ScopeProblem:
    return ScopeProblem(kind="spec", screen_ids=[], message=message, suggest_include=[])


def _names(spec: dict[str, Any]) -> dict[str, str]:
    return {
        str(s["id"]): str(s.get("name") or s["id"])
        for s in spec.get("screens", [])
        if isinstance(s, dict) and "id" in s
    }


def _label(names: dict[str, str], screen_id: str) -> str:
    return f"'{names.get(screen_id, screen_id)}' ({screen_id})"


def _roots_phrase(spec: dict[str, Any], names: dict[str, str], roots: list[str]) -> str:
    try:
        tabbed = bool(derive_tabs(spec))
    except NavigationSpecError:
        tabbed = True
    if not tabbed and roots:
        return f"the home screen {_label(names, roots[0])}"
    listed = ", ".join(f"'{names.get(r, r)}' {r}" for r in roots)
    return f"any tab ({listed})"


def _shortest_path(graph: dict[str, list[str]], roots: list[str], target: str) -> list[str]:
    root_set = set(roots)
    for root in roots:
        parents: dict[str, str | None] = {root: None}
        queue = deque([root])
        while queue:
            current = queue.popleft()
            if current == target:
                path = [current]
                while (parent := parents[path[-1]]) is not None:
                    path.append(parent)
                return path[::-1]
            for nxt in graph.get(current, []):
                if nxt in parents or nxt in root_set:
                    continue
                parents[nxt] = current
                queue.append(nxt)
    return []


def _ordered(full: dict[str, Any], ids: set[str]) -> list[str]:
    return [sid for sid in _names(full) if sid in ids]


def _unreachable(
    full: dict[str, Any],
    spec: dict[str, Any],
    included: list[str],
    orphan: str,
    roots: list[str],
    *,
    suggest: bool,
) -> ScopeProblem:
    names = _names(full)
    head = (
        f"Screen {_label(names, orphan)} is not reachable from "
        f"{_roots_phrase(spec, names, roots)} in the selected scope"
    )
    path = _shortest_path(_successors(full), roots, orphan) if suggest else []
    kept = set(included)
    candidates = [sid for sid in path[1:-1] if sid not in kept]
    if candidates and _fixes(full, included, candidates, orphan):
        labels = ", ".join(_label(names, sid) for sid in candidates)
        route = " → ".join(path)
        message = (
            f"{head}: the only path goes through excluded screens. "
            f"Include {labels} (path {route}) or exclude {orphan}."
        )
        return ScopeProblem("unreachable", [orphan], message, candidates)
    message = (
        f"{head}: no navigation path leads to it. "
        f"Exclude {orphan} or fix navigation.map in the app spec."
    )
    return ScopeProblem("unreachable", [orphan], message, [])


def _fixes(full: dict[str, Any], included: list[str], extra: list[str], orphan: str) -> bool:
    widened = _ordered(full, set(included) | set(extra))
    recheck = _problems(full, widened, suggest=False)
    return all(p.kind == "unreachable" and orphan not in p.screen_ids for p in recheck)


def _tab_roots(full: dict[str, Any]) -> list[str]:
    try:
        return [t.screen_id for t in derive_tabs(full)]
    except NavigationSpecError:
        raw = (full.get("navigation") or {}).get("tabs")
        if not isinstance(raw, list):
            return []
        return [str(t["screen_id"]) for t in raw if isinstance(t, dict) and t.get("screen_id")]


def _tabs_problem(
    full: dict[str, Any], included: list[str], exc: NavigationSpecError, *, suggest: bool
) -> ScopeProblem:
    names = _names(full)
    kept = set(included)
    excluded = [sid for sid in _tab_roots(full) if sid not in kept]
    message = f"Tab bar cannot be built for this selection: {exc}."
    if not excluded:
        return ScopeProblem("tabs", [], message, [])
    labels = ", ".join(_label(names, sid) for sid in excluded)
    message += f" Excluded tab root screens: {labels}."
    widened = _ordered(full, kept | set(excluded))
    if suggest and all(p.kind != "tabs" for p in _problems(full, widened, suggest=False)):
        return ScopeProblem("tabs", excluded, message + " Include them.", excluded)
    return ScopeProblem("tabs", excluded, message, [])
