"""Deterministic navigation structure for the SwiftUI scaffold (tabs + screen ownership).

Root tab navigation is a contract layer, so the scaffold renders it from data,
never from model output. :func:`derive_tabs` reads the explicit
``navigation.tabs`` field when the spec carries it, otherwise falls back to a
strict heuristic over older specs (``tab_bar`` components marked ``(active)``
plus ``"<title> tab"`` map edges) and raises :class:`NavigationSpecError` instead
of guessing when the result is ambiguous. :func:`tab_owners` assigns every
pushed screen to the first tab it is reachable from (breadth-first over
``navigation.map`` + ``navigates_to``); a screen reachable from no tab fails
loudly too.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_ACTIVE_SUFFIX = re.compile(r"\(\s*(?P<title>[^()]*?)\s*\bactive\s*\)", re.I)


class NavigationSpecError(ValueError):
    """The app_spec does not describe its navigation unambiguously."""


@dataclass(frozen=True)
class Tab:
    """One root tab: the screen at its root and its label."""

    screen_id: str
    title: str


def _is_tab_type(spec: dict[str, Any]) -> bool:
    nav = spec.get("navigation") or {}
    return "tab" in str(nav.get("type", "")).lower()


def _screen_ids(spec: dict[str, Any]) -> list[str]:
    return [str(s["id"]) for s in spec.get("screens", []) if isinstance(s, dict) and s.get("id")]


def _explicit_tabs(spec: dict[str, Any], ids: set[str]) -> list[Tab] | None:
    raw = (spec.get("navigation") or {}).get("tabs")
    if raw is None:
        return None
    tabs = [Tab(str(t["screen_id"]), str(t["title"])) for t in raw]
    unknown = [t.screen_id for t in tabs if t.screen_id not in ids]
    if unknown:
        raise NavigationSpecError(f"navigation.tabs references unknown screens: {unknown}")
    return tabs


def has_tab_bar(screen: dict[str, Any]) -> bool:
    """True when the screen's components include a tab bar (it is drawn on that screen)."""
    return _tab_bar_text(screen) is not None


def _tab_bar_text(screen: dict[str, Any]) -> str | None:
    for component in screen.get("components", []) or []:
        if not isinstance(component, dict):
            continue
        kind = f"{component.get('type', '')} {component.get('role', '')}".lower()
        if "tab_bar" in kind or "tabbar" in kind or "tab bar" in kind:
            return str(component.get("data") or "")
    return None


def _parse_tab_bar(text: str) -> tuple[list[str], str | None]:
    """Titles in order plus the active one, from e.g. ``"A (active) / B / C (C active)"``."""
    active: str | None = None
    titles: list[str] = []
    for part in text.split("/"):
        match = _ACTIVE_SUFFIX.search(part)
        title = _ACTIVE_SUFFIX.sub("", part).strip()
        if match:
            named = match.group("title").strip()
            active = named or title
        if title:
            titles.append(title)
    return titles, active


def _heuristic_tabs(spec: dict[str, Any]) -> list[Tab]:
    screens = {str(s["id"]): s for s in spec.get("screens", []) if isinstance(s, dict)}
    bars: dict[str, tuple[list[str], str | None]] = {}
    for sid, screen in screens.items():
        text = _tab_bar_text(screen)
        if text is not None:
            bars[sid] = _parse_tab_bar(text)
    if not bars:
        raise NavigationSpecError(
            "navigation.type is tab-based but no screen has a tab_bar component; "
            "add navigation.tabs to the spec"
        )
    orders = {tuple(titles) for titles, _ in bars.values()}
    if len(orders) != 1:
        raise NavigationSpecError(
            f"tab_bar components disagree on the tab list: {sorted(orders)}; "
            "add navigation.tabs to the spec"
        )
    titles = list(next(iter(orders)))
    by_title: dict[str, list[str]] = {}
    for sid, (_, active) in bars.items():
        if active:
            by_title.setdefault(active.lower(), []).append(sid)
    edges = (spec.get("navigation") or {}).get("map", []) or []
    tabs: list[Tab] = []
    for title in titles:
        owners = by_title.get(title.lower(), [])
        if not owners:
            owners = sorted(
                {
                    str(e.get("to"))
                    for e in edges
                    if isinstance(e, dict)
                    and re.fullmatch(
                        rf"{re.escape(title)}\s+tab", str(e.get("via", "")).strip(), re.I
                    )
                    and str(e.get("to")) in screens
                }
            )
        if len(owners) != 1:
            raise NavigationSpecError(
                f"cannot map tab {title!r} to exactly one screen (candidates: {owners}); "
                "add navigation.tabs to the spec"
            )
        tabs.append(Tab(owners[0], title))
    if len({t.screen_id for t in tabs}) != len(tabs):
        raise NavigationSpecError(f"two tabs map to the same screen: {tabs}")
    return tabs


def derive_tabs(spec: dict[str, Any]) -> list[Tab]:
    """Root tabs of the app: explicit ``navigation.tabs``, else heuristic, else error.

    Returns ``[]`` for apps whose navigation type is not tab-based. An empty
    ``navigation.tabs`` on a tab-based app (e.g. every tab root pruned by scope) is
    treated like a missing field, so the heuristic runs and fails loudly if needed.
    """
    explicit = _explicit_tabs(spec, set(_screen_ids(spec)))
    if explicit:
        return explicit
    if not _is_tab_type(spec):
        return []
    return _heuristic_tabs(spec)


def _successors(spec: dict[str, Any]) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for edge in (spec.get("navigation") or {}).get("map", []) or []:
        if isinstance(edge, dict):
            graph.setdefault(str(edge.get("from")), []).append(str(edge.get("to")))
    for screen in spec.get("screens", []):
        if isinstance(screen, dict):
            for target in screen.get("navigates_to", []) or []:
                graph.setdefault(str(screen.get("id")), []).append(str(target))
    return graph


def tab_owners(
    spec: dict[str, Any],
    tabs: list[Tab],
    *,
    stack_screens: Iterable[str],
) -> dict[str, str]:
    """Map every stack (pushed) screen to the tab root whose stack it lives in.

    ``stack_screens`` are the screens that must live inside a tab stack (not
    onboarding, not modal, not a tab root). Breadth-first from each tab root in
    tab order; traversal does not pass through modal/onboarding screens. Raises
    :class:`NavigationSpecError` listing screens reachable from no tab.
    """
    pending = set(stack_screens)
    roots = [t.screen_id for t in tabs]
    owners: dict[str, str] = {root: root for root in roots}
    graph = _successors(spec)
    passable = pending | set(roots)
    for root in roots:
        seen = {root}
        queue = deque([root])
        while queue:
            current = queue.popleft()
            for nxt in graph.get(current, []):
                if nxt in seen or nxt not in passable:
                    continue
                seen.add(nxt)
                owners.setdefault(nxt, root)
                queue.append(nxt)
    orphans = sorted(pending - owners.keys())
    if orphans:
        raise NavigationSpecError(
            f"screens reachable from no tab: {orphans}; exclude them via scope or fix "
            "navigation.map"
        )
    return {sid: owners[sid] for sid in pending}
