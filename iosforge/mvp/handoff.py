"""Enterprise handoff bundle: turn a validated App Spec v3 into developer-ready docs.

Deterministic renderers (no LLM) over the validated ``app_spec.json`` produce a
``handoff/`` directory consumed by development and the verification loop:

* ``requirements.md`` — PRD: EARS requirements grouped by priority, each traced
  to screens;
* ``design.md`` + ``tokens.json`` — design narrative + W3C design tokens ready
  for Style Dictionary / Figma import;
* ``features/acceptance.feature`` — Gherkin generated from the EARS requirements
  (the input for the Chrome/web acceptance run);
* ``traceability.md`` — requirement ↔ screen matrix;
* ``openapi.yaml`` — emitted only when the spec marks a backend as needed;
* ``README.md`` — index of the bundle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

_GHERKIN_KEYWORDS = ("given", "when", "then", "and", "but")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def render_requirements_md(spec: dict[str, Any]) -> str:
    name = spec.get("app_name", "App")
    reqs = _as_list(spec.get("requirements"))
    out = [
        f"# {name} — Requirements (PRD)",
        "",
        f"_App type: {spec.get('app_type', '?')}. {spec.get('one_liner', '')}_",
        "",
        spec.get("description", ""),
        "",
        f"**How it works:** {spec.get('how_it_works', '')}",
        "",
        f"Total requirements: {len(reqs)} (EARS, traceable to screens).",
        "",
    ]
    for priority in ("must", "should", "could", ""):
        group = [r for r in reqs if _as_dict(r).get("priority", "") == priority]
        if not group:
            continue
        out.append(f"## Priority: {priority or 'unspecified'}")
        out.append("")
        for req in group:
            r = _as_dict(req)
            screens = ", ".join(str(s) for s in _as_list(r.get("screens"))) or "—"
            out.append(f"### {r.get('id', 'REQ-?')} ({r.get('type', '')}, {r.get('source', '')})")
            out.append(f"- **Requirement:** {r.get('text', '')}")
            out.append(f"- **Screens:** {screens}")
            acceptance = _as_list(r.get("acceptance"))
            if acceptance:
                out.append("- **Acceptance:**")
                out.extend(f"  - {a}" for a in acceptance)
            out.append("")
    return "\n".join(out)


def render_design_md(spec: dict[str, Any]) -> str:
    name = spec.get("app_name", "App")
    tokens = _as_dict(spec.get("design_tokens"))
    out = [f"# {name} — Design", "", "## Design tokens (W3C)", ""]
    for group, entries in tokens.items():
        if not isinstance(entries, dict):
            out.append(f"- **{group}**: {entries}")
            continue
        named = {k: v for k, v in entries.items() if isinstance(v, dict) and "$value" in v}
        if not named:
            out.append(f"- **{group}**: {entries}")
            continue
        out.append(f"### {group}")
        out.append("")
        out.append("| token | value | type |")
        out.append("|-------|-------|------|")
        for token, body in named.items():
            b = _as_dict(body)
            out.append(f"| `{token}` | `{b.get('$value', '')}` | {b.get('$type', '')} |")
        out.append("")
    out.append("See `tokens.json` for the machine-readable W3C token set.")
    out.append("")
    return "\n".join(out)


def extract_tokens_json(spec: dict[str, Any]) -> dict[str, Any]:
    """Pull a clean W3C design-token document; non-token meta goes to $extensions."""
    design = _as_dict(spec.get("design_tokens"))
    tokens: dict[str, Any] = {}
    extensions: dict[str, Any] = {}
    for key, value in design.items():
        is_group = isinstance(value, dict) and any(
            isinstance(v, dict) and "$value" in v for v in value.values()
        )
        is_token = isinstance(value, dict) and "$value" in value
        if is_group or is_token:
            tokens[key] = value
        else:
            extensions[key] = value
    if extensions:
        tokens["$extensions"] = {"com.iosforge.meta": extensions}
    return tokens


def render_features(spec: dict[str, Any]) -> str:
    name = spec.get("app_name", "App")
    out = [
        f"Feature: {name} acceptance",
        "  Generated from EARS requirements in app_spec.json; run against the web build.",
        "",
    ]
    for req in _as_list(spec.get("requirements")):
        r = _as_dict(req)
        tags = [f"@{r.get('id', 'REQ')}", f"@{r.get('type', 'req')}"]
        if r.get("priority"):
            tags.append(f"@priority-{r['priority']}")
        if r.get("source"):
            tags.append(f"@source-{r['source']}")
        tags.extend(f"@screen-{s}" for s in _as_list(r.get("screens")))
        out.append("  " + " ".join(tags))
        out.append(f"  Scenario: {str(r.get('text', r.get('id', ''))).strip()}")
        acceptance = [str(a).strip() for a in _as_list(r.get("acceptance")) if str(a).strip()]
        if not acceptance:
            acceptance = [str(r.get("text", "")).strip()]
        for step in acceptance:
            keyword = step.split(" ", 1)[0].lower().rstrip(":")
            out.append(f"    {step}" if keyword in _GHERKIN_KEYWORDS else f"    * {step}")
        out.append("")
    return "\n".join(out)


def render_traceability_md(spec: dict[str, Any]) -> str:
    out = [
        f"# {spec.get('app_name', 'App')} — Traceability",
        "",
        "| Requirement | Type | Priority | Source | Screens | Acceptance |",
        "|-------------|------|----------|--------|---------|------------|",
    ]
    for req in _as_list(spec.get("requirements")):
        r = _as_dict(req)
        screens = ", ".join(str(s) for s in _as_list(r.get("screens"))) or "—"
        out.append(
            f"| {r.get('id', '?')} | {r.get('type', '')} | {r.get('priority', '')} "
            f"| {r.get('source', '')} | {screens} | {len(_as_list(r.get('acceptance')))} |"
        )
    out.append("")
    return "\n".join(out)


def render_openapi(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Minimal OpenAPI 3.1 skeleton, only when the spec marks a backend as needed."""
    backend = _as_dict(spec.get("backend"))
    if not backend.get("backend_needed"):
        return None
    paths: dict[str, Any] = {}
    for api in _as_list(backend.get("apis")):
        if isinstance(api, str) and api.strip():
            parts = api.split()
            has_method = len(parts) >= 2 and parts[0].isalpha()
            method = parts[0].lower() if has_method else "get"
            route = parts[1] if has_method else parts[0]
            paths.setdefault(route, {})[method] = {
                "summary": api,
                "responses": {"200": {"description": "OK"}},
            }
    schemas: dict[str, Any] = {}
    for entity in _as_list(_as_dict(spec.get("content")).get("data_model")):
        e = _as_dict(entity)
        ename = e.get("entity")
        if not ename:
            continue
        props = {str(f): {"type": "string"} for f in _as_list(e.get("fields"))}
        schemas[str(ename)] = {"type": "object", "properties": props}
    return {
        "openapi": "3.1.0",
        "info": {"title": f"{spec.get('app_name', 'App')} API", "version": "0.1.0"},
        "paths": paths or {"/health": {"get": {"responses": {"200": {"description": "OK"}}}}},
        "components": {"schemas": schemas},
    }


