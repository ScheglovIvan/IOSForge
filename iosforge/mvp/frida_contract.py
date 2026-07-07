"""Machine-checkable contract for the Frida capture archive (v1.0 / v1.1).

v1.1 is additive and back-compatible (consumer accepts any ``1.x``): it adds a
rich per-screen view hierarchy (``source/<id>.json`` — recursive node tree with
geometry, typography, colors, layer props: :data:`SOURCE_SCHEMA`), a ``fonts.json``
(:data:`FONTS_SCHEMA`) and byte-backed ``media.json`` entries
(:data:`MEDIA_ENTRY_SCHEMA`). Mirrors the producer's v1.1 delta.

The enforced form of ``docs/frida-archive.md``: JSON Schemas (Draft 2020-12) for
``manifest.json``, ``screens.json`` and a ``network.jsonl`` record, plus semantic
cross-checks that a plain schema cannot express (``request_only`` records must
null their response fields, ``navigates_to`` may only point at existing screens,
network events reference a real screen, ``manifest.integrity`` never hashes
itself). Mirrors :mod:`iosforge.mvp.spec_contract`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "1.1"

_NETWORK_PHASES = ["transaction", "request_only"]
_INITIATORS = ["resume", "completion", "delegate"]
_UI_KINDS = ["uikit", "swiftui", "hybrid"]

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_STR_OR_NULL = {"type": ["string", "null"]}
_INT_OR_NULL = {"type": ["integer", "null"]}
_OBJ_OR_NULL = {"type": ["object", "null"]}

_CAPABILITIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["network", "response_bodies", "correlation", "sanitized"],
    "properties": {
        "network": {"enum": ["transaction", "request_only", "none"]},
        "response_bodies": {"enum": ["json_only", "all", "none"]},
        "response_meta": _BOOL,
        "correlation": _BOOL,
        # "uikit-rich" (v1.1): source/<id>.json carries the full node tree with
        # geometry, typography, colors and layer props (see SOURCE_SCHEMA).
        "view_hierarchy": {"enum": ["uikit-rich", "uikit", "none"]},
        "accessibility_ids": {"enum": ["identifier", "synthetic", "none"]},
        "event_timeline": _BOOL,
        "storekit": {"enum": ["v1", "v2", "none"]},
        "entitlements": {"enum": ["full", "partial", "none"]},
        "storage": _BOOL,
        "auth": _BOOL,
        "design_tokens": _BOOL,
        # v1.1 additive capability flags.
        "fonts": _BOOL,
        "media_bytes": _BOOL,
        "layout_geometry": _BOOL,
        "media_source": {
            "type": "array",
            "items": {"enum": ["network", "bundle", "runtime-snapshot"]},
        },
        "sanitized": _BOOL,
        "screen_id_stable_cross_run": _BOOL,
        "coverage": {"enum": ["manual", "auto_bfs"]},
        "coverage_metric": {"type": ["number", "null"]},
    },
}

MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version",
        "app",
        "device",
        "session",
        "counts",
        "capabilities",
        "integrity",
    ],
    "properties": {
        "schema_version": {"type": "string", "pattern": r"^\d+\.\d+$"},
        "capture_tool": _STR,
        "app": {
            "type": "object",
            "required": ["bundle_id", "version", "platform"],
            "properties": {
                "bundle_id": _STR,
                "appstore_id": _STR_OR_NULL,
                "version": _STR,
                "build": _STR,
                "platform": {"enum": ["ios"]},
            },
        },
        "device": _STR,
        "session": {
            "type": "object",
            "required": ["start", "end"],
            "properties": {"start": _STR, "end": _STR},
        },
        "counts": {"type": "object", "additionalProperties": _INT},
        "capabilities": _CAPABILITIES_SCHEMA,
        "redaction": {"type": "array", "items": _STR},
        "integrity": {
            "type": "object",
            "required": ["algo", "files"],
            "properties": {
                "algo": {"enum": ["sha256"]},
                "files": {"type": "object", "additionalProperties": _STR},
            },
        },
    },
}

_ELEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["accessibility_id", "id_synthetic", "bounds"],
    "properties": {
        "class": _STR,
        "text": _STR_OR_NULL,
        "accessibility_id": _STR,
        "id_synthetic": _BOOL,
        "bounds": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "role": _STR_OR_NULL,
        "value": _STR_OR_NULL,
    },
}

_SCREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "screenshot", "ui_kind", "signature", "elements", "navigates_to"],
    "properties": {
        "id": _STR,
        "screenshot": _STR,
        "ui_kind": {"enum": _UI_KINDS},
        "view_controller": _STR_OR_NULL,
        "signature": _STR,
        "texts": {"type": "array", "items": _STR},
        "elements": {"type": "array", "items": _ELEMENT_SCHEMA},
        "native_ads": {"type": "array"},
        "from": _STR_OR_NULL,
        "navigates_to": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["to", "via_element"],
                "properties": {"to": _STR, "via_element": _STR_OR_NULL},
            },
        },
    },
}

SCREENS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["bundle_id", "screen_count", "screens"],
    "properties": {
        "bundle_id": _STR,
        "screen_count": _INT,
        "screens": {"type": "array", "items": _SCREEN_SCHEMA},
    },
}

# --------------------------------------------------------------------------- #
# v1.1 (additive): rich per-screen view hierarchy + fonts + media bytes.
# --------------------------------------------------------------------------- #

_COLOR = {"type": ["string", "null"], "pattern": r"^#[0-9A-Fa-f]{8}$"}  # sRGB #RRGGBBAA
_NUM = {"type": "number"}

SOURCE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["scale", "screen_size", "root"],
    "properties": {
        "scale": _NUM,  # px = pt * scale
        "screen_size": {"type": "object", "properties": {"w": _NUM, "h": _NUM}},
        "safe_area_insets": {
            "type": "object",
            "properties": {"top": _NUM, "left": _NUM, "bottom": _NUM, "right": _NUM},
        },
        "node_count": _INT,
        "root": {"$ref": "#/$defs/node"},
    },
    "$defs": {
        "color": _COLOR,
        "font": {
            "type": "object",
            "properties": {
                "postscript_name": _STR,
                "family": _STR,
                "point_size": _NUM,
                "weight": {"type": "integer", "minimum": 100, "maximum": 900},
                "italic": _BOOL,
            },
        },
        "node": {
            "type": "object",
            "required": ["class", "frame"],
            "properties": {
                "class": _STR,
                "node_id": _STR,  # v1.1: stable per-node id for the media join
                "role": _STR,
                # frame in POINTS, origin top-left, in screen coordinates.
                "frame": {
                    "type": "object",
                    "required": ["x", "y", "w", "h"],
                    "properties": {"x": _NUM, "y": _NUM, "w": _NUM, "h": _NUM},
                },
                "children": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/node"},
                },  # nesting + z-order
                "text": _STR_OR_NULL,
                "font": {"$ref": "#/$defs/font"},
                "colors": {
                    "type": "object",
                    "properties": {
                        "text": {"$ref": "#/$defs/color"},
                        "background": {"$ref": "#/$defs/color"},
                        "tint": {"$ref": "#/$defs/color"},
                    },
                },
                "layer": {
                    "type": "object",
                    "properties": {
                        "corner_radius": _NUM,
                        "border_width": _NUM,
                        "border_color": {"$ref": "#/$defs/color"},
                        "opacity": _NUM,
                        "shadow": {
                            "type": "object",
                            "properties": {
                                "opacity": _NUM,
                                "radius": _NUM,
                                "color": {"$ref": "#/$defs/color"},
                                "offset": {"type": "object", "properties": {"w": _NUM, "h": _NUM}},
                            },
                        },
                    },
                },
                "content_mode": _STR,
                "alpha": _NUM,
                "is_hidden": _BOOL,
                "accessibility_id": _STR,
                "accessibility_label": _STR_OR_NULL,
                "kind": {"enum": ["image", "video", "animation"]},
                "asset_ref": {
                    "type": "object",
                    "properties": {
                        "bundle_asset": _STR,
                        "media_request_id": _STR,
                        "media_id": _STR,  # v1.1: joins into media.json[].id
                        "resolved": _BOOL,  # false → bytes not recoverable (e.g. SwiftUI overlay)
                        "kind": {"enum": ["image", "video", "animation"]},
                    },
                },
                "scroll": {
                    "type": "object",
                    "properties": {
                        "item_count": _INT,
                        "cell_class": _STR,
                        "axis": {"enum": ["vertical", "horizontal"]},
                    },
                },
            },
        },
    },
}

FONTS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "array",
    "items": {
        "type": "object",
        "required": ["postscript_name", "is_system"],
        "properties": {
            "postscript_name": _STR,
            "family": _STR,
            "file": _STR_OR_NULL,  # custom → "fonts/<file>"; system → null
            "is_system": _BOOL,
        },
    },
}

# media.json entry — v1.0 shape (url-only) stays valid; v1.1 adds byte-file fields.
MEDIA_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [],
    "properties": {
        "id": _STR,  # v1.1: stable media id, target of node asset_ref.media_id
        # v1.1: bundle/runtime-snapshot media have no network URL.
        "source": {"enum": ["network", "bundle", "runtime-snapshot"]},
        "url": _STR,  # network entries only
        "request_id": _STR_OR_NULL,  # null for bundle/snapshot
        "screen": _STR_OR_NULL,  # null when not tied to a captured screen
        "kind": _STR,
        "content_type": _STR_OR_NULL,
        "signed_url": {"type": ["boolean", "string"]},
        "path": _STR,  # v1.1: local "media/<sha256>.<ext>"
        "sha256": _STR,
        "bytes": _INT,
        "role": _STR_OR_NULL,  # splash_background | paywall_hero | thumbnail | icon | null
        "skipped_large": _BOOL,
    },
}

NETWORK_RECORD_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "id",
        "phase",
        "t",
        "screen",
        "initiator",
        "method",
        "url",
        "host",
        "path",
        "query",
        "req_headers",
        "req_body",
        "status",
        "mime",
        "resp_headers",
        "size",
        "body_captured",
        "truncated",
        "resp_body_ref",
        "latency_ms",
    ],
    "properties": {
        "id": _STR,
        "phase": {"enum": _NETWORK_PHASES},
        "t": _INT,
        "screen": _STR,
        "initiator": {"enum": _INITIATORS},
        "method": _STR,
        "url": _STR,
        "host": _STR,
        "path": _STR,
        "query": {"type": "object"},
        "req_headers": {"type": "object"},
        "req_body": _STR_OR_NULL,
        "status": _INT_OR_NULL,
        "mime": _STR_OR_NULL,
        "resp_headers": _OBJ_OR_NULL,
        "size": _INT_OR_NULL,
        "body_captured": _BOOL,
        "truncated": _BOOL,
        "resp_body_ref": _STR_OR_NULL,
        "latency_ms": _INT_OR_NULL,
    },
}

JSON_BODY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["request_id", "screen", "body"],
    "properties": {
        "request_id": _STR,
        "screen": _STR,
        "mime": _STR_OR_NULL,
        "body": {"type": ["string", "object", "array", "null"]},
    },
}

_RESP_ONLY_FIELDS = ("status", "mime", "resp_headers", "size", "resp_body_ref", "latency_ms")


class FridaArchiveValidationError(RuntimeError):
    """Raised when a manifest / screens / network record violates the contract."""


def _first_schema_error(schema: dict[str, Any], payload: object) -> str | None:
    error = next(iter(Draft202012Validator(schema).iter_errors(payload)), None)
    if error is None:
        return None
    location = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return f"schema violation at {location!r}: {error.message}"


def _as_object(payload: object, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FridaArchiveValidationError(f"{what} must be a JSON object")
    return cast(dict[str, Any], payload)


def validate_manifest(payload: object) -> dict[str, Any]:
    """Validate ``manifest.json`` (schema + major version + integrity self-hash)."""
    manifest = _as_object(payload, "manifest")
    err = _first_schema_error(MANIFEST_SCHEMA, manifest)
    if err is not None:
        raise FridaArchiveValidationError(err)
    major = manifest["schema_version"].split(".")[0]
    if major != SCHEMA_VERSION.split(".")[0]:
        raise FridaArchiveValidationError(
            f"unsupported schema_version major: {manifest['schema_version']!r} "
            f"(consumer supports {SCHEMA_VERSION})"
        )
    if "manifest.json" in manifest["integrity"]["files"]:
        raise FridaArchiveValidationError("integrity.files must not hash manifest.json itself")
    return manifest


def validate_screens(payload: object) -> dict[str, Any]:
    """Validate ``screens.json`` (schema + navigation cross-references)."""
    screens = _as_object(payload, "screens")
    err = _first_schema_error(SCREENS_SCHEMA, screens)
    if err is not None:
        raise FridaArchiveValidationError(err)
    ids = {str(s["id"]) for s in screens["screens"]}
    dangling: list[str] = []
    for screen in screens["screens"]:
        parent = screen.get("from")
        if parent is not None and parent not in ids:
            dangling.append(f"screen {screen['id']!r} from unknown screen {parent!r}")
        for edge in screen["navigates_to"]:
            if edge["to"] not in ids:
                dangling.append(
                    f"screen {screen['id']!r} navigates_to unknown screen {edge['to']!r}"
                )
    if dangling:
        raise FridaArchiveValidationError("dangling references: " + "; ".join(dangling[:20]))
    return screens


def validate_source(payload: object) -> dict[str, Any]:
    """Validate a v1.1 ``source/<id>.json`` rich view hierarchy (recursive node tree)."""
    src = _as_object(payload, "source")
    err = _first_schema_error(SOURCE_SCHEMA, src)
    if err is not None:
        raise FridaArchiveValidationError(err)
    return src


def validate_fonts(payload: object) -> list[dict[str, Any]]:
    """Validate a v1.1 ``fonts.json`` — custom fonts must reference a bundled file."""
    err = _first_schema_error(FONTS_SCHEMA, payload)
    if err is not None:
        raise FridaArchiveValidationError(err)
    fonts = cast(list[dict[str, Any]], payload)
    for f in fonts:
        if not f["is_system"] and not f.get("file"):
            raise FridaArchiveValidationError(
                f"custom font {f['postscript_name']!r} must reference a bundled file"
            )
    return fonts


def validate_media(payload: object) -> list[dict[str, Any]]:
    """Validate ``media.json`` entries (v1.0 url-only + v1.1 byte-file fields)."""
    if not isinstance(payload, list):
        raise FridaArchiveValidationError("media.json must be a JSON array")
    for item in payload:
        err = _first_schema_error(MEDIA_ENTRY_SCHEMA, item)
        if err is not None:
            raise FridaArchiveValidationError(err)
    return cast(list[dict[str, Any]], payload)


def validate_network_record(
    payload: object, *, screen_ids: set[str] | None = None
) -> dict[str, Any]:
    """Validate one ``network.jsonl`` record (schema + phase/screen semantics)."""
    rec = _as_object(payload, "network record")
    err = _first_schema_error(NETWORK_RECORD_SCHEMA, rec)
    if err is not None:
        raise FridaArchiveValidationError(err)
    status = rec["status"]
    if status is not None and not (100 <= status <= 599):
        raise FridaArchiveValidationError(
            f"record {rec['id']!r} has non-HTTP status {status!r}; use null for 'no response'"
        )
    if rec["phase"] == "request_only":
        offending = [f for f in _RESP_ONLY_FIELDS if rec.get(f) is not None]
        if offending or rec["body_captured"]:
            raise FridaArchiveValidationError(
                f"request_only record {rec['id']!r} must null response fields "
                f"(offending: {offending + (['body_captured'] if rec['body_captured'] else [])})"
            )
    ref = rec["resp_body_ref"]
    if ref is not None and not ref.startswith("json_bodies.jsonl#"):
        raise FridaArchiveValidationError(
            f"record {rec['id']!r} resp_body_ref must be 'json_bodies.jsonl#<id>', got {ref!r}"
        )
    if screen_ids is not None and rec["screen"] not in screen_ids:
        raise FridaArchiveValidationError(
            f"record {rec['id']!r} references unknown screen {rec['screen']!r}"
        )
    return rec


def validate_archive(
    *,
    manifest: object,
    screens: object,
    network: Sequence[object] | None = None,
    json_bodies: Sequence[object] | None = None,
    fonts: object | None = None,
    media: object | None = None,
    sources: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Validate a full archive: manifest + screens + (optional) network + bodies.

    When ``json_bodies`` is given, also validates each body and enforces
    correlation closure: every ``resp_body_ref`` resolves to a present body and
    every body's ``request_id`` belongs to a network record. The optional v1.1
    payload (``fonts.json``, ``media.json`` and the per-screen ``source/<id>.json``
    rich hierarchies passed as ``sources``) is validated when supplied. Raises
    :class:`FridaArchiveValidationError` with a precise, single message.
    """
    m = validate_manifest(manifest)
    s = validate_screens(screens)
    ids = {str(sc["id"]) for sc in s["screens"]}
    net = list(network or [])
    net_ids: set[str] = set()
    for rec in net:
        net_ids.add(validate_network_record(rec, screen_ids=ids)["id"])

    body_ids: set[str] = set()
    if json_bodies is not None:
        for body in json_bodies:
            b = _as_object(body, "json_body")
            err = _first_schema_error(JSON_BODY_SCHEMA, b)
            if err is not None:
                raise FridaArchiveValidationError(err)
            if b["request_id"] not in net_ids:
                raise FridaArchiveValidationError(
                    f"json_body request_id {b['request_id']!r} has no network record"
                )
            body_ids.add(b["request_id"])
        for rec in net:
            ref = cast(dict[str, Any], rec).get("resp_body_ref")
            if ref is not None and ref.split("#", 1)[-1] not in body_ids:
                raise FridaArchiveValidationError(f"resp_body_ref {ref!r} resolves to no json_body")

    fonts_count = 0
    if fonts is not None:
        fonts_count = len(validate_fonts(fonts))
    media_count = 0
    if media is not None:
        media_count = len(validate_media(media))
    sources_count = 0
    if sources is not None:
        for sid, payload in sources.items():
            if sid not in ids:
                raise FridaArchiveValidationError(f"source/{sid}.json has no matching screen")
            validate_source(payload)
            sources_count += 1

    return {
        "schema_version": m["schema_version"],
        "screens": len(ids),
        "network": len(net),
        "json_bodies": len(body_ids),
        "fonts": fonts_count,
        "media": media_count,
        "sources": sources_count,
    }
