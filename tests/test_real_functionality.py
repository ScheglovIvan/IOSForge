"""Codegen prompts must mandate real functionality, not decorative stubs."""

from __future__ import annotations

from iosforge.mvp import claude_gen


def _all_prompts() -> dict[str, str]:
    return {
        "task_scaffold": claude_gen._task_prompt(
            {"id": "home", "type": "scaffold", "title": "Scaffold"}
        ),
        "task_screen": claude_gen._task_prompt(
            {"id": "dash", "type": "screen", "title": "Dashboard", "screens": ["dash"]}
        ),
        "augment": claude_gen._AUGMENT_PROMPT,
        "rework": claude_gen._rework_prompt("first tab reopens onboarding"),
    }


def test_prompts_forbid_stubs_and_demand_real_apis() -> None:
    for name, prompt in _all_prompts().items():
        assert "REAL FUNCTIONALITY" in prompt, name
        assert "NO DECORATIVE STUBS" in prompt, name
        assert "permission_handler" in prompt, name
        assert "apple_maps_flutter" in prompt, name
        assert "ios_permissions.json" in prompt, name
        assert "CAPABILITIES.md" in prompt, name


def test_prompts_have_no_unrendered_placeholders() -> None:
    for name, prompt in _all_prompts().items():
        assert "{_REAL_FUNCTIONALITY_NOTE}" not in prompt, name
        assert "{{" not in prompt, name


def test_note_lists_device_data_capabilities() -> None:
    note = claude_gen._REAL_FUNCTIONALITY_NOTE
    for pkg in ("geolocator", "battery_plus", "connectivity_plus", "device_info_plus"):
        assert pkg in note, pkg
    assert "MusicKit" in note  # Apple Music limitation is documented, not faked
