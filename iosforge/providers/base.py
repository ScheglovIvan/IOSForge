"""Provider interfaces (Protocols) + shared domain types (T-3.1, SPEC §9).

Each external capability of the pipeline is declared here as a
:class:`typing.Protocol` so the orchestrator/services depend on the *interface*,
not on a concrete class. Concrete implementations live under
``iosforge/providers/<kind>/`` (T-3.3 onward) and register themselves with the
:class:`~iosforge.providers.registry.ProviderRegistry`.

Interfaces (SPEC §):
* :class:`AndroidCatalogProvider`  — §5.2 (find Android analogue, ranked)
* :class:`ApkSourceProvider`       — §5.3 (search/download APK, prioritised + fallback)
* :class:`EmulatorProvider`        — §5.4 (install / walkthrough / teardown)
* :class:`CodegenTarget`           — §5.5/§11 (generate sources from a screen source)
* :class:`BuildProvider`           — §5.5 (build/status/artifact/logs)
* :class:`PromptSetProvider`       — §6   (versioned prompt sets)

``ComplianceMetric`` (§5.5/§9) is intentionally **not** declared here — it is
T-9.1 — but it will register under :attr:`ProviderKind.COMPLIANCE` via the same
registry mechanism.

Reuse note: the storage type :class:`~iosforge.storage.ArtifactRef` is reused
verbatim (not duplicated) wherever a provider returns a stored object — it is
the single Job-scoped artifact descriptor (SPEC §10).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from iosforge.storage import ArtifactRef

# --------------------------------------------------------------------------- #
# Shared domain types (Pydantic). Inputs/outputs of the provider interfaces.
# --------------------------------------------------------------------------- #


class AppMetadata(BaseModel):
    """Extracted metadata of the source iOS app (SPEC §5.2 input)."""

    source_ref: str  # link / bundle id / store id
    name: str | None = None
    publisher: str | None = None
    category: str | None = None
    description: str | None = None
    icon_ref: str | None = None  # storage key / URL of the icon


class CatalogCandidate(BaseModel):
    """A ranked Android analogue of the source app (SPEC §5.2 output)."""

    package_id: str
    title: str | None = None
    publisher: str | None = None
    score: float = Field(ge=0.0, le=1.0)  # match confidence
    source: str | None = None  # which catalog yielded it
    extra: dict[str, object] = Field(default_factory=dict)


class ApkManifest(BaseModel):
    """Integrity/provenance manifest of a downloaded APK (SPEC §5.3 output)."""

    package_id: str
    version: str | None = None
    source: str | None = None  # resource the APK came from
    sha256: str | None = None
    size_bytes: int | None = None


class ApkDownload(BaseModel):
    """Result of an APK acquisition: stored artifact + manifest (SPEC §5.3)."""

    artifact_ref: ArtifactRef
    manifest: ApkManifest


class WalkthroughStrategy(BaseModel):
    """Tunable walkthrough parameters (SPEC §5.4; defaults in Settings/DECISIONS Q3)."""

    max_screens: int = Field(default=40, gt=0)
    max_depth: int = Field(default=6, gt=0)
    job_timeout_s: int = Field(default=1200, gt=0)
    action_timeout_s: int = Field(default=15, gt=0)
    # Behaviour on login/registration/payment screens (DECISIONS Q2).
    on_auth_screen: str = "screenshot_skip"
    on_payment_screen: str = "hard_stop"
    extra: dict[str, object] = Field(default_factory=dict)


class ScreenShot(BaseModel):
    """A single captured screen (SPEC §5.4)."""

    screen_id: str
    artifact_ref: ArtifactRef  # the stored screenshot
    title: str | None = None


class WalkthroughResult(BaseModel):
    """Output of a walkthrough: screens, screen map (graph), logs (SPEC §5.4)."""

    screens: list[ScreenShot] = Field(default_factory=list)
    screen_map: dict[str, object] = Field(default_factory=dict)  # navigation graph
    logs: str | None = None
    partial: bool = False  # True when a limit was hit (not a failure, §5.4/Q3)


class ScreenSource(BaseModel):
    """Input to codegen: where screens come from (SPEC §5.5/§11).

    First phase = walkthrough screenshots + screen map. Future (§11): a folder of
    designer iOS mockups / Figma — same interface, different ``kind``.
    """

    kind: str = "walkthrough"  # 'walkthrough' | 'designer' (future, §11)
    screenshots: list[ArtifactRef] = Field(default_factory=list)
    screen_map: dict[str, object] = Field(default_factory=dict)


class PromptBundle(BaseModel):
    """The prompt set version handed to codegen (SPEC §6)."""

    prompt_set: str
    version: int
    bodies: dict[str, str] = Field(default_factory=dict)  # purpose -> prompt body


class CodegenResult(BaseModel):
    """Generated sources + decisions (SPEC §5.5 output)."""

    source_ref: ArtifactRef  # generated sources in the store
    target_kind: str  # e.g. 'flutter-ios'
    decisions: dict[str, object] = Field(default_factory=dict)  # admin/content needed?
    logs: str | None = None


class BuildTarget(StrEnum):
    """What we compile (SPEC §5.5): web for self-test, ios for the final build."""

    WEB = "web"
    IOS = "ios"


class BuildStatus(StrEnum):
    """Build lifecycle (SPEC §5.5; aligned with the orchestrator stage matrix)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BuildOptions(BaseModel):
    """Build options (SPEC §5.5; signing deferred on phase 1, §1.2)."""

    signed: bool = False
    workflow_id: str | None = None
    timeout_s: int | None = None
    env: dict[str, str] = Field(default_factory=dict)


