"""ApkSourceProvider implementations + registry (SPEC §5.3, §9).

Interface and domain types are in :mod:`iosforge.providers.base`; concrete
implementations register under
:attr:`~iosforge.providers.registry.ProviderKind.APK_SOURCE`.
"""

from iosforge.providers.base import ApkDownload, ApkManifest, ApkSourceProvider

__all__ = ["ApkDownload", "ApkManifest", "ApkSourceProvider"]
