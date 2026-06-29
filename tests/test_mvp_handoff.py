"""Tests for the enterprise handoff bundle renderers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from iosforge.mvp import handoff


def _spec(*, backend: bool = False) -> dict[str, Any]:
    return {
        "spec_version": "3.0",
        "app_name": "Todo",
        "app_type": "productivity",
        "one_liner": "A todo app",
        "description": "Manage tasks",
        "how_it_works": "Add and complete tasks",
        "provenance": {"generator": "iosforge-mvp-analyze/v3"},
        "screens": [{"id": "0000", "name": "Home", "purpose": "list"}],
        "requirements": [
            {
                "id": "REQ-add",
                "type": "event_driven",
                "text": "When the user taps Add, the system shall open the Add screen.",
                "priority": "must",
                "screens": ["0000"],
                "acceptance": ["Given Home", "When Add tapped", "Then Add screen shown"],
                "source": "observed",
            },
            {
                "id": "REQ-launch",
                "type": "ubiquitous",
                "text": "The system shall show the home screen on launch.",
                "priority": "should",
                "screens": ["0000"],
                "acceptance": [],
                "source": "inferred",
            },
        ],
        "design_tokens": {
            "color": {"primary": {"$value": "#3366FF", "$type": "color", "$description": "brand"}},
            "dimension": {"radius_md": {"$value": "8px", "$type": "dimension"}},
            "dark_mode": True,
            "ios_adaptation": ["cupertino nav"],
        },
        "content": {
            "data_model": [{"entity": "Task", "fields": ["title", "done"], "relations": []}]
        },
        "backend": {
            "backend_needed": backend,
            "apis": ["GET /tasks", "POST /tasks"] if backend else [],
        },
    }


def test_build_handoff_writes_core_artifacts(tmp_path: Path) -> None:
    written = handoff.build_handoff(_spec(), tmp_path)
    names = {p.name for p in written}
    assert {"requirements.md", "design.md", "tokens.json", "traceability.md", "README.md"} <= names
    assert (tmp_path / "features" / "acceptance.feature").exists()

    reqs = (tmp_path / "requirements.md").read_text()
    assert "REQ-add" in reqs and "Priority: must" in reqs

    feature = (tmp_path / "features" / "acceptance.feature").read_text()
    assert "Feature:" in feature
    assert "@REQ-add" in feature and "@screen-0000" in feature
    assert "Scenario:" in feature
    assert "Given Home" in feature  # acceptance kept as a Gherkin step
    assert "    * The system shall show the home screen" in feature  # generic step fallback


def test_tokens_json_is_w3c_and_separates_meta(tmp_path: Path) -> None:
    handoff.build_handoff(_spec(), tmp_path)
    tokens = json.loads((tmp_path / "tokens.json").read_text())
    assert tokens["color"]["primary"]["$value"] == "#3366FF"
    assert tokens["color"]["primary"]["$type"] == "color"
    # dark_mode / ios_adaptation are not W3C tokens -> moved out of the token tree
    assert "dark_mode" not in tokens
    assert tokens["$extensions"]["com.iosforge.meta"]["dark_mode"] is True


def test_traceability_lists_every_requirement(tmp_path: Path) -> None:
    handoff.build_handoff(_spec(), tmp_path)
    matrix = (tmp_path / "traceability.md").read_text()
    assert "REQ-add" in matrix and "REQ-launch" in matrix
    assert matrix.count("| REQ-") == 2


def test_openapi_only_when_backend_needed(tmp_path: Path) -> None:
    handoff.build_handoff(_spec(backend=False), tmp_path)
    assert not (tmp_path / "openapi.yaml").exists()

    out = tmp_path / "with_backend"
    handoff.build_handoff(_spec(backend=True), out)
    openapi_path = out / "openapi.yaml"
    assert openapi_path.exists()
    doc = yaml.safe_load(openapi_path.read_text())
    assert doc["openapi"].startswith("3.")
    assert "/tasks" in doc["paths"]
    assert "Task" in doc["components"]["schemas"]