def _render_index_md(spec: dict[str, Any], files: list[str]) -> str:
    out = [
        f"# {spec.get('app_name', 'App')} — Handoff bundle",
        "",
        f"Spec version: {spec.get('spec_version', '?')} · "
        f"generator: {_as_dict(spec.get('provenance')).get('generator', '?')}",
        "",
        "Developer-ready artifacts derived from the validated `app_spec.json`:",
        "",
    ]
    out.extend(f"- `{f}`" for f in files)
    out.append("")
    return "\n".join(out)


def build_handoff(spec: dict[str, Any], handoff_dir: Path) -> list[Path]:
    """Render the full handoff bundle into ``handoff_dir``; return written paths."""
    handoff_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _write(relative: str, text: str) -> None:
        path = handoff_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)

    _write("requirements.md", render_requirements_md(spec))
    _write("design.md", render_design_md(spec))
    _write("tokens.json", json.dumps(extract_tokens_json(spec), indent=2, ensure_ascii=False))
    _write("features/acceptance.feature", render_features(spec))
    _write("traceability.md", render_traceability_md(spec))

    openapi = render_openapi(spec)
    if openapi is not None:
        _write("openapi.yaml", yaml.safe_dump(openapi, sort_keys=False, allow_unicode=True))

    names = [str(p.relative_to(handoff_dir)) for p in written]
    _write("README.md", _render_index_md(spec, names))
    return written
