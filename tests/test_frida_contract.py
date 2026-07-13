"""Frida capture archive contract (v1.0) validation tests."""

from __future__ import annotations

import copy

import pytest

from iosforge.mvp import frida_contract as fc
from iosforge.mvp.frida_contract import FridaArchiveValidationError


def _manifest() -> dict:
    return {
        "schema_version": "1.0",
        "capture_tool": "iosforge-frida-capture/0.1.0",
        "app": {
            "bundle_id": "com.x.y",
            "appstore_id": None,
            "version": "3.2.1",
            "build": "59",
            "platform": "ios",
        },
        "device": "iPhone14,3 / iOS 17.5",
        "session": {"start": "2026-07-06T10:00:00Z", "end": "2026-07-06T10:00:18Z"},
        "counts": {"screens": 1, "requests": 2, "transactions": 1, "media": 1},
        "capabilities": {
            "network": "transaction",
            "response_bodies": "json_only",
            "response_meta": True,
            "correlation": True,
            "view_hierarchy": "uikit",
            "accessibility_ids": "synthetic",
            "event_timeline": False,
            "storekit": "v1",
            "entitlements": "partial",
            "storage": False,
            "auth": False,
            "design_tokens": False,
            "sanitized": True,
            "screen_id_stable_cross_run": False,
            "coverage": "manual",
            "coverage_metric": None,
        },
        "redaction": ["req.Authorization"],
        "integrity": {"algo": "sha256", "files": {"screens.json": "abc"}},
    }


def _screens() -> dict:
    return {
        "bundle_id": "com.x.y",
        "screen_count": 2,
        "screens": [
            {
                "id": "0000",
                "screenshot": "screens/0000.png",
                "ui_kind": "uikit",
                "signature": "sig0",
                "texts": ["Play"],
                "elements": [
                    {
                        "class": "UIButton",
                        "text": "Play",
                        "accessibility_id": "playBtn",
                        "id_synthetic": False,
                        "bounds": [0, 0, 100, 40],
                        "role": "button",
                        "value": None,
                    }
                ],
                "from": None,
                "navigates_to": [{"to": "0001", "via_element": "playBtn"}],
            },
            {
                "id": "0001",
                "screenshot": "screens/0001.png",
                "ui_kind": "swiftui",
                "signature": "sig1",
                "elements": [],
                "from": "0000",
                "navigates_to": [],
            },
        ],
    }


def _txn() -> dict:
    return {
        "id": "r7",
        "phase": "transaction",
        "t": 1783331021044,
        "screen": "0000",
        "initiator": "completion",
        "method": "POST",
        "url": "https://ms.applovin.com/5.0/i",
        "host": "ms.applovin.com",
        "path": "/5.0/i",
        "query": {"p": "1:578"},
        "req_headers": {"Authorization": "<REDACTED>"},
        "req_body": "{}",
        "status": 200,
        "mime": "application/json",
        "resp_headers": {"Server": "nginx"},
        "size": 12858,
        "body_captured": True,
        "truncated": False,
        "resp_body_ref": "json_bodies.jsonl#r7",
        "latency_ms": 292,
    }


def _req_only() -> dict:
    return {
        "id": "r12",
        "phase": "request_only",
        "t": 1783331022310,
        "screen": "0000",
        "initiator": "resume",
        "method": "POST",
        "url": "https://x.us/adn?aid=1",
        "host": "x.us",
        "path": "/adn",
        "query": {"aid": "1"},
        "req_headers": {"x-auth-token": "<REDACTED>"},
        "req_body": None,
        "status": None,
        "mime": None,
        "resp_headers": None,
        "size": None,
        "body_captured": False,
        "truncated": False,
        "resp_body_ref": None,
        "latency_ms": None,
    }


def test_valid_archive() -> None:
    summary = fc.validate_archive(
        manifest=_manifest(), screens=_screens(), network=[_txn(), _req_only()]
    )
    assert summary == {
        "schema_version": "1.0",
        "screens": 2,
        "network": 2,
        "json_bodies": 0,
        "fonts": 0,
        "media": 0,
        "sources": 0,
    }


def test_body_correlation_closure() -> None:
    body = {"request_id": "r7", "screen": "0000", "mime": "application/json", "body": "{}"}
    summary = fc.validate_archive(
        manifest=_manifest(), screens=_screens(), network=[_txn()], json_bodies=[body]
    )
    assert summary["json_bodies"] == 1


def test_dangling_resp_body_ref_rejected() -> None:
    with pytest.raises(FridaArchiveValidationError, match="resolves to no json_body"):
        fc.validate_archive(
            manifest=_manifest(), screens=_screens(), network=[_txn()], json_bodies=[]
        )


