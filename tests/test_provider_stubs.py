"""Contract tests for the provider *stub* implementations (T-3.3, SPEC §9).

Each swappable provider abstraction (catalog / apk_source / emulator / codegen /
build / promptset) ships a deterministic **stub** that the rest of the pipeline
(discovery / acquisition / walkthrough / codegen / delivery epics) can use as a
fixture while building under TDD.

What these tests pin (the DoD "контрактные тесты интерфейсов"):

* every stub is registered in the shared :data:`registry` under
  ``(kind, "stub")`` — so the orchestrator resolves it by a *string* from a
  ``ProviderConfig`` projection, never importing the concrete class;
* the resolved object structurally satisfies its ``@runtime_checkable``
  Protocol (``isinstance`` check);
* calling the interface methods yields the declared domain types with valid,
  deterministic content and **no** external (network/storage) side effects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Importing the stub packages registers their implementations against the shared
# registry (side-effect import, mirroring how services will pull a provider).
import iosforge.providers.apk.stub  # noqa: F401
import iosforge.providers.catalog.stub  # noqa: F401
import iosforge.providers.codegen.stub  # noqa: F401
import iosforge.providers.emulator.stub  # noqa: F401
import iosforge.providers.promptset.stub  # noqa: F401
from iosforge.providers import (
    AndroidCatalogProvider,
    ApkDownload,
    ApkManifest,
    ApkSourceProvider,
    AppMetadata,
    BuildOptions,
    BuildProvider,
    BuildResult,
    BuildStatus,
    BuildTarget,
    CatalogCandidate,
    CodegenResult,
    CodegenTarget,
    EmulatorProvider,
    PromptBundle,
    PromptSetProvider,
    PromptSetVersion,
    ProviderKind,
    ScreenShot,
    ScreenSource,
    WalkthroughResult,
    WalkthroughStrategy,
    registry,
)
from iosforge.storage import ArtifactRef

STUB = "stub"


# --------------------------------------------------------------------------- #
# Registration: every kind exposes a "stub" resolvable by string from config.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kind",
    [
        ProviderKind.CATALOG,
        ProviderKind.APK_SOURCE,
        ProviderKind.EMULATOR,
        ProviderKind.CODEGEN,
        ProviderKind.BUILD,
        ProviderKind.PROMPTSET,
    ],
)
def test_stub_registered_for_every_kind(kind: ProviderKind) -> None:
    assert registry.is_registered(kind, STUB)


@pytest.mark.parametrize(
    "kind",
    [
        ProviderKind.CATALOG,
        ProviderKind.APK_SOURCE,
        ProviderKind.EMULATOR,
        ProviderKind.CODEGEN,
        ProviderKind.BUILD,
        ProviderKind.PROMPTSET,
    ],
)
def test_stub_resolves_from_config_string(kind: ProviderKind) -> None:
    """The orchestrator path: ProviderConfig projection -> active impl by name."""
    config = {str(kind): STUB}
    provider = registry.resolve_from_config(kind, config)
    assert provider.name() == STUB


# --------------------------------------------------------------------------- #
# AndroidCatalogProvider stub (§5.2)
# --------------------------------------------------------------------------- #
def test_catalog_stub_contract() -> None:
    provider = registry.resolve(ProviderKind.CATALOG, STUB)
    assert isinstance(provider, AndroidCatalogProvider)

    app = AppMetadata(source_ref="ios://app/123", name="Acme", publisher="Acme Inc")
    candidates = provider.search(app, limit=3)

    assert candidates, "stub must return at least one candidate"
    assert all(isinstance(c, CatalogCandidate) for c in candidates)
    best = candidates[0]
    # Score derived from input metadata, within the declared [0, 1] bound.
    assert 0.0 <= best.score <= 1.0
    assert best.package_id
    # Deterministic: same input -> same output.
    assert provider.search(app, limit=3) == candidates


def test_catalog_stub_respects_limit() -> None:
    provider = registry.resolve(ProviderKind.CATALOG, STUB)
    app = AppMetadata(source_ref="ios://app/123", name="Acme")
    assert len(provider.search(app, limit=1)) <= 1


# --------------------------------------------------------------------------- #
# ApkSourceProvider stub (§5.3)
# --------------------------------------------------------------------------- #
def test_apk_stub_find_returns_manifests() -> None:
    provider = registry.resolve(ProviderKind.APK_SOURCE, STUB)
    assert isinstance(provider, ApkSourceProvider)

    manifests = provider.find("com.acme.app")
    assert manifests
    assert all(isinstance(m, ApkManifest) for m in manifests)
    assert manifests[0].package_id == "com.acme.app"


def test_apk_stub_download_returns_artifact_and_manifest() -> None:
    provider = registry.resolve(ProviderKind.APK_SOURCE, STUB)
    download = provider.download("com.acme.app", version="1.2.3")

    assert isinstance(download, ApkDownload)
    assert isinstance(download.artifact_ref, ArtifactRef)
    assert isinstance(download.manifest, ApkManifest)
    assert download.manifest.package_id == "com.acme.app"
    assert download.manifest.version == "1.2.3"
    # Manifest integrity fields reflect the fixture blob (no real network).
    assert download.manifest.size_bytes == download.artifact_ref.size
    assert download.manifest.sha256


# --------------------------------------------------------------------------- #
# EmulatorProvider stub (§5.4)
# --------------------------------------------------------------------------- #
def test_emulator_stub_contract() -> None:
    provider = registry.resolve(ProviderKind.EMULATOR, STUB)
    assert isinstance(provider, EmulatorProvider)

    apk = ArtifactRef(
        bucket="iosforge",
        key="jobs/j1/apk/app.apk",
        size=10,
        content_type="application/vnd.android.package-archive",
    )
    # install / teardown are no-ops that must not raise.
    assert provider.install(apk) is None

    result = provider.walkthrough(WalkthroughStrategy())
    assert isinstance(result, WalkthroughResult)
    assert result.screens, "stub must yield at least one screen"
    assert all(isinstance(s, ScreenShot) for s in result.screens)
    # screen_map references the captured screens (minimal navigation graph).
    assert result.screen_map
    assert result.logs

    assert provider.teardown() is None


# --------------------------------------------------------------------------- #
# CodegenTarget stub (§5.5/§11)
# --------------------------------------------------------------------------- #
def test_codegen_stub_contract(tmp_path: Path) -> None:
    provider = registry.resolve(ProviderKind.CODEGEN, STUB)
    assert isinstance(provider, CodegenTarget)

    screens = ScreenSource(kind="walkthrough", screenshots=[], screen_map={"nodes": []})
    prompts = PromptBundle(prompt_set="default", version=1, bodies={"codegen": "build it"})

    result = provider.generate(screens, prompts, tmp_path)
    assert isinstance(result, CodegenResult)
    assert isinstance(result.source_ref, ArtifactRef)
    assert result.target_kind


# --------------------------------------------------------------------------- #
# BuildProvider stub (§5.5)
# --------------------------------------------------------------------------- #
def test_build_stub_contract() -> None:
    provider = registry.resolve(ProviderKind.BUILD, STUB)
    assert isinstance(provider, BuildProvider)

    source = ArtifactRef(
        bucket="iosforge",
        key="jobs/j1/sources/app.zip",
        size=42,
        content_type="application/zip",
    )
    result = provider.build(source, BuildTarget.WEB, BuildOptions())
    assert isinstance(result, BuildResult)
    assert result.status is BuildStatus.SUCCEEDED
    assert isinstance(result.artifact_ref, ArtifactRef)

    # status / artifact / logs are consistent for the returned build_id.
    assert provider.status(result.build_id) is BuildStatus.SUCCEEDED
    assert provider.artifact(result.build_id) == result.artifact_ref
    assert provider.logs(result.build_id)
    # cancel on a finished build is a no-op (must not raise).
    assert provider.cancel(result.build_id) is None


def test_build_stub_unknown_build_id_raises() -> None:
    provider = registry.resolve(ProviderKind.BUILD, STUB)
    with pytest.raises(KeyError):
        provider.status("does-not-exist")


# --------------------------------------------------------------------------- #
# PromptSetProvider stub (§6)
# --------------------------------------------------------------------------- #
def test_promptset_stub_contract() -> None:
    provider = registry.resolve(ProviderKind.PROMPTSET, STUB)
    assert isinstance(provider, PromptSetProvider)

    version = provider.get("default")
    assert isinstance(version, PromptSetVersion)
    assert version.prompt_set == "default"
    assert version.bodies

    versions = provider.list_versions("default")
    assert versions
    assert version.version in versions
