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

#: Navigation contract every generated app MUST honour (SPEC §5.5, SwiftUI pivot).
#:
#: To let the Vision Judge render a single screen headlessly on the iOS
#: Simulator, the generated app must accept a ``screen-id`` and open straight on
#: that screen, bypassing normal navigation. This replaced the old Flutter web
#: route ``/#/screen/:id``. A :class:`CodegenTarget` implementation must emit an
#: app whose ONLY mandatory headless entry point is the launch argument:
#:
#: * ``simctl launch --terminate-running-process <udid> <bundle> -screen-id <id>``
#:   — read from the UserDefaults argument domain
#:   (``UserDefaults.standard.string(forKey: "screen-id")``).
#:
#: The custom URL scheme ``iosforge://screen/<id>`` is OPTIONAL (manual QA only):
#: ``simctl openurl`` raises the system "Open in …?" dialog on cold and warm start
#: (iOS 17.2 and 26.5), so the Vision Judge, ``nav_audit`` and the worker never use it.
#:
#: In screen-id mode the app MUST:
#:
#: * skip onboarding / first-run gates;
#: * suppress ALL system permission prompts (notifications, ATT, location,
#:   camera, …) — they overlay the target screen;
#: * render data-dependent screens from bundled fixtures;
#: * present modal targets (``sheet`` / ``fullScreenCover``) as the target, with a
#:   correct navigation stack beneath them;
#: * render an explicit, detectable error screen for an unknown id — never a
#:   silent fallback to home.
#:
#: The app opens on the matching screen with no operator interaction so
#: ``simctl io <udid> screenshot`` captures exactly that screen. Reference
#: implementation: Phase 0 spike (DECISIONS 2026-10-09 "Контракт screen-id").
SCREEN_NAV_CONTRACT: str = (
    "generated app MUST open headlessly on a screen given the launch argument "
    "-screen-id <id> (the only mandatory entry point; iosforge://screen/<id> is "
    "optional, manual QA only), skipping onboarding, suppressing permission "
    "prompts, rendering from fixtures and showing an explicit error screen for "
    "an unknown id"
)

#: Simulator environment contract the worker MUST enforce before screenshotting
#: generated screens for the Vision Judge (complements :data:`SCREEN_NAV_CONTRACT`).
#:
#: * pin the environment once per job: ``simctl status_bar <udid> override``
#:   (time / battery / network), appearance (light / dark) and the simulator
#:   locale / region / language set to the language of THIS job's original app —
#:   derived per job, never hard-coded (an unpinned simulator keeps its own default
#:   locale and the judge would compare screens across different locales);
#: * relaunch per screen with ``simctl launch --terminate-running-process``;
#: * wait for a stable frame instead of a fixed sleep: capture until two
#:   consecutive screenshots differ by < 0.5 % of pixels (the spike settled at
#:   ~0.7 s after launch);
#: * compare frames with a pixel / perceptual diff with tolerance, never a byte
#:   hash (status bar / home indicator anti-aliasing breaks md5 equality).
SIMULATOR_ENV_CONTRACT: str = (
    "worker pins status bar, appearance and the simulator locale/region/language "
    "to the job's original-app language, relaunches with "
    "--terminate-running-process per screen and waits until two consecutive "
    "screenshots differ by < 0.5 % of pixels (tolerant pixel diff, no byte hash)"
)

#: Capability contract (Level 2): how a generated app gets REAL functionality.
#:
#: A capability declared in ``app_spec.capabilities`` and routed by the scope stage
#: to tier 2 is implemented by a registry module
#: (:mod:`iosforge.mvp.swiftui_capabilities`), never by screen code:
#:
#: * the module is contract code under its scaffold directory (``App/Capabilities``,
#:   ``App/Monetization`` for the core modules), restored byte-for-byte; any SwiftPM
#:   package it needs is linked by the scaffold and imported ONLY there;
#: * screens call the module's Swift API (its screen rule is injected into every
#:   screen prompt) and never import the SDK or the system framework it wraps;
#: * headless screen-id mode never starts a module (screens show fixtures);
#: * a module may ship a ``mock`` and a ``functional_check``: functional
#:   verification proves the integration against that MOCK, not against real
#:   hardware or a real backend.
#:
#: Tier 1 capabilities stay in screen code (simple system API), tier 3 is a custom
#: core written by a human, tier 4 is not built (flagged to the operator).
CAPABILITY_CONTRACT: str = (
    "tier-2 capabilities are registry modules: contract code under App/Capabilities "
    "(core: App/Monetization), SDKs linked and imported only there, screens call the "
    "module API, headless never starts a module, functional checks run against the "
    "module's mock"
)

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
    target_kind: str = "swiftui-ios"  # e.g. 'swiftui-ios'
    decisions: dict[str, object] = Field(default_factory=dict)  # admin/content needed?
    logs: str | None = None


class BuildTarget(StrEnum):
    """What we compile (SPEC §5.5): a Simulator build for the Vision Judge
    self-test, a signed iOS archive for the final App Store build."""

    SIMULATOR = "simulator"
    IOS_ARCHIVE = "ios_archive"


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
        """Implementation key (e.g. ``firebase``, ``swiftui-ios``) for ProviderConfig."""
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

    Implementations: ``simulator`` (``xcodebuild`` + iOS Simulator for the Vision
    Judge self-test) and ``xcodebuild-archive`` (final signed IPA via
    ``xcodebuild archive``/``-exportArchive``). Aligned with the BuildProvider
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
    "CAPABILITY_CONTRACT",
    "SCREEN_NAV_CONTRACT",
    "SIMULATOR_ENV_CONTRACT",
    "ScreenShot",
    "ScreenSource",
    "WalkthroughResult",
    "WalkthroughStrategy",
]
