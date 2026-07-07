"""Frida archive ingestion adapter tests (synthetic conformant archive)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from iosforge.mvp import frida_ingest
from iosforge.mvp.frida_ingest import FridaIngestError
from iosforge.mvp.paths import RunPaths

_PNG = b"\x89PNG\r\n\x1a\n"


def _archive_files() -> dict[str, bytes]:
    """A minimal contract-v1.0-conformant archive. ``status`` is a STRING on
    purpose to exercise the adapter's coercion before validation."""
    screens = {
        "bundle_id": "com.x.y",
        "screen_count": 1,
        "screens": [
            {
                "id": "0000",
                "screenshot": "screens/0000.png",
                "ui_kind": "uikit",
                "view_controller": "HomeController",
                "signature": "sig0",
                "texts": ["Live"],
                "elements": [
                    {
                        "class": "UIButton",
                        "text": "Live",
                        "accessibility_id": "syn_724",
                        "id_synthetic": True,
                        "bounds": [56, 90, 132, 44],
                        "role": "button",
                        "value": None,
                    }
                ],
                "native_ads": [],
                "from": None,
                "navigates_to": [],
            }
        ],
    }
    txn = {
        "id": "r1",
        "phase": "transaction",
        "t": 1783339644887,
        "screen": "0000",
        "initiator": "completion",
        "method": "POST",
        "url": "https://ms.applovin.com/5.0/i",
        "host": "ms.applovin.com",
        "path": "/5.0/i",
        "query": {},
        "req_headers": {"Authorization": "<REDACTED>"},
        "req_body": None,
        "status": "200",
        "mime": "application/json",
        "resp_headers": {"Server": "nginx"},
        "size": 12834,
        "body_captured": True,
        "truncated": False,
        "resp_body_ref": "json_bodies.jsonl#r1",
        "latency_ms": 327,
    }
    body = {"request_id": "r1", "screen": "0000", "mime": "application/json", "body": "{}"}
    media = [
        {
            "url": "https://cdn/x.jpeg",
            "request_id": "r1",
            "screen": "0000",
            "kind": "image",
            "content_type": "image/jpeg",
            "signed_url": False,
        }
    ]
    return {
        "screens.json": json.dumps(screens).encode(),
        "network.jsonl": (json.dumps(txn) + "\n").encode(),
        "json_bodies.jsonl": (json.dumps(body) + "\n").encode(),
        "media.json": json.dumps(media).encode(),
        "screens/0000.png": _PNG,
        "source/0000.json": b"{}",
    }


def _manifest(files: dict[str, bytes]) -> bytes:
    integrity = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    manifest = {
        "schema_version": "1.0",
        "app": {
            "bundle_id": "com.x.y",
            "appstore_id": None,
            "version": "1.9",
            "build": "59",
            "platform": "ios",
        },
        "device": "iPhone / iOS 16.7",
        "session": {"start": "2026-07-06T11:58:34", "end": "2026-07-06T11:58:54"},
        "counts": {"screens": 1, "requests": 1, "transactions": 1, "media": 1},
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
        "redaction": ["req_headers.Authorization"],
        "integrity": {"algo": "sha256", "files": integrity},
    }
    return json.dumps(manifest).encode()


def _build_archive(path: Path, *, tamper: str | None = None, slip: bool = False) -> Path:
    files = _archive_files()
    manifest = _manifest(files)
    if tamper is not None:
        files[tamper] = files[tamper] + b"tampered"  # bytes now disagree with manifest hash
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", manifest)
        if slip:
            zf.writestr("../evil.txt", b"x")
    return path


def _run_paths(tmp_path: Path) -> RunPaths:
    return RunPaths.create(tmp_path / "run")


def test_ingest_happy_path(tmp_path: Path) -> None:
    archive = _build_archive(tmp_path / "a.zip")
    paths = _run_paths(tmp_path)
    screen_map = frida_ingest.ingest_archive(archive, paths)

    assert screen_map["package"] == "com.x.y"
    assert [s["id"] for s in screen_map["screens"]] == ["0000"]
    # canonical mapping: accessibility_id -> resource_id, view_controller -> activity
    el = screen_map["screens"][0]["elements"][0]
    assert el["resource_id"] == "syn_724" and el["text"] == "Live"
    assert el["id_synthetic"] is True
    assert screen_map["screens"][0]["activity"] == "HomeController"
    assert screen_map["screens"][0]["native_ads"] == []
    # artifacts laid into RunPaths
    assert (paths.screens_dir / "0000.png").is_file()
    assert paths.screens_json.is_file() and paths.network_jsonl.is_file()
    assert (paths.source_dir / "0000.json").is_file()
    # per-screen network index built, status coerced to int
    index = json.loads(paths.network_index_json.read_text())
    assert index["0000"]["requests"][0]["status"] == 200
    assert index["0000"]["bodies"] == ["r1"]
    assert index["0000"]["media"][0]["content_type"] == "image/jpeg"
    # scratch removed
    assert not (paths.run_dir / "_frida_raw").exists()


