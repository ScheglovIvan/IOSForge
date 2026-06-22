"""AndroidCatalogProvider implementations + registry (SPEC §5.2, §9).

Interface and domain types are declared in :mod:`iosforge.providers.base`;
concrete implementations (T-3.3 stub, T-5.2 clone Play Market) register under
:attr:`~iosforge.providers.registry.ProviderKind.CATALOG`.
"""

from iosforge.providers.base import (
    AndroidCatalogProvider,
    AppMetadata,
    CatalogCandidate,
)

__all__ = ["AndroidCatalogProvider", "AppMetadata", "CatalogCandidate"]
