"""Deterministic tab derivation and tab ownership for the SwiftUI scaffold."""

from __future__ import annotations

from typing import Any

import pytest

from iosforge.mvp.swiftui_navigation import (
    NavigationSpecError,
    Tab,
    _parse_tab_bar,
    derive_tabs,
    tab_owners,
)


def _bar(text: str) -> dict[str, Any]:
    return {"type": "tab_bar", "role": "navigation", "data": text}


def _tab_spec(**nav: Any) -> dict[str, Any]:
    return {
        "screens": [
            {
                "id": "0011",
                "name": "Home",
                "components": [_bar("Cleaner (active) / Mode / Stereo")],
            },
            {
                "id": "0012",
                "name": "Modes",
                "components": [_bar("Cleaner / Mode (active) / Stereo")],
            },
            {
                "id": "0002",
                "name": "Channel Test",
                "components": [_bar("Cleaner / Mode / Stereo (Stereo active)")],
            },
            {"id": "0008", "name": "Instructions", "navigates_to": ["0006"]},
            {"id": "0006", "name": "Done"},
            {"id": "0001", "name": "Paywall"},
        ],
        "navigation": {
            "type": "tab_bar_with_stack",
            "map": [
                {"from": "0011", "to": "0012", "via": "Mode tab"},
                {"from": "0011", "to": "0001", "via": "crown"},
                {"from": "0012", "to": "0008", "via": "select a mode"},
                {"from": "0011", "to": "0008", "via": "start"},
            ],
            "deep_links": [],
            **nav,
        },
    }


@pytest.mark.parametrize(
    ("text", "titles", "active"),
    [
        (
            "Cleaner (active) / Mode / dB Meter / Stereo",
            ["Cleaner", "Mode", "dB Meter", "Stereo"],
            "Cleaner",
        ),
        (
            "Cleaner / Mode / dB Meter / Stereo (Stereo active)",
            ["Cleaner", "Mode", "dB Meter", "Stereo"],
            "Stereo",
        ),
        ("Home / Profile", ["Home", "Profile"], None),
    ],
)
def test_parse_tab_bar(text: str, titles: list[str], active: str | None) -> None:
    assert _parse_tab_bar(text) == (titles, active)


def test_heuristic_tabs_follow_tab_bar_order() -> None:
    assert derive_tabs(_tab_spec()) == [
        Tab("0011", "Cleaner"),
        Tab("0012", "Mode"),
        Tab("0002", "Stereo"),
    ]


def test_explicit_navigation_tabs_win() -> None:
    spec = _tab_spec(
        tabs=[{"screen_id": "0012", "title": "Modes"}, {"screen_id": "0011", "title": "Home"}]
    )
    assert derive_tabs(spec) == [Tab("0012", "Modes"), Tab("0011", "Home")]


def test_explicit_tabs_with_unknown_screen_fail() -> None:
    with pytest.raises(NavigationSpecError, match="unknown screens"):
        derive_tabs(_tab_spec(tabs=[{"screen_id": "9999", "title": "X"}]))


def test_non_tab_app_has_no_tabs() -> None:
    spec = _tab_spec()
    spec["navigation"]["type"] = "stack"
    assert derive_tabs(spec) == []


def test_disagreeing_tab_bars_fail_loud() -> None:
    spec = _tab_spec()
    spec["screens"][1]["components"] = [_bar("Cleaner / Mode (active)")]
    with pytest.raises(NavigationSpecError, match="disagree"):
        derive_tabs(spec)


def test_unmappable_tab_fails_loud() -> None:
    spec = _tab_spec()
    spec["screens"][2]["components"] = [_bar("Cleaner / Mode / Stereo")]
    with pytest.raises(NavigationSpecError, match="'Stereo'"):
        derive_tabs(spec)


def test_tab_mapped_from_edge_when_no_active_marker() -> None:
    spec = _tab_spec()
    spec["screens"][1]["components"] = [_bar("Cleaner / Mode / Stereo")]
    assert derive_tabs(spec)[1] == Tab("0012", "Mode")


def test_tab_bar_type_without_any_bar_fails_loud() -> None:
    spec = _tab_spec()
    for screen in spec["screens"]:
        screen.pop("components", None)
    with pytest.raises(NavigationSpecError, match="no screen has a tab_bar"):
        derive_tabs(spec)


def test_owners_are_first_reachable_tab_in_tab_order() -> None:
    tabs = derive_tabs(_tab_spec())
    owners = tab_owners(_tab_spec(), tabs, stack_screens=["0008", "0006"])
    assert owners == {"0008": "0011", "0006": "0011"}


def test_traversal_does_not_pass_through_modal_screens() -> None:
    spec = _tab_spec()
    spec["navigation"]["map"] = [
        {"from": "0011", "to": "0001", "via": "crown"},
        {"from": "0001", "to": "0006", "via": "after purchase"},
    ]
    with pytest.raises(NavigationSpecError, match=r"reachable from no tab: \['0006'\]"):
        tab_owners(spec, [Tab("0011", "Home")], stack_screens=["0006"])


def test_orphan_screen_fails_loud() -> None:
    spec = _tab_spec()
    with pytest.raises(NavigationSpecError, match="0099"):
        tab_owners(spec, derive_tabs(spec), stack_screens=["0099"])


def test_empty_explicit_tabs_on_tab_app_fall_back_to_heuristic() -> None:
    assert [t.screen_id for t in derive_tabs(_tab_spec(tabs=[]))] == ["0011", "0012", "0002"]


def test_empty_explicit_tabs_on_stack_app_mean_no_tabs() -> None:
    spec = _tab_spec(tabs=[])
    spec["navigation"]["type"] = "stack"
    assert derive_tabs(spec) == []
