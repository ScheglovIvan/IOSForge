"""Level 2 Phase C: the remote_api capability module (stub, rendering, Mac e2e)."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import feasibility, functional, swiftui_gen
from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp import swiftui_remote_api as remote
from iosforge.mvp.paths import RunPaths
from tests.test_compliance_ios import _first_iphone
from tests.test_feasibility import _spec_three_screens

ASK_SCREEN = """import SwiftUI

struct Screen0001View: View {
    @State private var question = ""
    @State private var answer = ""
    @State private var busy = false

    var body: some View {
        VStack(spacing: 16) {
            TextField("Ask anything", text: $question)
                .textFieldStyle(.roundedBorder)
            Button("Ask") {
                busy = true
                Task {
                    do {
                        answer = try await RemoteAPI.send(question)
                    } catch {
                        answer = error.localizedDescription
                    }
                    busy = false
                }
            }
            if busy { ProgressView() }
            Text(answer)
        }
        .padding()
    }
}
"""

CANNED_SCREEN = ASK_SCREEN.replace(
    "answer = try await RemoteAPI.send(question)",
    'answer = "IOSFORGE-STUB-REPLY: " + question',
)


def _spec(config: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = _spec_three_screens()
    spec["capabilities"] = [
        {
            "name": "ask_ai",
            "kind": "remote_api",
            "inputs": ["question"],
            "expected_behavior": "the answer from the API is rendered under the question",
            "screens": ["0001"],
            "tier": 2,
            "module": "remote_api",
            "config": config
            or {
                "base_url": "https://api.example.com",
                "path": "/v1/ask",
                "output_field": "data.0.text",
            },
        }
    ]
    return spec


def test_registry_routes_remote_api() -> None:
    caps.load_modules()
    assert "remote_api" in caps.REGISTRY and "`remote_api`" in feasibility.scope_prompt()
    assert remote.MOCK in functional.MOCKS


def test_stub_answers_post_and_get_at_the_output_path() -> None:
    with remote.stub_server("data.0.text") as (url, received):
        request = urllib.request.Request(
            url + "/v1/ask", data=json.dumps({"prompt": "hi"}).encode(), method="POST"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.loads(response.read()) == {"data": [{"text": "IOSFORGE-STUB-REPLY: hi"}]}
        with urllib.request.urlopen(url + "/v1/ask?q=yo", timeout=5) as response:
            assert json.loads(response.read())["data"][0]["text"].endswith("yo")
    assert [r["method"] for r in received] == ["POST", "GET"] and received[0]["input"] == "hi"


def test_service_renders_the_configured_endpoint(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    spec = _spec({"base_url": "https://x.io/", "path": "/q", "method": "get", "input_field": "q",
                  "output_field": "result", "headers": {"Accept": "application/json"}})  # fmt: skip
    swiftui_gen.write_scaffold(app, spec, app_name="Ask", bundle_id="dev.iosforge.ask")
    service = (app / "App/Capabilities/RemoteAPI.swift").read_text()
    assert 'static let defaultBaseURL = "https://x.io/"' in service
    assert 'static let method = "GET"' in service and 'static let inputField = "q"' in service
    assert '"Accept": "application/json",' in service
    assert 'Functional.value("REMOTE_API_URL")' in service
    project = (app / "project.yml").read_text()
    assert "NSAllowsLocalNetworking: true" in project and "FunctionalUITests" in project
    rules = swiftui_gen.module_rules(spec, app)
    assert any("RemoteAPI.send" in rule for rule in rules)


def test_functional_check_targets_the_capability_screen() -> None:
    ctx = caps.CapabilityContext(_spec(), caps.integ.Integrations(), _spec()["capabilities"][0])
    check = remote.DESCRIPTOR.functional_check(ctx)
    assert check is not None and check.screen_id == "0001" and check.mock == remote.MOCK
    assert check.env == {"IOSFORGE_REMOTE_API_OUTPUT": "data.0.text"}
    assert check.expect_events == ("remote_api.request", "remote_api.response")


def _run(tmp_path: Path, screen: str) -> dict[str, Any]:
    udid = _first_iphone()
    assert udid is not None
    spec = _spec()
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(spec))
    swiftui_gen.write_scaffold(
        paths.xcode_app, spec, app_name="Ask Demo", bundle_id="dev.iosforge.ask"
    )
    (paths.xcode_app / "App/Features/0001/Screen0001View.swift").write_text(screen)
    return functional.run(paths, udid=udid, spec=spec, timeout=1200)


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_screen_calling_the_api_draws_the_stub_answer(tmp_path: Path) -> None:
    report = _run(tmp_path, ASK_SCREEN)
    assert report["ok"], report
    assert report["checks"][0]["events"] == ["remote_api.request", "remote_api.response"]


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_canned_answer_without_the_api_fails(tmp_path: Path) -> None:
    check = _run(tmp_path, CANNED_SCREEN)["checks"][0]
    assert not check["passed"] and check["ui_passed"] and not check["infra_error"]
    assert check["missing_events"] == ["remote_api.request", "remote_api.response"]


def test_config_is_inferred_from_the_analysis_shapes(tmp_path: Path) -> None:
    config = {
        "base_url": "<backend proxy - not observed>",
        "path": "/v1/translate",
        "request": {"text": "string", "source_lang": "BCP-47", "target_lang": "BCP-47"},
        "response": {"translation": "string", "detected_source_lang": "string?"},
    }
    app = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(app, _spec(config), app_name="T", bundle_id="dev.iosforge.t")
    service = (app / "App/Capabilities/RemoteAPI.swift").read_text()
    assert f'static let defaultBaseURL = "{remote.UNCONFIGURED_URL}"' in service
    assert 'static let inputField = "text"' in service
    assert 'static let outputField = "translation"' in service
    assert "needs its service endpoint configured" in service


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return self.objects[key]

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data


def test_approved_routing_survives_the_codegen_scope(tmp_path: Path) -> None:
    from iosforge.mvp.scope_models import FeasibilityFinding, FeasibilityReport
    from tests.test_feasibility import _scope

    spec = _spec()
    for capability in spec["capabilities"]:
        capability.pop("tier")
        capability.pop("module")
    scope = _scope()
    scope.feasibility = FeasibilityReport(
        overall_verdict="partial",
        summary="",
        findings=[
            FeasibilityFinding(
                capability="ask_ai", verdict="partial", note="", tier=2, module="remote_api"
            )
        ],
    )
    storage = _Storage()
    key = "jobs/j1/app_spec/app_spec.json"
    storage.objects[key] = json.dumps(spec).encode()
    assert feasibility.save_routed_spec(storage, "j1", scope)  # type: ignore[arg-type]
    stored = json.loads(storage.objects[key])
    assert stored["capabilities"][0]["tier"] == 2 and len(stored["screens"]) == 3

    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(stored))
    swiftui_gen.scope_to(paths, ["0000", "0001"])
    pruned = json.loads(paths.app_spec_json.read_text())
    assert [key for key, _ in caps.routed_modules(pruned)] == ["remote_api"]
    keys = [s.descriptor.key for s in caps.select(pruned, caps.integ.Integrations())]
    assert "remote_api" in keys


def test_spec_without_capabilities_is_left_alone() -> None:
    from tests.test_feasibility import _scope

    storage = _Storage()
    storage.objects["jobs/j2/app_spec/app_spec.json"] = json.dumps(_spec_three_screens()).encode()
    assert not feasibility.save_routed_spec(storage, "j2", _scope())  # type: ignore[arg-type]
