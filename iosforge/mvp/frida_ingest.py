"""Ingest a Frida capture archive (contract v1.0) into a run directory.

Deterministic adapter that sits where ``video_frames``/``crawl`` sit: it unpacks
the untrusted archive (zip-slip + decompression-bomb guarded), re-verifies the
``manifest`` sha256 integrity, validates the whole archive against
:mod:`iosforge.mvp.frida_contract` (coercing the occasionally-stringified
``status`` to int first), lays the screenshots + raw channels into the
:class:`~iosforge.mvp.paths.RunPaths` layout, writes a canonical ``screens.json``
for Stage B analysis and a per-screen ``network_index.json``. Returns the screen
map for ``WalkthroughResult``. Capabilities-aware: with ``network: "none"`` the
index is simply empty and the backend contract stays inferred.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from iosforge.mvp import frida_contract
from iosforge.mvp.paths import RunPaths

_MAX_FILES = 2000
_MAX_UNCOMPRESSED = 1024 * 1024 * 1024


class FridaIngestError(RuntimeError):
    """Raised when an archive is malformed, tampered or fails the contract."""


def _safe_extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > _MAX_FILES:
            raise FridaIngestError(f"archive has too many files ({len(infos)} > {_MAX_FILES})")
        total = 0
        for info in infos:
            target = (dest / info.filename).resolve()
            if root != target and root not in target.parents:
                raise FridaIngestError(f"unsafe path in archive: {info.filename!r}")
            total += info.file_size
            if total > _MAX_UNCOMPRESSED:
                raise FridaIngestError("archive exceeds the uncompressed size cap")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _verify_integrity(src: Path, manifest: dict[str, Any]) -> None:
    declared: dict[str, str] = manifest["integrity"]["files"]
    present = {str(p.relative_to(src)) for p in src.rglob("*") if p.is_file()} - {"manifest.json"}
    missing = sorted(f for f in declared if not (src / f).is_file())
    extra = sorted(present - set(declared))
    if missing or extra:
        raise FridaIngestError(f"integrity file-set mismatch: missing={missing} extra={extra}")
    for rel, want in declared.items():
        got = hashlib.sha256((src / rel).read_bytes()).hexdigest()
        if got != want:
            raise FridaIngestError(f"integrity sha256 mismatch for {rel!r}")


def _verify_referenced_bytes(
    src: Path, fonts: list[dict[str, Any]] | None, media: list[dict[str, Any]] | None
) -> None:
    for font in fonts or []:
        rel = font.get("file")
        if rel and not (src / rel).is_file():
            raise FridaIngestError(f"fonts.json references missing file {rel!r}")
    for entry in media or []:
        rel = entry.get("path")
        if rel and not (src / rel).is_file():
            raise FridaIngestError(f"media.json references missing file {rel!r}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _coerce_status(records: list[dict[str, Any]]) -> None:
    for rec in records:
        status = rec.get("status")
        if isinstance(status, str) and status.isdigit():
            status = int(status)
            rec["status"] = status
        if isinstance(status, int) and not (100 <= status <= 599):
            rec["status"] = None


def _to_canonical_screens(raw: dict[str, Any]) -> dict[str, Any]:
    """Map the frida screens.json to a crawl-compatible shape for ``analyze``."""
    screens: list[dict[str, Any]] = []
    for sc in raw.get("screens", []):
        elements = [
            {
                "class": el.get("class"),
                "text": el.get("text"),
                "resource_id": el.get("accessibility_id"),
                "id_synthetic": el.get("id_synthetic"),
                "bounds": el.get("bounds"),
                "role": el.get("role"),
                "value": el.get("value"),
            }
            for el in sc.get("elements", [])
        ]
        screens.append(
            {
                "id": sc["id"],
                "screenshot": sc["screenshot"],
                "activity": sc.get("view_controller"),
                "ui_kind": sc.get("ui_kind"),
                "signature": sc.get("signature"),
                "texts": sc.get("texts", []),
                "elements": elements,
                "native_ads": sc.get("native_ads", []),
                "from": sc.get("from"),
                "navigates_to": sc.get("navigates_to", []),
            }
        )
    return {
        "package": raw.get("bundle_id"),
        "screen_count": raw.get("screen_count", len(screens)),
        "screens": screens,
    }


def _build_network_index(
    network: list[dict[str, Any]],
    bodies: list[dict[str, Any]],
    media: list[dict[str, Any]],
) -> dict[str, Any]:
    body_ids = {b["request_id"] for b in bodies}
    index: dict[str, dict[str, list[Any]]] = {}

    def _screen(sid: str) -> dict[str, list[Any]]:
        return index.setdefault(sid, {"requests": [], "bodies": [], "media": []})

    for rec in network:
        bucket = _screen(str(rec["screen"]))
        bucket["requests"].append(
            {
                "id": rec["id"],
                "method": rec["method"],
                "host": rec["host"],
                "path": rec["path"],
                "status": rec.get("status"),
                "mime": rec.get("mime"),
            }
        )
        ref = rec.get("resp_body_ref")
        if ref is not None and ref.split("#", 1)[-1] in body_ids:
            bucket["bodies"].append(ref.split("#", 1)[-1])
    for item in media:
        sid = str(item.get("screen") or "")
        if not sid:
            continue
        bucket = _screen(sid)
        bucket["media"].append(
            {
                "id": item.get("id"),
                "source": item.get("source"),
                "url": item.get("url"),
                "kind": item.get("kind"),
                "content_type": item.get("content_type"),
                "path": item.get("path"),
                "role": item.get("role"),
            }
        )
    return index


def ingest_archive(archive_path: Path, paths: RunPaths) -> dict[str, Any]:
    """Unpack + validate a Frida archive into ``paths``; return the screen map."""
    src = paths.run_dir / "_frida_raw"
    _safe_extract(archive_path, src)

    try:
        manifest_path = src / "manifest.json"
        screens_path = src / "screens.json"
        for required in (manifest_path, screens_path):
            if not required.is_file():
                raise FridaIngestError(f"archive missing {required.name}")
        manifest = json.loads(manifest_path.read_text())
        _verify_integrity(src, manifest)

        screens_raw = json.loads(screens_path.read_text())
        network = _read_jsonl(src / "network.jsonl")
        bodies = _read_jsonl(src / "json_bodies.jsonl")
        _coerce_status(network)
        media = (
            json.loads((src / "media.json").read_text()) if (src / "media.json").is_file() else []
        )
        caps = manifest.get("capabilities", {})
        fonts = (
            json.loads((src / "fonts.json").read_text())
            if caps.get("fonts") and (src / "fonts.json").is_file()
            else None
        )
        sources = (
            {sf.stem: json.loads(sf.read_text()) for sf in sorted((src / "source").glob("*.json"))}
            if caps.get("view_hierarchy") == "uikit-rich"
            else None
        )

        try:
            frida_contract.validate_archive(
                manifest=manifest,
                screens=screens_raw,
                network=network,
                json_bodies=bodies,
                fonts=fonts,
                media=media,
                sources=sources,
            )
        except frida_contract.FridaArchiveValidationError as exc:
            raise FridaIngestError(f"archive violates Frida contract: {exc}") from exc

        _verify_referenced_bytes(src, fonts, media)

        for sc in screens_raw.get("screens", []):
            shot = src / sc["screenshot"]
            if shot.is_file():
                (paths.screens_dir / Path(sc["screenshot"]).name).write_bytes(shot.read_bytes())
        if (src / "source").is_dir():
            shutil.copytree(src / "source", paths.source_dir, dirs_exist_ok=True)
        if (src / "fonts").is_dir():
            shutil.copytree(src / "fonts", paths.fonts_dir, dirs_exist_ok=True)
        if (src / "media").is_dir():
            shutil.copytree(src / "media", paths.media_dir, dirs_exist_ok=True)
        for name, dst in (
            ("network.jsonl", paths.network_jsonl),
            ("json_bodies.jsonl", paths.json_bodies_jsonl),
            ("media.json", paths.media_json),
            ("fonts.json", paths.fonts_json),
            ("subscriptions.json", paths.subscriptions_json),
            ("sdks.json", paths.sdks_json),
            ("ads_raw.json", paths.ads_raw_json),
        ):
            if (src / name).is_file():
                shutil.copy2(src / name, dst)

        canonical = _to_canonical_screens(screens_raw)
        paths.screens_json.write_text(json.dumps(canonical, indent=2, ensure_ascii=False))
        index = _build_network_index(network, bodies, media)
        paths.network_index_json.write_text(json.dumps(index, indent=2, ensure_ascii=False))
        return canonical
    finally:
        shutil.rmtree(src, ignore_errors=True)