class BuildResult(BaseModel):
    """Outcome of a build request (SPEC §5.5)."""

    build_id: str
    status: BuildStatus
    artifact_ref: ArtifactRef | None = None
    logs: str | None = None


class PromptSetVersion(BaseModel):
    """A resolved prompt-set version (SPEC §6)."""

    prompt_set: str
    version: int
    bodies: dict[str, str] = Field(default_factory=dict)  # purpose -> body
    author: str | None = None


# --------------------------------------------------------------------------- #
# Provider interfaces (Protocols). Implementations register in the registry.
# --------------------------------------------------------------------------- #


@runtime_checkable
class Provider(Protocol):
    """Common provider surface: a stable implementation key for ProviderConfig."""

    def name(self) -> str:
        """Implementation key (e.g. ``firebase``, ``flutter-ios``) for ProviderConfig."""
        ...


@runtime_checkable
class AndroidCatalogProvider(Protocol):
    """Find the Android analogue of an iOS app (SPEC §5.2).

    Swappable: clone Play Market today, another catalog tomorrow.
    """

    def name(self) -> str: ...

    def search(self, app: AppMetadata, *, limit: int = 10) -> list[CatalogCandidate]:
        """Return ranked Android candidates (best first) for ``app``."""
        ...


@runtime_checkable
class ApkSourceProvider(Protocol):
    """Search/download an APK by package id (SPEC §5.3).

    A provider fronts an ordered list of resources with fallback when one is
    unavailable; integrity (hash/version/size) is captured in the manifest.
    """

    def name(self) -> str: ...

    def find(self, package_id: str) -> list[ApkManifest]:
        """Locate available APK builds for ``package_id`` across its resources."""
        ...

    def download(self, package_id: str, *, version: str | None = None) -> ApkDownload:
        """Download (with fallback) and store the APK; return ref + manifest."""
        ...


@runtime_checkable
class EmulatorProvider(Protocol):
    """Walk an APK on an online Android emulator (SPEC §5.4).

    First implementation: Firebase Test Lab. Swapping = a new implementation of
    this Protocol + a ProviderConfig row, nothing else (SPEC §5.4/§9).
    """

    def name(self) -> str: ...

    def install(self, apk: ArtifactRef) -> None:
        """Install the APK on the emulator (prepare a session)."""
        ...

    def walkthrough(self, strategy: WalkthroughStrategy) -> WalkthroughResult:
        """Walk the screens; return ``{screens, screen_map, logs}`` (SPEC §5.4)."""
        ...

    def teardown(self) -> None:
        """Release the emulator session / sandbox."""
        ...


@runtime_checkable
class CodegenTarget(Protocol):
    """Generate target-platform **sources** from a screen source (SPEC §5.5/§11).

    Does NOT compile — sources go to a :class:`BuildProvider`. Aligned with the
    CodegenTarget contract in API_MAP (T-2.x).
    """

    def name(self) -> str: ...

    def generate(
        self,
        screens: ScreenSource,
        prompts: PromptBundle,
        workdir: Path,
    ) -> CodegenResult:
        """Generate sources for the target platform from screens + prompts."""
        ...


@runtime_checkable
class BuildProvider(Protocol):
    """Compile/build generated sources for a target (SPEC §5.5).

    Implementations: ``local-web`` (Flutter web/Chrome self-test) and
    ``remote-ios`` (Codemagic, final Flutter->iOS). Aligned with the BuildProvider
    contract already in API_MAP.
    """

    def name(self) -> str: ...

    def build(
        self,
        source: ArtifactRef,
        target: BuildTarget,
        opts: BuildOptions | None = None,
    ) -> BuildResult:
        """Start a build of ``source`` for ``target`` (sync waits, async returns id)."""
        ...

    def status(self, build_id: str) -> BuildStatus:
        """Poll the state of a build (for async/remote builds)."""
        ...

    def artifact(self, build_id: str) -> ArtifactRef:
        """Reference to the finished artifact (.ipa/.app or web bundle)."""
        ...

    def logs(self, build_id: str) -> str:
        """Build logs for diagnostics/admin."""
        ...

    def cancel(self, build_id: str) -> None:
        """Cancel a running remote build (optional)."""
        ...


@runtime_checkable
class PromptSetProvider(Protocol):
    """Resolve versioned Claude Code prompt sets (SPEC §6).

    Prompt sets are edited/versioned in the admin; the worker (§5.5) gets the
    active version's bodies to drop into the task workdir.
    """

    def name(self) -> str: ...

    def get(self, prompt_set: str, *, version: int | None = None) -> PromptSetVersion:
        """Return the requested (or latest) version of ``prompt_set``."""
        ...

    def list_versions(self, prompt_set: str) -> list[int]:
        """All version numbers of ``prompt_set`` (newest first)."""
        ...


__all__ = [
    "AndroidCatalogProvider",
    "ApkDownload",
    "ApkManifest",
    "ApkSourceProvider",
    "AppMetadata",
    "BuildOptions",
    "BuildProvider",
    "BuildResult",
    "BuildStatus",
    "BuildTarget",
    "CatalogCandidate",
    "CodegenResult",
    "CodegenTarget",
    "EmulatorProvider",
    "PromptBundle",
    "PromptSetProvider",
    "PromptSetVersion",
    "Provider",
    "ScreenShot",
    "ScreenSource",
    "WalkthroughResult",
    "WalkthroughStrategy",
]
