"""Level 2 Phase A: capabilities in the App Spec, feasibility routing, module registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import capability_registry, feasibility, spec_contract, swiftui_gen
from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp import swiftui_integrations as integ
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.scope_models import FeasibilityFinding, FeasibilityReport
from iosforge.mvp.swiftui_prompts import base_rules, screen_prompt
from iosforge.mvp.swiftui_scaffold import AppIdentity, build_plan, enforce_contract
from tests.test_feasibility import _scope, _spec_three_screens

CAPABILITY = {
    "name": "ask_ai",
    "kind": "remote_api",
    "inputs": ["prompt"],
    "expected_behavior": "the answer from the API is rendered under the prompt",
    "screens": ["0000", "0002"],
    "config": {"base_url": "https://api.example.com", "path": "/v1/ask", "method": "POST"},
}


def _spec(**extra: Any) -> dict[str, Any]:
    spec = _spec_three_screens()
    spec.update(extra)
    return spec


def test_spec_without_capabilities_stays_valid() -> None:
    assert "capabilities" not in spec_contract.validate_spec(_spec_three_screens())


def test_capabilities_and_launch_validate() -> None:
    spec = _spec(capabilities=[CAPABILITY])
    spec["navigation"]["launch"] = [
        {"screen_id": "0001", "presentation": "fullScreenCover", "condition": "first_launch"}
    ]
    assert spec_contract.validate_spec(spec)["capabilities"][0]["name"] == "ask_ai"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s["capabilities"][0].update(screens=["9999"]), "unknown screen '9999'"),
        (lambda s: s["capabilities"].append(dict(CAPABILITY)), "declared more than once"),
        (lambda s: s["capabilities"][0].update(kind="teleport"), "teleport"),
        (lambda s: s["capabilities"][0].update(tier=7), "7"),
        (
            lambda s: s["navigation"].update(
                launch=[{"screen_id": "x", "presentation": "sheet", "condition": "every_launch"}]
            ),
            "navigation.launch references unknown screen 'x'",
        ),
    ],
)
def test_capability_contract_errors(mutate: Any, message: str) -> None:
    spec = _spec(capabilities=[dict(CAPABILITY)])
    mutate(spec)
    with pytest.raises(spec_contract.SpecValidationError, match=message):
        spec_contract.validate_spec(spec)


def test_parse_proposal_coerces_tier_and_module() -> None:
    payload = {
        "screens": [{"screen_id": "0000", "include": True}],
        "feasibility": {
            "overall_verdict": "native",
            "findings": [
                {
                    "capability": "a",
                    "verdict": "native",
                    "tier": "2",
                    "module": "subscriptions_apphud",
                },
                {"capability": "b", "verdict": "partial", "tier": 9, "module": "made_up"},
            ],
        },
    }
    findings = feasibility._parse_proposal(payload, _spec_three_screens()).feasibility.findings
    assert (findings[0].tier, findings[0].module) == (2, "subscriptions_apphud")
    assert (findings[1].tier, findings[1].module) == (None, None)


def test_scope_prompt_lists_the_registry() -> None:
    prompt = feasibility.scope_prompt()
    assert "{modules}" not in prompt
    for key in capability_registry.MODULES:
        assert f"`{key}`" in prompt


def _routed_scope(*findings: FeasibilityFinding) -> Any:
    scope = _scope()
    scope.feasibility = FeasibilityReport(
        overall_verdict="native", summary="ok", findings=list(findings)
    )
    return scope


def test_apply_scope_prunes_and_stamps_capabilities(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path)
    dropped = {**CAPABILITY, "name": "onboarding_quiz", "screens": ["0002"]}
    spec = _spec(capabilities=[dict(CAPABILITY), dropped])
    spec["navigation"]["launch"] = [
        {"screen_id": "0002", "presentation": "sheet", "condition": "first_launch"},
        {"screen_id": "0001", "presentation": "fullScreenCover", "condition": "not_premium"},
    ]
    paths.app_spec_json.write_text(json.dumps(spec))
    finding = FeasibilityFinding(
        capability="ask_ai", verdict="native", note="", tier=2, module="subscriptions_apphud"
    )
    feasibility.apply_scope(paths, _routed_scope(finding))
    pruned = json.loads(paths.app_spec_json.read_text())
    assert [c["name"] for c in pruned["capabilities"]] == ["ask_ai"]
    assert pruned["capabilities"][0]["screens"] == ["0000"]
    assert pruned["capabilities"][0]["tier"] == 2
    assert pruned["capabilities"][0]["module"] == "subscriptions_apphud"
    assert [e["screen_id"] for e in pruned["navigation"]["launch"]] == ["0001"]


def test_tier_two_without_a_known_module_becomes_custom() -> None:
    capability = {**CAPABILITY, "tier": 2, "module": "not_in_registry"}
    routed = feasibility.route_capabilities([capability], _routed_scope(), {"0000"})
    assert (routed[0]["tier"], routed[0]["module"]) == (3, None)


def _module_files(ctx: caps.CapabilityContext) -> dict[str, str]:
    return {f"{caps.CAPABILITIES_DIR}/Echo.swift": "enum Echo { static func start() {} }\n"}


@pytest.fixture
def echo_module(monkeypatch: pytest.MonkeyPatch) -> caps.CapabilityDescriptor:
    monkeypatch.setitem(
        capability_registry.MODULES,
        "echo_test",
        capability_registry.ModuleInfo("echo_test", ("other",), "test module"),
    )
    descriptor = caps.CapabilityDescriptor(
        key="echo_test",
        directory=caps.CAPABILITIES_DIR,
        render=_module_files,
        startup="Echo.start()",
        screen_api_rule="Echo only through `Echo` (App/Capabilities/Echo.swift).",
        packages=lambda ctx: (caps.SwiftPackage("EchoKit", "https://example.com/echo", "1.0.0"),),
        prompters=lambda ctx: ("camera",),
    )
    monkeypatch.setitem(caps.REGISTRY, "echo_test", descriptor)
    return descriptor


def _routed_spec() -> dict[str, Any]:
    return _spec(capabilities=[{**CAPABILITY, "tier": 2, "module": "echo_test"}])


def test_core_modules_are_always_selected() -> None:
    keys = [s.descriptor.key for s in caps.select({}, integ.Integrations())]
    assert keys == ["subscriptions_apphud", "attribution_tenjin"]


def test_routed_module_flows_into_the_scaffold(
    tmp_path: Path, echo_module: caps.CapabilityDescriptor
) -> None:
    spec = _routed_spec()
    app = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(app, spec, app_name="Demo", bundle_id="com.ex.d")
    assert (app / caps.CAPABILITIES_DIR / "Echo.swift").read_text().startswith("enum Echo")
    project = (app / "project.yml").read_text()
    assert "  EchoKit:" in project and "      - package: EchoKit" in project
    assert "Echo.start()" in (app / "App/App.swift").read_text()
    assert "CameraPermission" in swiftui_gen.prompter_names(spec, app)
    rules = swiftui_gen.module_rules(spec, app)
    assert rules[-1].startswith("Echo only through")
    assert "Echo only through" in base_rules([], rules)
    plan = build_plan(spec)
    prompt = screen_prompt(plan.entries[0], plan, targets=[], prompters=[], modules=rules)
    assert "Echo only through" in prompt and "Subscriptions only through" in prompt


def test_enforce_contract_guards_module_files(
    tmp_path: Path, echo_module: caps.CapabilityDescriptor
) -> None:
    spec = _routed_spec()
    app = tmp_path / "xcode_app"
    identity = AppIdentity(app_name="Demo", bundle_id="com.ex.d")
    swiftui_gen.write_scaffold(app, spec, app_name="Demo", bundle_id="com.ex.d")
    (app / caps.CAPABILITIES_DIR / "Echo.swift").write_text("tampered")
    (app / caps.CAPABILITIES_DIR / "Rogue.swift").write_text("import EchoKit")
    report = enforce_contract(app, spec, identity)
    assert f"{caps.CAPABILITIES_DIR}/Echo.swift" in report.restored
    assert f"{caps.CAPABILITIES_DIR}/Rogue.swift" in report.removed
    assert not (app / caps.CAPABILITIES_DIR / "Rogue.swift").exists()


def test_register_requires_a_routable_key() -> None:
    with pytest.raises(ValueError, match="capability_registry"):
        caps.register(
            caps.CapabilityDescriptor(
                key="nowhere",
                directory=caps.CAPABILITIES_DIR,
                render=lambda c: {},
                screen_api_rule="",
            )
        )


def test_operator_confirms_or_overrides_routing() -> None:
    scope = _routed_scope(
        FeasibilityFinding(capability="ask_ai", verdict="native", note="", tier=3),
        FeasibilityFinding(
            capability="paywall", verdict="native", note="", tier=2, module="subscriptions_apphud"
        ),
    )
    feasibility.confirm_routing(
        scope,
        {"tier_0": "2", "module_0": "subscriptions_apphud", "tier_1": "", "module_1": "bogus"},
    )
    first, second = scope.feasibility.findings
    assert (first.tier, first.module) == (2, "subscriptions_apphud")
    assert (second.tier, second.module) == (2, None)


def test_scope_ui_shows_and_edits_routing() -> None:
    from tests.test_admin_templates_render import _NOW, _USER, _render

    scope = _routed_scope(
        FeasibilityFinding(
            capability="ask_ai", verdict="native", note="api", tier=2, module="subscriptions_apphud"
        ),
        FeasibilityFinding(capability="mirror", verdict="blocked", note="private", tier=4),
    )

    class _Job:
        id = "job-9"
        source_app_ref = "x"
        state = type("S", (), {"value": "needs_input"})()
        created_at = _NOW
        result_version = 1
        source_app_metadata: dict[str, Any] = {}

    html = _render(
        "job_detail.html", user=_USER, job=_Job(), gen=None, build=None, timeline=[],
        codegen_tasks=[], signing={"configured": False}, signed_ok=False, sign_error="",
        scope_status="proposed", scope=scope,
        capability_modules=list(capability_registry.MODULES),
    )  # fmt: skip
    assert 'name="tier_0"' in html and 'name="module_0"' in html
    assert '<option value="subscriptions_apphud" selected>' in html
    assert "not built" in html


def test_launch_screens_render_into_the_scaffold(tmp_path: Path) -> None:
    spec = _spec_three_screens()
    spec["navigation"]["launch"] = [
        {"screen_id": "0001", "presentation": "fullScreenCover", "condition": "first_launch"},
        {"screen_id": "9999", "presentation": "sheet", "condition": "every_launch"},
    ]
    app = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(app, spec, app_name="Demo", bundle_id="com.ex.d")
    screen_id = (app / "App/Navigation/ScreenID.swift").read_text()
    assert (
        "ScaffoldLaunchScreen(screen: .s0001, asSheet: false, condition: .firstLaunch),"
        in screen_id
    )
    assert "9999" not in screen_id
    assert "router.presentLaunch()" in (app / "App/App.swift").read_text()
    router = (app / "App/Navigation/Router.swift").read_text()
    assert 'let key = "iosforge.launch_shown.\\(entry.screen.rawValue)"' in router
    assert "        Task { @MainActor in self.presentLaunch() }\n" in router
    assert "    @MainActor\n    func finishOnboarding()" not in router


def test_no_launch_screens_keep_the_entry_point_plain(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(app, _spec_three_screens(), app_name="Demo", bundle_id="com.ex.d")
    from iosforge.mvp import swiftui_templates as tpl

    assert "presentLaunch" not in (app / "App/App.swift").read_text()
    assert (app / "App/Navigation/Router.swift").read_text() == tpl.ROUTER
    screen_id = (app / "App/Navigation/ScreenID.swift").read_text()
    assert "launchScreens" not in screen_id and "ScaffoldLaunchScreen" not in screen_id


def test_analysis_prompt_asks_for_capabilities_and_launch() -> None:
    from iosforge.mvp import analyze

    assert (
        "CAPABILITIES" in analyze.ANALYZE_PROMPT and '"capabilities": [' in analyze.ANALYZE_PROMPT
    )
    assert '"launch": [' in analyze.ANALYZE_PROMPT
    assert "capabilities" in analyze._SPEC_MD_ORDER


def test_launch_on_an_onboarding_screen_is_ignored(tmp_path: Path) -> None:
    spec = _spec_three_screens()
    spec["navigation"]["launch"] = [
        {"screen_id": "0002", "presentation": "sheet", "condition": "every_launch"}
    ]
    plan = build_plan(spec)
    onboarding = {e.screen_id for e in plan.entries if e.presentation == "onboarding"}
    from iosforge.mvp.swiftui_scaffold import launch_screens

    expected = [] if "0002" in onboarding else [spec["navigation"]["launch"][0]]
    assert launch_screens(spec, plan) == expected


def test_attribution_rule_only_with_tenjin() -> None:
    bare = caps.screen_rules(caps.select({}, integ.Integrations()))
    assert not any("Attribution" in rule for rule in bare)
    tenjin = caps.screen_rules(caps.select({}, integ.Integrations(tenjin_key="T")))
    assert any("never ask for tracking" in rule for rule in tenjin)


def test_every_routable_module_has_a_descriptor() -> None:
    assert set(capability_registry.MODULES) <= set(caps.REGISTRY)


def test_capability_without_screens_survives_scoping() -> None:
    capability = {**CAPABILITY, "screens": []}
    routed = feasibility.route_capabilities([capability], _routed_scope(), {"0000"})
    assert routed and routed[0]["screens"] == []


def test_old_scope_json_without_routing_still_loads() -> None:
    from iosforge.mvp.scope_models import ScopeDecision

    legacy = _scope().model_dump(mode="json")
    legacy["feasibility"]["findings"] = [
        {"capability": "widgets", "verdict": "partial", "note": "x", "screens": []}
    ]
    finding = ScopeDecision.model_validate(legacy).feasibility.findings[0]
    assert (finding.tier, finding.module) == (None, None)


def test_tier_coercion_accepts_float_strings() -> None:
    assert feasibility._coerce_tier("2.0") == 2 and feasibility._coerce_tier(None) is None
    assert feasibility._coerce_tier("inf") is None and feasibility._coerce_tier("2.7") is None
