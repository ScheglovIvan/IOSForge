"""Formal contract for the App Spec (v3) handed to development / codegen.

Senior/enterprise hardening of Stage B (see ``docs/app-spec-v2.md`` and the
2026-06-29 DECISIONS entry):

* a machine-checkable JSON Schema (Draft 2020-12) — typed, enum-constrained,
  with required sections, so an incomplete or malformed spec never reaches
  codegen;
* cross-reference integrity — navigation / requirements may only point at
  screen ids that exist, mirroring the acyclic check already done for
  ``tasks.json``;
* screen-coverage check against the crawl, so every crawled screen is accounted
  for;
* provenance + ``spec_version`` for reproducibility and audit.

Requirements use EARS types (``ubiquitous`` / ``event_driven`` / ``state_driven``
/ ``optional_feature`` / ``unwanted_behavior``) and carry stable ids so the spec
is traceable into tasks, code and tests. Design is expressed as W3C-style design
tokens (``$value`` / ``$type`` / ``$description``).
"""

from __future__ import annotations

from typing import Any, cast

from jsonschema import Draft202012Validator

SPEC_VERSION = "3.0"

_APP_TYPES = [
    "game",
    "photo-editor",
    "social",
    "utility",
    "content-subscription",
    "e-commerce",
    "productivity",
    "health",
    "education",
    "media",
    "other",
]

_EARS_TYPES = [
    "ubiquitous",
    "event_driven",
    "state_driven",
    "optional_feature",
    "unwanted_behavior",
]

_SOURCE_KINDS = ["observed", "inferred"]

_STR = {"type": "string"}
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}

APP_SPEC_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "IOSForge App Spec v3",
    "type": "object",
    "required": [
        "spec_version",
        "provenance",
        "app_name",
        "app_type",
        "description",
        "how_it_works",
        "market_research",
        "business_logic",
        "screens",
        "requirements",
        "design_tokens",
        "navigation",
        "content",
        "monetization",
        "backend",
        "permissions",
        "integrations",
        "cross_cutting",
        "analysis_quality",
        "acceptance_criteria",
    ],
    "properties": {
        "spec_version": {"const": SPEC_VERSION},
        "provenance": {
            "type": "object",
            "required": ["generator", "generated_at", "source_crawl_sha256", "screen_count"],
            "properties": {
                "generator": _STR,
                "generated_at": _STR,
                "source_crawl_sha256": _STR,
                "screen_count": {"type": "integer", "minimum": 0},
            },
        },
        "app_name": _STR,
        "package": _STR,
        "app_type": {"type": "string", "enum": _APP_TYPES},
        "one_liner": _STR,
        "description": _STR,
        "how_it_works": _STR,
        "target_audience": _STR,
        "platforms": _STR_ARRAY,
        "market_research": {"type": "object"},
        "business_logic": {"type": "object"},
        "screens": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "name", "purpose"],
                "properties": {
                    "id": _STR,
                    "name": _STR,
                    "purpose": _STR,
                    "screenshot": _STR,
                    "source": {"type": "string", "enum": _SOURCE_KINDS},
                    "navigates_to": _STR_ARRAY,
                },
            },
        },
        "requirements": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "type", "text"],
                "properties": {
                    "id": {"type": "string", "pattern": "^REQ-[A-Za-z0-9_-]+$"},
                    "type": {"type": "string", "enum": _EARS_TYPES},
                    "text": _STR,
                    "priority": {"type": "string", "enum": ["must", "should", "could"]},
                    "screens": _STR_ARRAY,
                    "acceptance": _STR_ARRAY,
                    "source": {"type": "string", "enum": _SOURCE_KINDS},
                },
            },
        },
        "design_tokens": {"type": "object"},
        "navigation": {
            "type": "object",
            "properties": {
                "type": _STR,
                "deep_links": _STR_ARRAY,
                "map": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"from": _STR, "to": _STR, "via": _STR},
                    },
                },
            },
        },
        "content": {"type": "object"},
        "monetization": {"type": "object"},
        "backend": {"type": "object"},
        "permissions": {"type": "array"},
        "integrations": {"type": "array"},
        "cross_cutting": {"type": "object"},
        "analysis_quality": {
            "type": "object",
            "required": ["assumptions", "open_questions", "coverage_gaps", "confidence"],
            "properties": {
                "assumptions": _STR_ARRAY,
                "open_questions": _STR_ARRAY,
                "coverage_gaps": _STR_ARRAY,
                "confidence": {"type": "object"},
            },
        },
        "acceptance_criteria": {"type": "array", "minItems": 1},
    },
}


class SpecValidationError(RuntimeError):
    """Raised when an app_spec fails schema or cross-reference validation."""


def _screen_ids(spec: dict[str, Any]) -> set[str]:
    return {str(s["id"]) for s in spec.get("screens", []) if isinstance(s, dict) and "id" in s}


def _cross_reference_errors(spec: dict[str, Any]) -> list[str]:
    ids = _screen_ids(spec)
    errors: list[str] = []
    for screen in spec.get("screens", []):
        if not isinstance(screen, dict):
            continue
        for target in screen.get("navigates_to", []):
            if target not in ids:
                errors.append(f"screen {screen.get('id')!r} navigates_to unknown screen {target!r}")
    navigation = spec.get("navigation", {})
    if isinstance(navigation, dict):
        for edge in navigation.get("map", []):
            if not isinstance(edge, dict):
                continue
            for side in ("from", "to"):
                value = edge.get(side)
                if value and value not in ids:
                    errors.append(f"navigation.map.{side} references unknown screen {value!r}")
    for req in spec.get("requirements", []):
        if not isinstance(req, dict):
            continue
        for sid in req.get("screens", []):
            if sid not in ids:
                errors.append(f"requirement {req.get('id')!r} references unknown screen {sid!r}")
    return errors


def validate_spec(payload: object) -> dict[str, Any]:
    """Validate an app_spec against the v3 schema and cross-references.

    Raises :class:`SpecValidationError` with a precise, single message on the
    first schema violation, or a combined message for dangling references.
    """
    if not isinstance(payload, dict):
        raise SpecValidationError("app_spec must be a JSON object")
    spec = cast(dict[str, Any], payload)
    validator = Draft202012Validator(APP_SPEC_SCHEMA)
    error = next(iter(validator.iter_errors(spec)), None)
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise SpecValidationError(f"schema violation at {location!r}: {error.message}")
    dangling = _cross_reference_errors(spec)
    if dangling:
        raise SpecValidationError("dangling references: " + "; ".join(dangling[:20]))
    return spec


def uncovered_screens(spec: dict[str, Any], crawl_screen_ids: set[str]) -> list[str]:
    """Crawled screen ids that the spec does not account for (coverage gap)."""
    return sorted(crawl_screen_ids - _screen_ids(spec))


def build_provenance(
    *, generator: str, generated_at: str, source_crawl_sha256: str, screen_count: int
) -> dict[str, Any]:
    """Provenance block injected into the spec for reproducibility / audit."""
    return {
        "generator": generator,
        "generated_at": generated_at,
        "source_crawl_sha256": source_crawl_sha256,
        "screen_count": screen_count,
    }