def test_orphan_body_rejected() -> None:
    orphan = {"request_id": "zzz", "screen": "0000", "body": "{}"}
    with pytest.raises(FridaArchiveValidationError, match="no network record"):
        fc.validate_archive(
            manifest=_manifest(), screens=_screens(), network=[_req_only()], json_bodies=[orphan]
        )


def test_non_json_transaction_has_no_body_ref() -> None:
    rec = _txn()
    rec.update(id="r5", mime="application/x-protobuf", body_captured=True, resp_body_ref=None)
    assert fc.validate_network_record(rec, screen_ids={"0000"})["id"] == "r5"


def test_non_http_status_rejected() -> None:
    bad = _txn()
    bad["status"] = 0
    with pytest.raises(FridaArchiveValidationError, match="non-HTTP status"):
        fc.validate_network_record(bad)


def test_null_status_on_transaction_accepted() -> None:
    rec = _txn()
    rec.update(status=None, mime=None, resp_headers=None, body_captured=False, resp_body_ref=None)
    assert fc.validate_network_record(rec, screen_ids={"0000"})["status"] is None


def test_request_only_must_null_response_fields() -> None:
    bad = _req_only()
    bad["status"] = 200
    with pytest.raises(FridaArchiveValidationError, match="request_only"):
        fc.validate_network_record(bad)


def test_bad_resp_body_ref_rejected() -> None:
    bad = _txn()
    bad["resp_body_ref"] = "r7"
    with pytest.raises(FridaArchiveValidationError, match="resp_body_ref"):
        fc.validate_network_record(bad)


def test_network_unknown_screen_rejected() -> None:
    with pytest.raises(FridaArchiveValidationError, match="unknown screen"):
        fc.validate_network_record(_txn(), screen_ids={"9999"})


def test_navigates_to_dangling_rejected() -> None:
    bad = _screens()
    bad["screens"][0]["navigates_to"] = [{"to": "nope", "via_element": None}]
    with pytest.raises(FridaArchiveValidationError, match="dangling"):
        fc.validate_screens(bad)


def test_manifest_must_not_hash_itself() -> None:
    bad = _manifest()
    bad["integrity"]["files"]["manifest.json"] = "deadbeef"
    with pytest.raises(FridaArchiveValidationError, match="manifest.json itself"):
        fc.validate_manifest(bad)


def test_unsupported_major_version_rejected() -> None:
    bad = _manifest()
    bad["schema_version"] = "2.0"
    with pytest.raises(FridaArchiveValidationError, match="schema_version major"):
        fc.validate_manifest(bad)


def test_missing_required_network_field_rejected() -> None:
    bad = _txn()
    del bad["latency_ms"]
    with pytest.raises(FridaArchiveValidationError, match="schema violation"):
        fc.validate_network_record(bad)


def test_swiftui_screen_allows_empty_elements() -> None:
    only_swiftui = copy.deepcopy(_screens())
    only_swiftui["screens"] = [only_swiftui["screens"][1]]
    only_swiftui["screens"][0]["from"] = None
    assert fc.validate_screens(only_swiftui)["screen_count"] == 2


# --- v1.1 (additive): rich hierarchy + fonts + media bytes -------------------


def _rich_source() -> dict:
    return {
        "scale": 3,
        "screen_size": {"w": 393, "h": 852},
        "safe_area_insets": {"top": 59, "left": 0, "bottom": 34, "right": 0},
        "node_count": 3,
        "root": {
            "class": "UIView",
            "frame": {"x": 0, "y": 0, "w": 393, "h": 852},
            "colors": {"background": "#0B0B0FFF"},
            "children": [
                {
                    "class": "UILabel",
                    "frame": {"x": 16, "y": 80, "w": 200, "h": 28},
                    "text": "StoryReel",
                    "font": {
                        "postscript_name": "SFProDisplay-Bold",
                        "family": "SF Pro Display",
                        "point_size": 24,
                        "weight": 700,
                        "italic": False,
                    },
                    "colors": {"text": "#FFFFFFFF"},
                },
                {
                    "class": "UIImageView",
                    "frame": {"x": 0, "y": 120, "w": 393, "h": 220},
                    "kind": "video",
                    "asset_ref": {"media_request_id": "r72"},
                    "layer": {"corner_radius": 12},
                },
            ],
        },
    }


def test_v11_manifest_validates() -> None:
    m = _manifest()
    m["schema_version"] = "1.1"
    m["capabilities"].update(
        {
            "view_hierarchy": "uikit-rich",
            "fonts": True,
            "media_bytes": True,
            "layout_geometry": True,
            "design_tokens": True,
        }
    )
    m["counts"].update({"fonts": 2, "media_files": 5})
    assert fc.validate_manifest(m)["schema_version"] == "1.1"


