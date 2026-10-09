"""``remote_api`` capability module: a light HTTP / AI API called from the app's screens.

Contract code ``App/Capabilities/RemoteAPI.swift`` wraps URLSession (no SDK): one
``RemoteAPI.send(_:)`` call posts the user's input to the endpoint from the
capability's ``config`` and returns the text found at the configured response path.
Screens only call that API (rule injected into every screen prompt). In functional mode
the base URL comes from ``IOSFORGE_REMOTE_API_URL`` (the local stub of :func:`stub_server`)
and every request / response is journaled.

``config`` keys (all optional): ``base_url``, ``path``, ``method`` (``POST`` | ``GET``),
``input_field`` (request field carrying the user's text), ``output_field`` (dotted path
into the JSON response, numeric parts index arrays) and ``headers`` (static, non-secret
headers). A real production endpoint / key is the operator's input; functional
verification proves the screen → module → stub integration only.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp.swiftui_functional import FunctionalCheck, Step, register_mock, swift_literal
from iosforge.mvp.swiftui_templates import DO_NOT_EDIT

KEY = "remote_api"
MOCK = "remote_api_stub"
STUB_REPLY = "IOSFORGE-STUB-REPLY"
PROBE_INPUT = "hello from iosforge"
ACTION_PATTERN = r"ask|send|generate|submit|translate|search|go|run|check|get|answer|create"
DEFAULT_INPUT_FIELD = "prompt"
DEFAULT_OUTPUT_FIELD = "answer"


_INPUT_NAMES = ("text", "prompt", "query", "q", "input", "message", "question", "content")
UNCONFIGURED_URL = "https://endpoint-not-configured.invalid"


def _first_field(shape: object, preferred: tuple[str, ...], fallback: str) -> str:
    if not isinstance(shape, dict) or not shape:
        return fallback
    keys = [str(k) for k in shape]
    for name in preferred:
        if name in keys:
            return name
    textual = [k for k in keys if "str" in str(shape.get(k, "")).lower()]
    return textual[0] if textual else keys[0]


def _base_url(value: object) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"https?://[^\s<>]+", text):
        return UNCONFIGURED_URL
    return text


def _config(ctx: caps.CapabilityContext) -> dict[str, Any]:
    """The endpoint the module calls, from the capability ``config`` (inferred where absent).

    Input / output fields fall back to the ``request`` / ``response`` shapes the analysis
    recorded; a placeholder or missing base URL becomes :data:`UNCONFIGURED_URL`.
    """
    config = ctx.config
    headers = config.get("headers")
    method = str(config.get("method") or "POST").upper()
    return {
        "base_url": _base_url(config.get("base_url")),
        "path": str(config.get("path") or "/"),
        "method": method if method in ("POST", "GET") else "POST",
        "input_field": str(
            config.get("input_field")
            or _first_field(config.get("request"), _INPUT_NAMES, DEFAULT_INPUT_FIELD)
        ),
        "output_field": str(
            config.get("output_field")
            or _first_field(config.get("response"), (), DEFAULT_OUTPUT_FIELD)
        ),
        "headers": {str(k): str(v) for k, v in headers.items()}
        if isinstance(headers, dict)
        else {},
    }


def render_service(ctx: caps.CapabilityContext) -> str:
    """``App/Capabilities/RemoteAPI.swift`` for the capability's endpoint."""
    cfg = _config(ctx)
    name = str(ctx.capability.get("name") or KEY)
    headers = ",\n".join(
        f"        {swift_literal(k)}: {swift_literal(v)}" for k, v in sorted(cfg["headers"].items())
    )
    header_literal = f"[\n{headers},\n    ]" if headers else "[:]"
    return f"""import Foundation

/// The app's remote API (`{name}`): posts the user's input, returns the answer text.
/// Screens call only `RemoteAPI.send(_:)`. {DO_NOT_EDIT}
enum RemoteAPI {{
    struct Failure: LocalizedError {{
        let message: String
        var errorDescription: String? {{ message }}
    }}

    static let defaultBaseURL = {swift_literal(cfg["base_url"])}
    static let path = {swift_literal(cfg["path"])}
    static let method = {swift_literal(cfg["method"])}
    static let inputField = {swift_literal(cfg["input_field"])}
    static let outputField = {swift_literal(cfg["output_field"])}
    static let headers: [String: String] = {header_literal}

    static var baseURL: String {{
        Functional.value("REMOTE_API_URL") ?? defaultBaseURL
    }}

    /// False until the operator configures the real endpoint (the analysis could not observe it).
    static var isConfigured: Bool {{
        baseURL != {swift_literal(UNCONFIGURED_URL)}
    }}

    /// Sends `input` and returns the text at `outputField` of the JSON reply.
    static func send(_ input: String) async throws -> String {{
        guard !Headless.isActive else {{ throw Failure(message: "offline in headless mode") }}
        guard isConfigured else {{
            throw Failure(message: "This feature needs its service endpoint configured.")
        }}
        let request = try makeRequest(input)
        Functional.record("remote_api.request", ["input": input])
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {{
            Functional.record("remote_api.error", ["status": String(status)])
            throw Failure(message: "The service answered with status \\(status).")
        }}
        let object = try JSONSerialization.jsonObject(with: data)
        guard let text = extract(object, path: outputField) else {{
            throw Failure(message: "The service answer had no text.")
        }}
        Functional.record("remote_api.response", ["status": String(status)])
        return text
    }}

    private static func makeRequest(_ input: String) throws -> URLRequest {{
        let trimmedBase = baseURL.hasSuffix("/") ? String(baseURL.dropLast()) : baseURL
        guard var components = URLComponents(string: trimmedBase + path) else {{
            throw Failure(message: "Invalid service address.")
        }}
        if method == "GET" {{
            components.queryItems = (components.queryItems ?? []) + [URLQueryItem(name: inputField, value: input)]
        }}
        guard let url = components.url else {{ throw Failure(message: "Invalid service address.") }}
        var request = URLRequest(url: url, timeoutInterval: 30)
        request.httpMethod = method
        for (field, value) in headers {{
            request.setValue(value, forHTTPHeaderField: field)
        }}
        if method == "POST" {{
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: [inputField: input])
        }}
        return request
    }}

    private static func extract(_ object: Any, path: String) -> String? {{
        var current: Any? = object
        for part in path.split(separator: ".").map(String.init) {{
            if let index = Int(part), let array = current as? [Any] {{
                current = array.indices.contains(index) ? array[index] : nil
            }} else {{
                current = (current as? [String: Any])?[part]
            }}
        }}
        if let text = current as? String {{ return text }}
        if let number = current as? NSNumber {{ return number.stringValue }}
        return nil
    }}
}}
"""