def test_coerce_status_normalizes_string_and_non_http() -> None:
    records = [
        {"status": "200"},  # delegate/completion stringified -> int
        {"status": 0},  # no NSHTTPURLResponse -> null
        {"status": 304},  # valid, untouched
        {"status": None},  # request_only, untouched
    ]
    frida_ingest._coerce_status(records)
    assert [r["status"] for r in records] == [200, None, 304, None]


def test_ingest_rejects_integrity_tamper(tmp_path: Path) -> None:
    archive = _build_archive(tmp_path / "a.zip", tamper="network.jsonl")
    paths = _run_paths(tmp_path)
    with pytest.raises(FridaIngestError, match="sha256 mismatch"):
        frida_ingest.ingest_archive(archive, paths)
    # scratch is cleaned even when ingestion fails mid-way (try/finally)
    assert not (paths.run_dir / "_frida_raw").exists()


def test_ingest_rejects_zip_slip(tmp_path: Path) -> None:
    archive = _build_archive(tmp_path / "a.zip", slip=True)
    with pytest.raises(FridaIngestError, match="unsafe path"):
        frida_ingest.ingest_archive(archive, _run_paths(tmp_path))


def test_ingest_rejects_extra_file(tmp_path: Path) -> None:
    files = _archive_files()
    manifest = _manifest(files)  # integrity computed WITHOUT the extra file
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", manifest)
        zf.writestr("stowaway.json", b"{}")
    with pytest.raises(FridaIngestError, match="file-set mismatch"):
        frida_ingest.ingest_archive(archive, _run_paths(tmp_path))


_TTF = b"\x00\x01\x00\x00ttf-bytes"
_MP4 = b"\x00\x00\x00\x18ftypmp42"


def _rich_archive_files() -> dict[str, bytes]:
    files = _archive_files()
    files["source/0000.json"] = json.dumps(
        {
            "scale": 3.0,
            "screen_size": {"w": 393, "h": 852},
            "safe_area_insets": {"top": 59, "left": 0, "bottom": 34, "right": 0},
            "node_count": 2,
            "root": {
                "class": "UIView",
                "frame": {"x": 0, "y": 0, "w": 393, "h": 852},
                "children": [
                    {
                        "class": "UILabel",
                        "frame": {"x": 16, "y": 100, "w": 200, "h": 24},
                        "text": "Live",
                        "font": {
                            "postscript_name": "Urbanist-Bold",
                            "point_size": 17,
                            "weight": 700,
                        },
                        "colors": {"text": "#111111FF"},
                    }
                ],
            },
        }
    ).encode()
    files["fonts.json"] = json.dumps(
        [
            {
                "postscript_name": ".SFUI-Regular",
                "family": ".AppleSystemUIFont",
                "file": None,
                "is_system": True,
            },
            {
                "postscript_name": "Urbanist-Bold",
                "family": "Urbanist",
                "file": "fonts/Urbanist-Bold.ttf",
                "is_system": False,
            },
        ]
    ).encode()
    files["fonts/Urbanist-Bold.ttf"] = _TTF
    _SPLASH = b"\x00\x00\x00\x18ftypisomsplash"
    files["media.json"] = json.dumps(
        [
            {
                "id": "m1",
                "source": "network",
                "url": "https://cdn/clip.mp4",
                "request_id": "r1",
                "screen": "0000",
                "kind": "video",
                "content_type": "video/mp4",
                "signed_url": False,
                "path": "media/clip.mp4",
                "sha256": hashlib.sha256(_MP4).hexdigest(),
                "bytes": len(_MP4),
                "role": None,
            },
            {
                "id": "m2",
                "source": "bundle",
                "request_id": None,
                "screen": None,
                "kind": "video",
                "path": "media/splash_intro.mp4",
                "sha256": hashlib.sha256(_SPLASH).hexdigest(),
                "bytes": len(_SPLASH),
                "role": "splash_background",
            },
        ]
    ).encode()
    files["media/clip.mp4"] = _MP4
    files["media/splash_intro.mp4"] = _SPLASH
    # join the network clip to its node so the consumer can resolve node -> bytes
    src = json.loads(files["source/0000.json"])
    src["root"]["children"][0]["node_id"] = "n1"
    src["root"]["children"][0]["asset_ref"] = {"media_id": "m1", "resolved": True}
    files["source/0000.json"] = json.dumps(src).encode()
    files["subscriptions.json"] = json.dumps(
        {
            "products": [{"id": "premium.month", "price": "$9.99", "period": "P1M"}],
            "purchase_attempts": [],
        }
    ).encode()
    files["sdks.json"] = json.dumps(
        {"sdks": [{"sdk": "Google AdMob", "evidence": ["googleads.g.doubleclick.net"]}]}
    ).encode()
    files["ads_raw.json"] = json.dumps(
        {"ready_hooks": {"adblock": ["GADInterstitialAd -presentFromRootViewController:"]}}
    ).encode()
    return files