def test_v10_manifest_still_validates() -> None:  # back-compat
    assert fc.validate_manifest(_manifest())["schema_version"] == "1.0"


def test_rich_source_validates_and_recurses() -> None:
    src = fc.validate_source(_rich_source())
    assert src["node_count"] == 3
    assert src["root"]["children"][0]["font"]["weight"] == 700


def test_source_rejects_bad_color() -> None:
    bad = _rich_source()
    bad["root"]["colors"]["background"] = "black"  # not #RRGGBBAA
    with pytest.raises(FridaArchiveValidationError):
        fc.validate_source(bad)


def test_fonts_json_validates_and_requires_file_for_custom() -> None:
    ok = [
        {
            "postscript_name": "SFProText-Regular",
            "family": "SF Pro Text",
            "file": None,
            "is_system": True,
        },
        {
            "postscript_name": "Gilroy-Bold",
            "family": "Gilroy",
            "file": "fonts/Gilroy-Bold.ttf",
            "is_system": False,
        },
    ]
    assert len(fc.validate_fonts(ok)) == 2
    bad = [{"postscript_name": "Gilroy-Bold", "is_system": False}]  # custom w/o file
    with pytest.raises(FridaArchiveValidationError):
        fc.validate_fonts(bad)


def test_media_entries_v10_and_v11_validate() -> None:
    items = [
        {
            "url": "https://cdn.x/img.webp",
            "request_id": "r70",
            "screen": "0002",
            "kind": "image",
            "content_type": "image/webp",
            "signed_url": False,
        },  # v1.0
        {
            "url": "https://cdn.x/splash.mp4",
            "screen": "0000",
            "kind": "video",
            "path": "media/abc.mp4",
            "sha256": "abc",
            "bytes": 12345,
            "role": "splash_background",
        },  # v1.1
    ]
    assert len(fc.validate_media(items)) == 2


def test_hybrid_ui_kind_allowed() -> None:
    s = _screens()
    s["screens"][0]["ui_kind"] = "hybrid"
    assert fc.validate_screens(s)["screen_count"] == 2


def test_bundle_media_entry_without_url_validates() -> None:
    items = [
        {
            "id": "m12",
            "source": "bundle",
            "request_id": None,
            "screen": None,
            "kind": "video",
            "path": "media/deadbeef.mp4",
            "sha256": "deadbeef",
            "bytes": 29884416,
            "role": "splash_background",
        },
        {
            "id": "m40",
            "source": "runtime-snapshot",
            "screen": "0003",
            "kind": "image",
            "path": "media/cafe.png",
            "sha256": "cafe",
            "bytes": 8192,
            "role": None,
        },
    ]
    assert len(fc.validate_media(items)) == 2


def test_media_source_bad_enum_rejected() -> None:
    with pytest.raises(FridaArchiveValidationError):
        fc.validate_media([{"id": "m1", "source": "carrier-pigeon", "kind": "image"}])


def test_node_media_id_join_and_resolved_flag_validate() -> None:
    src = _rich_source()
    hero = src["root"]["children"][1]
    hero["node_id"] = "n_88"
    hero["asset_ref"] = {"media_id": "m12", "resolved": True}
    unresolved = {
        "class": "UIView",
        "node_id": "n_swiftui_overlay",
        "frame": {"x": 0, "y": 0, "w": 393, "h": 852},
        "kind": "image",
        "asset_ref": {"media_id": "m12", "resolved": False},
    }
    src["root"]["children"].append(unresolved)
    out = fc.validate_source(src)
    assert out["root"]["children"][1]["asset_ref"]["media_id"] == "m12"
    assert out["root"]["children"][-1]["asset_ref"]["resolved"] is False


def test_media_source_capability_validates() -> None:
    m = _manifest()
    m["capabilities"]["media_source"] = ["network", "bundle", "runtime-snapshot"]
    m["counts"].update({"media_bundle": 76, "media_network": 4, "media_runtime": 11})
    assert fc.validate_manifest(m)["schema_version"] == "1.0"


def test_audio_media_entry_validates() -> None:
    # a bundled sound (the core feature of audio apps) validates as a media entry
    items = [
        {
            "id": "a1",
            "source": "bundle",
            "kind": "audio",
            "path": "media/deadbeef.mp3",
            "sha256": "deadbeef",
            "bytes": 44100,
            "role": "car_brand_sound",
            "screen": "0003",
            "request_id": None,
        }
    ]
    assert len(fc.validate_media(items)) == 1
    # and it flows through the full archive validation as media
    summary = fc.validate_archive(manifest=_manifest(), screens=_screens(), media=items)
    assert summary["media"] == 1
