"""CodegenTarget + BuildProvider implementations (Flutter->iOS first) (SPEC §5.5, §9).

Interfaces and domain types are in :mod:`iosforge.providers.base`. CodegenTarget
implementations register under
:attr:`~iosforge.providers.registry.ProviderKind.CODEGEN`; BuildProvider
implementations (``local-web`` / ``remote-ios``) under
:attr:`~iosforge.providers.registry.ProviderKind.BUILD`.
"""

from iosforge.providers.base import (
    BuildOptions,
    BuildProvider,
    BuildResult,
    BuildStatus,
    BuildTarget,
    CodegenResult,
    CodegenTarget,
    PromptBundle,
    ScreenSource,
)

__all__ = [
    "BuildOptions",
    "BuildProvider",
    "BuildResult",
    "BuildStatus",
    "BuildTarget",
    "CodegenResult",
    "CodegenTarget",
    "PromptBundle",
    "ScreenSource",
]
