"""Deterministic stub for :class:`ApkSourceProvider` (T-3.3, SPEC §5.3/§9).

No real network/store access: ``find`` fabricates a manifest and ``download``
returns a tiny in-memory APK blob wrapped in an :class:`ArtifactRef` plus a
matching :class:`ApkManifest` (size/sha256 computed from the blob, so integrity
fields are internally consistent). Registered under ``(apk_source, "stub")``.
"""

from __future__ import annotations

import hashlib

from iosforge.providers.base import ApkDownload, ApkManifest
from iosforge.providers.registry import ProviderKind, registry
from iosforge.storage import ArtifactRef

STUB_NAME = "stub"

#: A tiny, deterministic fixture "APK" blob (ZIP local-file-header magic only).
_FAKE_APK_BLOB = b"PK\x03\x04stub-apk-fixture"


def _fixture_manifest(package_id: str, version: str | None) -> ApkManifest:
    sha256 = hashlib.sha256(_FAKE_APK_BLOB).hexdigest()
    return ApkManifest(
        package_id=package_id,
        version=version or "0.0.1",
        source=STUB_NAME,
        sha256=sha256,
        size_bytes=len(_FAKE_APK_BLOB),
    )


class StubApkSourceProvider:  # structural conformance to ApkSourceProvider
    """Fixture APK source: deterministic manifest + in-memory blob, no network."""

    def name(self) -> str:
        return STUB_NAME

    def find(self, package_id: str) -> list[ApkManifest]:
        return [_fixture_manifest(package_id, version="0.0.1")]

    def download(self, package_id: str, *, version: str | None = None) -> ApkDownload:
        manifest = _fixture_manifest(package_id, version)
        artifact_ref = ArtifactRef(
            bucket="iosforge",
            key=f"jobs/stub/apk/{package_id}-{manifest.version}.apk",
            size=len(_FAKE_APK_BLOB),
            content_type="application/vnd.android.package-archive",
            kind="apk",
            job_id="stub",
        )
        return ApkDownload(artifact_ref=artifact_ref, manifest=manifest)


registry.add(ProviderKind.APK_SOURCE, STUB_NAME, StubApkSourceProvider, override=True)
