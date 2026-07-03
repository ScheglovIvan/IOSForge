"""Tests for the App Spec v3 formal contract (schema + cross-ref + coverage)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from iosforge.mvp import spec_contract
from iosforge.mvp.spec_contract import SpecValidationError, validate_spec


def _valid_spec() -> dict[str, Any]:
    return {
        "spec_version": spec_contract.SPEC_VERSION,
        "provenance": spec_contract.build_provenance(
            generator="iosforge/test",
            generated_at="2026-06-29T00:00:00Z",
            source_crawl_sha256="abc123",
            screen_count=2,
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
        "screens": [
            {"id": "0000", "name": "Home", "purpose": "list", "navigates_to": ["0001"]},
            {"id": "0001", "name": "Add", "purpose": "create", "navigates_to": ["0000"]},
        ],
        "requirements": [
            {
                "id": "REQ-add-task",
                "type": "event_driven",
                "text": "When the user taps Add, the system shall open the Add screen.",
                "priority": "must",
                "screens": ["0000", "0001"],
                "acceptance": ["Given Home, when Add tapped, then Add screen shown"],
                "source": "observed",
            }
        ],
        "design_tokens": {
            "color": {"primary": {"$value": "#3366FF", "$type": "color"}},
        },
        "navigation": {
            "type": "stack",
            "deep_links": [],
            "map": [{"from": "0000", "to": "0001", "via": "Add"}],
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


def test_valid_spec_passes() -> None:
    spec = validate_spec(_valid_spec())
    assert spec["app_name"] == "Todo"


def test_rejects_non_object() -> None:
    with pytest.raises(SpecValidationError, match="JSON object"):
        validate_spec(["not", "an", "object"])


def test_rejects_missing_required_section() -> None:
    bad = _valid_spec()
    del bad["monetization"]
    with pytest.raises(SpecValidationError, match="schema violation"):
        validate_spec(bad)


def test_rejects_unknown_app_type() -> None:
    bad = _valid_spec()
    bad["app_type"] = "spaceship"
    with pytest.raises(SpecValidationError, match="app_type"):
        validate_spec(bad)


def test_rejects_bad_requirement_id() -> None:
    bad = _valid_spec()
    bad["requirements"][0]["id"] = "add-task"
    with pytest.raises(SpecValidationError, match="requirements"):
        validate_spec(bad)


def test_rejects_wrong_spec_version() -> None:
    bad = _valid_spec()
    bad["spec_version"] = "2.0"
    with pytest.raises(SpecValidationError, match="spec_version"):
        validate_spec(bad)


def test_rejects_dangling_screen_navigation() -> None:
    bad = _valid_spec()
    bad["screens"][0]["navigates_to"] = ["9999"]
    with pytest.raises(SpecValidationError, match="dangling references"):
        validate_spec(bad)


def test_rejects_dangling_requirement_screen() -> None:
    bad = _valid_spec()
    bad["requirements"][0]["screens"] = ["does-not-exist"]
    with pytest.raises(SpecValidationError, match="dangling references"):
        validate_spec(bad)


def test_uncovered_screens_reports_gap() -> None:
    spec = _valid_spec()
    uncovered = spec_contract.uncovered_screens(spec, {"0000", "0001", "0002"})
    assert uncovered == ["0002"]
    assert spec_contract.uncovered_screens(spec, {"0000", "0001"}) == []


def test_validate_returns_same_payload() -> None:
    original = _valid_spec()
    returned = validate_spec(copy.deepcopy(original))
    assert returned["requirements"][0]["id"] == "REQ-add-task"


def test_structured_ad_monetization_validates() -> None:
    spec = _valid_spec()
    spec["monetization"] = {
        "model": "mixed",
        "ads": ["rewarded video for coins"],
        "ad_networks": [
            {
                "name": "AdMob",
                "confidence": "low",
                "evidence": "install-now card",
                "source": "video",
            }
        ],
        "ad_placements": [
            {
                "format": "rewarded",
                "trigger": "watch-for-coins tap",
                "screen_context": "Rewards",
                "frequency": "on demand",
                "frames": ["0048.png"],
            }
        ],
    }
    assert validate_spec(spec)["monetization"]["ad_placements"][0]["format"] == "rewarded"


def test_legacy_flat_ads_still_validates() -> None:
    spec = _valid_spec()
    spec["monetization"] = {"model": "ads", "ads": ["banner", "interstitial"]}
    assert validate_spec(spec)["monetization"]["ads"] == ["banner", "interstitial"]


def test_rejects_bad_ad_format() -> None:
    bad = _valid_spec()
    bad["monetization"] = {"model": "ads", "ad_placements": [{"format": "popup"}]}
    with pytest.raises(SpecValidationError, match="schema violation"):
        validate_spec(bad)