def _rich_manifest(files: dict[str, bytes]) -> bytes:
    manifest = json.loads(_manifest(files))
    manifest["schema_version"] = "1.1"
    manifest["capabilities"].update(
        {
            "view_hierarchy": "uikit-rich",
            "fonts": True,
            "media_bytes": True,
            "layout_geometry": True,
            "design_tokens": True,
            "media_source": ["network", "bundle", "runtime-snapshot"],
        }
    )
    manifest["counts"].update({"media_bundle": 1, "media_network": 1, "media_runtime": 0})
    manifest["integrity"]["files"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()
    }
    return json.dumps(manifest).encode()


def _build_rich_archive(path: Path, *, drop_font_bytes: bool = False) -> Path:
    files = _rich_archive_files()
    manifest = _rich_manifest(files)
    if drop_font_bytes:
        files.pop("fonts/Urbanist-Bold.ttf")
        manifest = _rich_manifest(files)  # recompute integrity without the dropped file
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", manifest)
    return path


def test_ingest_v11_materializes_fonts_media_and_rich_source(tmp_path: Path) -> None:
    archive = _build_rich_archive(tmp_path / "rich.zip")
    paths = _run_paths(tmp_path)
    frida_ingest.ingest_archive(archive, paths)

    assert paths.fonts_json.is_file()
    assert (paths.fonts_dir / "Urbanist-Bold.ttf").read_bytes() == _TTF
    assert (paths.media_dir / "clip.mp4").read_bytes() == _MP4
    assert paths.media_json.is_file()
    src = json.loads((paths.source_dir / "0000.json").read_text())
    assert src["root"]["children"][0]["font"]["postscript_name"] == "Urbanist-Bold"
    assert not (paths.run_dir / "_frida_raw").exists()


def test_ingest_v11_materializes_bundle_media_and_indexes_source(tmp_path: Path) -> None:
    archive = _build_rich_archive(tmp_path / "rich.zip")
    paths = _run_paths(tmp_path)
    frida_ingest.ingest_archive(archive, paths)

    # bundle bytes (splash) with screen:null still land on disk, byte-accurate
    assert (paths.media_dir / "splash_intro.mp4").is_file()
    # network media stays indexed per-screen, now carrying id/source/role/path
    index = json.loads(paths.network_index_json.read_text())
    clip = index["0000"]["media"][0]
    assert clip["id"] == "m1" and clip["source"] == "network" and clip["path"] == "media/clip.mp4"
    # node -> bytes join survives ingest for downstream codegen
    node = json.loads((paths.source_dir / "0000.json").read_text())["root"]["children"][0]
    assert node["asset_ref"] == {"media_id": "m1", "resolved": True}
    # monetization / ads / sdk channels materialize for evidence-based analysis
    assert json.loads(paths.subscriptions_json.read_text())["products"][0]["id"] == "premium.month"
    assert json.loads(paths.sdks_json.read_text())["sdks"][0]["sdk"] == "Google AdMob"
    assert "GADInterstitialAd" in paths.ads_raw_json.read_text()


def test_ingest_v11_rejects_referenced_font_without_bytes(tmp_path: Path) -> None:
    archive = _build_rich_archive(tmp_path / "rich.zip", drop_font_bytes=True)
    with pytest.raises(FridaIngestError, match="fonts.json references missing file"):
        frida_ingest.ingest_archive(archive, _run_paths(tmp_path))
