"""Tests for the MVP analysis (Stage B) and decomposition (Stage C) steps.

No real Claude CLI: subprocess.run is replaced with a fake that writes a
canned artifact into the workspace, mirroring how the live CLI would behave.
Covers happy paths plus tasks.json validation (acyclic / deps resolve).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import analyze
from iosforge.mvp.paths import RunPaths

_SCREENS_JSON = {
    "package": "com.example.todo",
    "screen_count": 3,
    "screens": [
        {
            "id": "0000",
            "screenshot": "screens/0000.png",
            "activity": "com.example.todo/.MainActivity",
            "signature": "aaaa1111",
            "elements": [
                {
                    "text": "Add",
                    "resource_id": "com.example.todo:id/add",
                    "bounds": "[0,0][100,50]",
                }
            ],
            "from": None,
            "tapped": {"resource_id": "com.example.todo:id/add", "text": "Add"},
        },
        {
            "id": "0001",
            "screenshot": "screens/0001.png",
            "activity": "com.example.todo/.AddActivity",
            "signature": "bbbb2222",
            "elements": [
                {
                    "text": "Save",
                    "resource_id": "com.example.todo:id/save",
                    "bounds": "[0,0][100,50]",
                }
            ],
            "from": "0000",
            "tapped": {"resource_id": "com.example.todo:id/save", "text": "Save"},
        },
        {
            "id": "0002",
            "screenshot": "screens/0002.png",
            "activity": "com.example.todo/.MainActivity",
            "signature": "aaaa1111",
            "elements": [],
            "from": "0001",
        },
    ],
}

_VALID_APP_SPEC = {
    "app_name": "Todo",
    "package": "com.example.todo",
    "screens": [
        {
            "id": "0000",
            "name": "Home",
            "purpose": "list todos",
            "screenshot": "screens/0000.png",
            "components": [{"type": "appbar"}, {"type": "list"}],
            "layout_notes": "fab bottom-right",
            "navigates_to": ["0001"],
        },
        {
            "id": "0001",
            "name": "Add",
            "purpose": "create todo",
            "screenshot": "screens/0001.png",
            "components": [{"type": "textfield"}, {"type": "button"}],
            "layout_notes": "form",
            "navigates_to": ["0000"],
        },
    ],
    "app_type": "productivity",
    "one_liner": "A todo app",
    "description": "Manage tasks",
    "how_it_works": "Add, edit and complete tasks",
    "target_audience": "everyone",
    "platforms": ["ios"],
    "market_research": {
        "similar_apps": [{"name": "Things", "notes": "premium todo"}],
        "category_conventions": ["swipe to complete"],
        "monetization_norms": ["one-time purchase"],
        "sources": [],
    },
    "business_logic": {
        "summary": "task tracking",
        "domain_rules": ["a task is done or not"],
        "workflows": [{"name": "create", "steps": ["tap add", "enter text", "save"]}],
        "state_machine": [],
    },
    "requirements": [
        {
            "id": "REQ-add-task",
            "type": "event_driven",
            "text": "When the user taps Add, the system shall open the Add screen.",
            "priority": "must",
            "screens": ["0000", "0001"],
            "acceptance": ["Given Home, when Add tapped, then Add screen is shown"],
            "source": "observed",
        }
    ],
    "design_tokens": {
        "color": {"primary": {"$value": "#3366FF", "$type": "color"}},
        "dark_mode": True,
        "ios_adaptation": ["use cupertino nav"],
    },
    "navigation": {"type": "stack", "map": [], "deep_links": []},
    "content": {
        "data_model": [{"entity": "Task", "fields": ["title", "done"], "relations": []}],
        "content_inventory": [],
        "content_to_seed": [],
        "persistence": "local",
    },
    "monetization": {
        "model": "free",
        "paywalls": [],
        "packages": [],
        "ads": [],
        "free_vs_premium": [],
    },
    "backend": {
        "backend_needed": False,
        "why": "local only",
        "admin_panel_needed": False,
        "admin_scope": [],
        "auth": {"required": False, "methods": []},
        "user_roles": [],
        "apis": [],
        "push_notifications": False,
        "cloud_sync": False,
    },
    "permissions": [],
    "integrations": [],
    "cross_cutting": {
        "localization": ["en"],
        "onboarding": "none",
        "analytics_events": [],
        "accessibility": [],
        "legal": [],
    },
    "analysis_quality": {
        "assumptions": ["inferred swipe-to-complete"],
        "open_questions": [],
        "coverage_gaps": [],
        "confidence": {"overall": "medium", "by_section": []},
    },
    "acceptance_criteria": ["can add and complete a task"],
}

_VALID_TASKS = {
    "tasks": [
        {
            "id": "t-scaffold",
            "type": "scaffold",
            "title": "Project + theme",
            "screens": [],
            "deps": [],
        },
        {
            "id": "t-home",
            "type": "screen",
            "title": "Home screen",
            "screens": ["0000"],
            "deps": ["t-scaffold"],
        },
        {
            "id": "t-add",
            "type": "screen",
            "title": "Add screen",
            "screens": ["0001"],
            "deps": ["t-scaffold", "t-home"],
        },
    ]
}


def _make_paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path)
    (rp.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (rp.screens_dir / "0001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (rp.screens_dir / "0002.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.screens_json.write_text(json.dumps(_SCREENS_JSON))
    return rp


def _fake_claude(filename: str, payload: object) -> Any:
    """Return a subprocess.run stand-in that drops ``filename`` into cwd."""

    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        (cwd / filename).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


def test_analyze_writes_and_validates_app_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    monkeypatch.setattr(analyze.subprocess, "run", _fake_claude("app_spec.json", _VALID_APP_SPEC))

    out = analyze.analyze(rp)

    assert out == rp.app_spec_json
    assert out.exists()
    spec = json.loads(out.read_text())
    assert spec["app_name"] == "Todo"
    assert len(spec["screens"]) == 2
    assert (rp.claude_ws / "screens" / "0000.png").exists()
    assert "app_spec.json" in (rp.claude_ws / "ANALYZE_PROMPT.md").read_text()
    assert rp.spec_md.exists()
    md = rp.spec_md.read_text()
    assert "App Specification" in md
    assert "Monetization" in md
    assert "Requirements (EARS)" in md
    # provenance + spec_version are injected by analyze(), not by the model
    assert spec["spec_version"] == "3.0"
    assert spec["provenance"]["source_crawl_sha256"]
    assert spec["provenance"]["screen_count"] == 3
    assert spec["requirements"][0]["id"] == "REQ-add-task"


def test_analyze_rejects_empty_screens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rp = _make_paths(tmp_path)
    bad = {"app_name": "X", "package": "p", "screens": []}
    monkeypatch.setattr(analyze.subprocess, "run", _fake_claude("app_spec.json", bad))

    with pytest.raises(RuntimeError, match="schema violation"):
        analyze.analyze(rp)


def test_analyze_rejects_incomplete_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rp = _make_paths(tmp_path)
    incomplete = {"app_name": "X", "package": "p", "screens": [{"id": "0000"}]}
    monkeypatch.setattr(analyze.subprocess, "run", _fake_claude("app_spec.json", incomplete))

    with pytest.raises(RuntimeError, match="schema violation"):
        analyze.analyze(rp)


def test_decompose_validates_tasks_and_deps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    rp.app_spec_json.write_text(json.dumps(_VALID_APP_SPEC))
    monkeypatch.setattr(analyze.subprocess, "run", _fake_claude("tasks.json", _VALID_TASKS))

    out = analyze.decompose(rp)

    assert out == rp.tasks_json
    tasks = json.loads(out.read_text())["tasks"]
    assert tasks[0]["type"] == "scaffold"
    assert tasks[0]["deps"] == []
    assert (rp.claude_ws / "app_spec.json").exists()


def test_decompose_requires_app_spec(tmp_path: Path) -> None:
    rp = _make_paths(tmp_path)
    with pytest.raises(RuntimeError, match="app_spec.json missing"):
        analyze.decompose(rp)


def test_decompose_rejects_dependency_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    rp.app_spec_json.write_text(json.dumps(_VALID_APP_SPEC))
    cyclic = {
        "tasks": [
            {"id": "a", "type": "scaffold", "title": "A", "screens": [], "deps": ["b"]},
            {"id": "b", "type": "screen", "title": "B", "screens": [], "deps": ["a"]},
        ]
    }
    monkeypatch.setattr(analyze.subprocess, "run", _fake_claude("tasks.json", cyclic))

    with pytest.raises(RuntimeError, match="cycle"):
        analyze.decompose(rp)


def test_decompose_rejects_unknown_dep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rp = _make_paths(tmp_path)
    rp.app_spec_json.write_text(json.dumps(_VALID_APP_SPEC))
    bad = {
        "tasks": [
            {"id": "t-scaffold", "type": "scaffold", "title": "S", "screens": [], "deps": []},
            {"id": "t-home", "type": "screen", "title": "H", "screens": [], "deps": ["nope"]},
        ]
    }
    monkeypatch.setattr(analyze.subprocess, "run", _fake_claude("tasks.json", bad))

    with pytest.raises(RuntimeError, match="unknown ids"):
        analyze.decompose(rp)
