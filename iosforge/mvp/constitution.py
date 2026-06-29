"""Development constitution: the steering doc every codegen worker must follow.

Deterministically rendered from the validated App Spec v3 (no LLM), so that
parallel Hermes/Claude workers build a CONSISTENT app instead of each picking its
own stack, navigation and style. This is the mobile-tuned equivalent of a Spec
Kit "constitution" / Kiro "tech.md".

Fixed decisions (confirmed with the user):
* Target = **iOS** (built on Codemagic); Android + Chrome web are for testing.
* Stack = **Flutter + Riverpod + go_router**, theme generated from design tokens,
  feature-first structure.
* Fidelity = **1:1 to the captured screens, iOS conventions win on conflict**
  (where an element differs on iOS, build the iOS/Cupertino form).
* Scope = full app **with backend when the spec needs one**; lean process
  (verify at milestones, not after every change).
"""

from __future__ import annotations

from typing import Any

STACK = {
    "framework": "Flutter (stable)",
    "state": "Riverpod (flutter_riverpod / riverpod_annotation)",
    "routing": "go_router",
    "http": "dio",
    "theme": "ThemeData generated from handoff/tokens.json",
    "structure": "feature-first (lib/features/<feature>/, lib/core/{theme,router,data})",
}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _routes(spec: dict[str, Any]) -> list[tuple[str, str, str]]:
    routes: list[tuple[str, str, str]] = []
    for screen in _as_list(spec.get("screens")):
        s = _as_dict(screen)
        sid = str(s.get("id", ""))
        if not sid:
            continue
        route = str(s.get("route") or f"/{sid}")
        routes.append((sid, route, str(s.get("name", sid))))
    return routes


def render_constitution(spec: dict[str, Any]) -> str:
    """Render CONSTITUTION.md — the binding steering doc for all codegen workers."""
    name = spec.get("app_name", "App")
    backend = _as_dict(spec.get("backend"))
    backend_needed = bool(backend.get("backend_needed"))
    routes = _routes(spec)

    out: list[str] = [
        f"# {name} — Development Constitution",
        "",
        "Binding rules for every codegen worker. Do not deviate. The actual coding "
        "is performed by the local `claude` CLI; this document is the contract it follows.",
        "",
        "## Target & build",
        "- **Primary platform: iOS** — the deliverable is an iOS app, built on **Codemagic**.",
        "- Develop and test on **Android emulator + Chrome (Flutter web)**; the iOS `.ipa`"
        " is produced on Codemagic (a `codemagic.yaml` is part of the scaffold).",
        "- Native-only features (camera, IAP, push, pickers) do not run on web — guard them"
        " and verify on the Android emulator at the milestone gate.",
        "",
        "## Fixed stack (do not substitute)",
    ]
    out += [f"- **{key}**: {value}" for key, value in STACK.items()]
    out += [
        "",
        "## Fidelity rule (1:1, iOS-priority)",
        "- Reproduce each captured screen **1:1** for content, layout, brand and colors.",
        "- **iOS conventions win on conflict**: where an element is done differently on iOS"
        " (back navigation, switches, action sheets, nav bars, pickers, scroll physics),"
        " build the **iOS/Cupertino** form rather than the Android one.",
        "- Respect safe areas, status bar and gestures per the iOS HIG.",
        "",
        "## Theming",
        "- Build `ThemeData` (light + dark) **from `handoff/tokens.json`** (W3C design tokens)."
        " Do not eyeball colors when a token exists.",
        "",
        "## Routing (declarative, from the navigation graph)",
        "- Use `go_router`. Register exactly these routes (one screen each):",
    ]
    if routes:
        out += [f"  - `{route}` → `{name_}` (screen id `{sid}`)" for sid, route, name_ in routes]
    else:
        out.append("  - (no screens in spec)")
    out += [
        "",
        "## State & data",
        "- State via **Riverpod** providers; no `setState` for shared/business state.",
        "- Data models from `app_spec.json` `content.data_model`; repositories abstract the"
        " data source.",
        "",
        "## Backend",
    ]
    if backend_needed:
        out += [
            "- This app **needs a backend**. Build a real service from"
            " `app_spec.json` `backend` + `content.data_model` + `handoff/openapi.yaml`,"
            " and an app-side API layer (dio client + repositories) against it.",
            "- The API contract is **inferred from the UI** (no network capture) — implement it"
            " faithfully to the spec and mark inferred endpoints with a `TODO(contract)` note.",
        ]
    else:
        persistence = _as_dict(spec.get("content")).get("persistence", "local")
        out += [
            "- This app **does not need a backend**. Persist locally per"
            f" `content.persistence` ({persistence}).",
        ]
    out += [
        "",
        "## Requirements & acceptance",
        "- Implement requirements by id from `handoff/requirements.md`"
        " (`must` first, then `should`; defer `could`).",
        "- Each requirement maps to a Gherkin scenario in"
        " `handoff/features/acceptance.feature` — the app must satisfy them on the web build.",
        "",
        "## Process (lean — confirmed)",
        "- **Verify at milestones, not after every change**: after scaffold+theme+routing,"
        " after all screens, after integration, and a final gate.",
        "- Milestone gate = `flutter analyze` clean + `flutter build web` succeeds +"
        " `acceptance.feature` green in headless Chrome + visual match vs `screens/`.",
        "- Cap corrective iterations (2-3). Review only at major milestones / the end.",
        "",
        "## Store-ready bar (still MVP)",
        "- Compiles and runs; no analyzer errors; no placeholder/lorem text or crashes on"
        " captured flows; app icon + launch screen + `Info.plist` permissions + `codemagic.yaml`"
        " present. Build only what the spec covers — do not invent features.",
        "",
    ]
    return "\n".join(out)
