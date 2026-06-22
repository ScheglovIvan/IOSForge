"""Swappable provider abstractions (Protocol/ABC) + registry (SPEC §9).

Public surface (T-3.1; contract documented by T-3.2 in API_MAP):

* :class:`ProviderRegistry` / :data:`registry` / :class:`ProviderKind` — the
  registry-factory that resolves the active implementation by a string from
  ``ProviderConfig``/config, without touching the orchestrator.
* The provider Protocols + their domain types (see :mod:`iosforge.providers.base`).

Concrete implementations live under ``iosforge/providers/<kind>/`` and register
against :data:`registry` (T-3.3 onward).
"""

from iosforge.providers.base import (
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
    Provider,
    ScreenShot,
    ScreenSource,
    WalkthroughResult,
    WalkthroughStrategy,
)
from iosforge.providers.registry import (
    ProviderAlreadyRegistered,
    ProviderError,
    ProviderKind,
    ProviderNotRegistered,
    ProviderRegistry,
    registry,
)

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
    "ProviderAlreadyRegistered",
    "ProviderError",
    "ProviderKind",
    "ProviderNotRegistered",
    "ProviderRegistry",
    "ScreenShot",
    "ScreenSource",
    "WalkthroughResult",
    "WalkthroughStrategy",
    "registry",
]
