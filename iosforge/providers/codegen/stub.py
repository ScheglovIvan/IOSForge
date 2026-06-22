"""Deterministic stubs for :class:`CodegenTarget` and :class:`BuildProvider`
(T-3.3, SPEC §5.5/§9).

Both abstractions live in this subpackage (codegen kind + build kind). The
codegen stub fabricates a :class:`CodegenResult` pointing at a fixture sources
ref (no Claude CLI call); the build stub fabricates an in-memory ``SUCCEEDED``
:class:`BuildResult` and keeps a per-instance ``build_id -> result`` map so
``status``/``artifact``/``logs`` stay consistent. No real compilation, no
Codemagic / Flutter toolchain is invoked.

Registered under ``(codegen, "stub")`` and ``(build, "stub")``.
"""

from __future__ import annotations

from pathlib import Path

from iosforge.providers.base import (
    BuildOptions,
    BuildResult,
    BuildStatus,
    BuildTarget,
    CodegenResult,
    PromptBundle,
    ScreenSource,
)
from iosforge.providers.registry import ProviderKind, registry
from iosforge.storage import ArtifactRef

STUB_NAME = "stub"


# --------------------------------------------------------------------------- #
# CodegenTarget stub
# --------------------------------------------------------------------------- #
class StubCodegenTarget:
    """Fixture codegen: returns a CodegenResult referencing stub sources."""

    def name(self) -> str:
        return STUB_NAME

    def generate(
        self,
        screens: ScreenSource,
        prompts: PromptBundle,
        workdir: Path,
    ) -> CodegenResult:
        source_ref = ArtifactRef(
            bucket="iosforge",
            key="jobs/stub/sources/stub-sources.zip",
            size=64,
            content_type="application/zip",
            kind="sources",
            job_id="stub",
        )
        return CodegenResult(
            source_ref=source_ref,
            target_kind="flutter-ios",
            decisions={"needs_admin": False, "needs_content": False},
            logs=(
                f"stub codegen: prompt_set={prompts.prompt_set} v{prompts.version}, "
                f"{len(screens.screenshots)} screens"
            ),
        )


# --------------------------------------------------------------------------- #
# BuildProvider stub
# --------------------------------------------------------------------------- #
class StubBuildProvider:
    """Fixture build provider: synchronous SUCCEEDED builds, in-memory state."""

    def __init__(self) -> None:
        self._builds: dict[str, BuildResult] = {}

    def name(self) -> str:
        return STUB_NAME

    def build(
        self,
        source: ArtifactRef,
        target: BuildTarget,
        opts: BuildOptions | None = None,
    ) -> BuildResult:
        build_id = f"stub-build-{len(self._builds) + 1}"
        ext = "zip" if target is BuildTarget.WEB else "ipa"
        artifact_ref = ArtifactRef(
            bucket="iosforge",
            key=f"jobs/stub/builds/{build_id}.{ext}",
            size=128,
            content_type="application/octet-stream",
            kind="build",
            job_id="stub",
        )
        result = BuildResult(
            build_id=build_id,
            status=BuildStatus.SUCCEEDED,
            artifact_ref=artifact_ref,
            logs=f"stub build: target={target} succeeded",
        )
        self._builds[build_id] = result
        return result

    def _require(self, build_id: str) -> BuildResult:
        try:
            return self._builds[build_id]
        except KeyError:
            raise KeyError(f"unknown build_id={build_id!r}") from None

    def status(self, build_id: str) -> BuildStatus:
        return self._require(build_id).status

    def artifact(self, build_id: str) -> ArtifactRef:
        result = self._require(build_id)
        if result.artifact_ref is None:  # pragma: no cover - stub always sets it
            raise KeyError(f"no artifact for build_id={build_id!r}")
        return result.artifact_ref

    def logs(self, build_id: str) -> str:
        return self._require(build_id).logs or ""

    def cancel(self, build_id: str) -> None:
        # Finished stub builds cannot be cancelled; validate the id and no-op.
        self._require(build_id)
        return None


registry.add(ProviderKind.CODEGEN, STUB_NAME, StubCodegenTarget, override=True)
registry.add(ProviderKind.BUILD, STUB_NAME, StubBuildProvider, override=True)
