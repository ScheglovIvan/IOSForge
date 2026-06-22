"""EmulatorProvider implementations (Firebase Test Lab first) (SPEC §5.4, §9).

Interface and domain types are in :mod:`iosforge.providers.base`; concrete
implementations register under
:attr:`~iosforge.providers.registry.ProviderKind.EMULATOR`.
"""

from iosforge.providers.base import (
    EmulatorProvider,
    ScreenShot,
    WalkthroughResult,
    WalkthroughStrategy,
)

__all__ = [
    "EmulatorProvider",
    "ScreenShot",
    "WalkthroughResult",
    "WalkthroughStrategy",
]
