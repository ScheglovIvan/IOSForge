"""Tests for the MVP task-runner codegen (Stage D, generate_from_tasks).

No real Claude CLI: subprocess.run is replaced with a fake that inspects the
per-task prompt and drops the files that task would create into the workspace
flutter_app/, recording the order of task ids it was invoked with.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import claude_gen
from iosforge.mvp.paths import RunPaths

_TASKS = {
    "tasks": [
        {
            "id": "t-scaffold",
            "type": "scaffold",
            "title": "Project + theme",
            "screens": [],
            "deps": [],
        },
        {
            "id": "t-add",
            "type": "screen",
            "title": "Add screen",
            "screens": ["0001"],
            "deps": ["t-scaffold", "t-home"],
        },
        {
            "id": "t-home",
            "type": "screen",
            "title": "Home screen",
            "screens": ["0000"],
            "deps": ["t-scaffold"],
        },
    ]
}

_APP_SPEC = {
    "app_name": "Todo",
    "package": "com.example.todo",
    "screens": [{"id": "0000", "name": "Home"}, {"id": "0001", "name": "Add"}],
    "flows": [],
    "data_model": [],
    "design": {"primary_color": "#3366FF", "theme": "light"},
}


def _field(prompt: str, name: str) -> str:
    m = re.search(rf"- {name}: (\S+)", prompt)
    assert m is not None, f"prompt missing {name}"
    return m.group(1)


def _make_paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path)
    (rp.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (rp.screens_dir / "0001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.screens_json.write_text(json.dumps({"package": "com.example.todo", "screens": []}))
    rp.app_spec_json.write_text(json.dumps(_APP_SPEC))
    rp.tasks_json.write_text(json.dumps(_TASKS))
    return rp


def _fake_runner(order: list[str], *, scaffold_writes: bool = True) -> Any:
    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        prompt = cmd[2]
        tid = _field(prompt, "id")
        ttype = _field(prompt, "type")
        order.append(tid)
        app = cwd / "flutter_app"
        if ttype == "scaffold" and scaffold_writes:
            (app / "lib").mkdir(parents=True, exist_ok=True)
            (app / "pubspec.yaml").write_text("name: todo\n")
            (app / "lib" / "main.dart").write_text("void main() {}\n")
        elif ttype == "screen":
            (app / "lib" / "screens").mkdir(parents=True, exist_ok=True)
            (app / "lib" / "screens" / f"{tid}.dart").write_text("// screen\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


def test_generate_from_tasks_runs_in_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner(order))

    out = claude_gen.generate_from_tasks(rp)

    assert out == rp.flutter_app
    assert not (rp.claude_ws / "flutter_app").exists()
    assert (out / "pubspec.yaml").exists()
    assert (out / "lib" / "main.dart").exists()
    assert (out / "lib" / "screens" / "t-home.dart").exists()
    assert (out / "lib" / "screens" / "t-add.dart").exists()

    assert len(order) == 3
    deps = {str(t["id"]): {str(d) for d in t["deps"]} for t in _TASKS["tasks"]}
    for tid, required in deps.items():
        idx = order.index(tid)
        for dep in required:
            assert order.index(dep) < idx, f"{dep} must precede {tid}"


def test_generate_from_tasks_stages_all_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner([]))

    claude_gen.generate_from_tasks(rp)

    assert (rp.claude_ws / "app_spec.json").exists()
    assert (rp.claude_ws / "tasks.json").exists()
    assert (rp.claude_ws / "screens" / "0000.png").exists()
    assert not (rp.claude_ws / "PROMPT.md").exists()


def test_generate_from_tasks_rejects_malformed_tasks_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    rp.tasks_json.write_text(json.dumps({"not_tasks": []}))
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner([]))

    with pytest.raises(RuntimeError, match="empty or missing 'tasks'"):
        claude_gen.generate_from_tasks(rp)


def test_generate_from_tasks_raises_when_scaffold_produces_no_pubspec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner([], scaffold_writes=False))

    with pytest.raises(RuntimeError, match="scaffold"):
        claude_gen.generate_from_tasks(rp)


def test_generate_from_tasks_requires_artifacts(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    with pytest.raises(RuntimeError, match="app_spec.json missing"):
        claude_gen.generate_from_tasks(rp)


def test_augment_prompt_targets_missing_pieces() -> None:
    p = claude_gen._AUGMENT_PROMPT
    assert "GAP ANALYSIS" in p
    assert "add only the missing pieces" in p.lower()
    assert "apphud_config.json" in p
    assert "Apphud.start" in p
    assert "do NOT rewrite" in p or "do NOT change the divergent design" in p


def test_augment_adds_over_existing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rp = RunPaths.create(tmp_path)
    rp.screens_json.write_text(json.dumps({"package": "com.x", "screens": []}))
    rp.app_spec_json.write_text(json.dumps({"app_name": "X", "screens": [{"id": "0000"}]}))
    rp.apphud_config_json.write_text(json.dumps({"sdk_key": "app_K", "mode": "sandbox"}))
    # an existing generated app to augment
    (rp.flutter_app / "lib").mkdir(parents=True, exist_ok=True)
    (rp.flutter_app / "pubspec.yaml").write_text("name: app")
    (rp.flutter_app / "lib" / "main.dart").write_text("void main() {}")

    def _fake_run(workspace: Path, prompt: str, *, timeout: int, tlog: object) -> int:
        assert "GAP ANALYSIS" in prompt  # augment prompt, not from-scratch
        assert (workspace / "apphud_config.json").exists()  # apphud_config staged
        ws_app = workspace / "flutter_app"
        (ws_app / "lib" / "paywall.dart").write_text("// added")
        return 0

    monkeypatch.setattr(claude_gen, "run_task", _fake_run)
    out = claude_gen.augment(rp)
    assert out == rp.flutter_app
    assert (out / "lib" / "paywall.dart").exists()  # incremental addition kept
    assert (out / "lib" / "main.dart").exists()  # existing preserved


def test_prompts_update_stale_sdk_keys_instead_of_skipping() -> None:
    # the SDK key is baked into the app as a constant and rotates per app; an
    # "add only if not already configured" prompt leaves the OLD key in place when the
    # operator pastes a new one, silently reporting to the wrong Apphud/Tenjin account.
    augment = claude_gen._AUGMENT_PROMPT
    assert "ALREADY wired" in augment
    assert "UPDATE" in augment
    assert "does not already configure" not in augment  # the skip-wording is the bug

    task = claude_gen._task_prompt(
        {"id": "t", "type": "scaffold", "title": "Scaffold", "screens": []}
    )
    assert "ALREADY WIRED" in task
    assert "single source of truth" in task


def test_prompts_match_vendor_sdk_requirements() -> None:
    # gaps found by auditing our wiring against the Apphud / Tenjin docs — each of these
    # silently breaks a feature the operator expects to work.
    task = claude_gen._task_prompt(
        {"id": "t", "type": "scaffold", "title": "Scaffold", "screens": []}
    )
    # Apphud: without paywallShown there are NO paywall analytics and no A/B tests
    assert "Apphud.paywallShown" in task
    # trials must be gated on real eligibility, not advertised blindly
    assert "checkEligibilityForIntroductoryOffer" in task
    # Tenjin requires initialize+connect on every launch, not just the first
    assert "EVERY app" in task
    # Apphud Connections is a paid feature, so revenue is reported app-side to Tenjin —
    # exactly once per purchase, or ROAS per creative is inflated
    assert "subscriptionWithStoreKit" in task
    assert "EXACTLY ONCE per successful purchase" in task
    # codegen picked the deprecated init(apiKey:) once; the build pins a newer plugin
    # where that can be gone, so the correct method must be named explicitly
    assert "TenjinSDK.instance.initialize(sdkKey:" in task
    assert "DEPRECATED" in task
    assert "Apphud.paywallShown" in claude_gen._AUGMENT_PROMPT


def test_task_prompt_includes_attribution_wiring() -> None:
    prompt = claude_gen._task_prompt(
        {"id": "t", "type": "scaffold", "title": "Scaffold", "screens": []}
    )
    assert "attribution_config.json" in prompt
    assert "tenjin_plugin" in prompt
    assert "TenjinSDK.instance.connect" in prompt
    assert "requestTrackingAuthorization" in prompt
    # the IDFA must reach Apphud or purchases never tie back to a campaign
    assert "setAdvertisingIdentifier" in prompt
    # traffic sources live in the dashboard — never baked into the binary
    assert "NEVER hardcode an ad network" in prompt


def test_task_prompt_includes_apphud_wiring() -> None:
    prompt = claude_gen._task_prompt(
        {"id": "t", "type": "scaffold", "title": "Scaffold", "screens": []}
    )
    assert "apphud_config.json" in prompt
    assert "apphud" in prompt
    assert "Apphud.start" in prompt
    assert "hasPremiumAccess" in prompt


def test_content_divergence_toggle_in_screen_prompt() -> None:
    task = {"id": "t", "type": "screen", "title": "Home", "screens": ["0000"]}
    on = claude_gen._task_prompt(task, True, True)
    off = claude_gen._task_prompt(task, True, False)
    assert "ANTI-CLONE CONTENT" in on and "paraphrase" in on.lower()
    assert "ANTI-CLONE CONTENT" not in off
    assert "Keep the original icons, images, text/copy and media." in off


def test_task_prompt_component_library_branch() -> None:
    prompt = claude_gen._task_prompt(
        {"id": "t-lib", "type": "component_library", "title": "Design system", "screens": []}
    )
    assert "lib/ui/components/" in prompt
    assert "manifest.json" in prompt
    assert "design_tokens" in prompt


def test_task_prompt_screen_composes_from_components_layout_only() -> None:
    prompt = claude_gen._task_prompt(
        {"id": "t-home", "type": "screen", "title": "Home", "screens": ["0000"]}
    )
    assert "lib/ui/components/" in prompt
    assert "lib/features/" in prompt
    assert "ONLY for LAYOUT" in prompt
    assert "gradient" in prompt.lower()


def test_task_prompt_scaffold_uses_divergent_design() -> None:
    prompt = claude_gen._task_prompt(
        {"id": "t-scaffold", "type": "scaffold", "title": "Scaffold", "screens": []}
    )
    assert "DIVERGENT" in prompt
    assert "google_fonts" in prompt
    assert "gradient" in prompt.lower()
    # content divergence (default on): paraphrase copy, restyle icons, drop branding
    assert "ANTI-CLONE CONTENT" in prompt
    assert "paraphrase" in prompt.lower()
    # nav 20% = IA-only: presentation may change, reachability must not
    assert "NAVIGATION PRESENTATION" in prompt
    assert "MUST NOT change reachability" in prompt
    # parallel-safe wiring: routes pre-registered to per-feature stubs
    assert "PARALLEL-SAFE WIRING" in prompt
