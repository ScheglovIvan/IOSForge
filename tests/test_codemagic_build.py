"""CodeMagic build helpers: summarize, artifacts, logs, workflow parsing."""

from __future__ import annotations

from iosforge.mvp import codemagic_build as cb


def test_first_workflow_id() -> None:
    yaml = "workflows:\n  ios-native-quick-start:\n    name: iOS\n    scripts: []\n"
    assert cb.first_workflow_id(yaml) == "ios-native-quick-start"
    assert cb.first_workflow_id("nope: 1\n") is None


def test_summarize_maps_fields() -> None:
    build = {
        "_id": "b1",
        "index": 7,
        "workflowId": "wf",
        "status": "finished",
        "branch": "main",
        "startedAt": "2026-07-07T10:00:00Z",
        "finishedAt": "2026-07-07T10:20:00Z",
        "message": None,
        "artefacts": [
            {"name": "app.ipa", "type": "ipa", "url": "https://cm/x.ipa", "size": 1024, "md5": "a"}
        ],
    }
    s = cb.summarize(build)
    assert s["build_id"] == "b1"
    assert s["build_number"] == 7
    assert s["status"] == "finished"
    assert s["artifacts"][0]["name"] == "app.ipa"
    assert s["artifacts"][0]["url"] == "https://cm/x.ipa"


def test_build_logs_concatenates_actions_and_message() -> None:
    build = {
        "buildActions": [
            {"name": "pub get", "status": "finished", "logs": "Resolving..."},
            {"name": "build", "status": "failed", "logs": "error: X"},
        ],
        "message": "No matching profiles found",
    }
    logs = cb.build_logs(build)
    assert "pub get" in logs and "Resolving..." in logs
    assert "error: X" in logs
    assert "No matching profiles found" in logs


def test_terminal_statuses() -> None:
    assert "finished" in cb.TERMINAL and "failed" in cb.TERMINAL
    assert "building" not in cb.TERMINAL