SCREEN_RULE = (
    "Remote API only through the scaffold module (`App/Capabilities/RemoteAPI.swift`): from the\n"
    "  user's action call `let answer = try await RemoteAPI.send(text)` in a `Task`, show a\n"
    "  progress state while it runs, render the returned text on screen and show the error\n"
    "  message on failure. Keep the input in a `TextField`/`TextEditor` and the action in a\n"
    "  `Button` whose label says what it does (Ask / Send / Translate / Generate ...). Never use\n"
    "  URLSession, hard-coded answers or timers instead of the call; in headless mode show the\n"
    "  fixture answer."
)


def functional_check(ctx: caps.CapabilityContext) -> FunctionalCheck | None:
    """Type a probe, tap the action, the stub's reply must be drawn and journaled."""
    screen = caps.capability_screen(ctx)
    if not screen:
        return None
    return FunctionalCheck(
        name=str(ctx.capability.get("name") or KEY),
        screen_id=screen,
        steps=(
            Step("type", PROBE_INPUT),
            Step("tap", ACTION_PATTERN, timeout=10),
            Step("wait_text", STUB_REPLY, timeout=20),
        ),
        expect_events=("remote_api.request", "remote_api.response"),
        mock=MOCK,
    )


def _nest(path: str, value: str) -> Any:
    result: Any = value
    for part in reversed(path.split(".")):
        result = [result] if part.isdigit() else {part: result}
    return result


class _StubHandler(BaseHTTPRequestHandler):
    output_field = DEFAULT_OUTPUT_FIELD
    requests: list[dict[str, Any]] = []

    def _reply(self, payload: dict[str, Any]) -> None:
        type(self).requests.append(payload)
        body = json.dumps(_nest(self.output_field, f"{STUB_REPLY}: {payload['input']}")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        text = next((str(v) for v in data.values()), "") if isinstance(data, dict) else ""
        self._reply({"method": "POST", "path": self.path, "input": text})

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        text = next((values[0] for values in query.values() if values), "")
        self._reply({"method": "GET", "path": self.path, "input": text})

    def log_message(self, format: str, *args: Any) -> None:
        return None


@contextmanager
def stub_server(
    output_field: str = DEFAULT_OUTPUT_FIELD,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A local JSON stub on 127.0.0.1 answering every request with :data:`STUB_REPLY`."""
    received: list[dict[str, Any]] = []
    handler = type("Stub", (_StubHandler,), {"output_field": output_field, "requests": received})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _mock(check: FunctionalCheck) -> Iterator[dict[str, str]]:
    output_field = check.env.get("IOSFORGE_REMOTE_API_OUTPUT", DEFAULT_OUTPUT_FIELD)
    with stub_server(output_field) as (url, _requests):
        yield {"IOSFORGE_REMOTE_API_URL": url}


def _functional_check(ctx: caps.CapabilityContext) -> FunctionalCheck | None:
    check = functional_check(ctx)
    if check is None:
        return None
    return FunctionalCheck(
        name=check.name,
        screen_id=check.screen_id,
        steps=check.steps,
        expect_events=check.expect_events,
        mock=check.mock,
        env={"IOSFORGE_REMOTE_API_OUTPUT": _config(ctx)["output_field"]},
    )


DESCRIPTOR = caps.CapabilityDescriptor(
    key=KEY,
    directory=caps.CAPABILITIES_DIR,
    render=lambda ctx: {f"{caps.CAPABILITIES_DIR}/RemoteAPI.swift": render_service(ctx)},
    screen_api_rule=SCREEN_RULE,
    info_properties=lambda ctx: [
        "        NSAppTransportSecurity:",
        "          NSAllowsLocalNetworking: true",
    ],
    functional_check=_functional_check,
)


def register() -> None:
    """Register the module descriptor and its stub mock."""
    caps.register(DESCRIPTOR)
    register_mock(MOCK, _mock)
