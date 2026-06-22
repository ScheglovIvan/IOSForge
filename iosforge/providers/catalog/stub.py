"""Deterministic stub for :class:`AndroidCatalogProvider` (T-3.3, SPEC §5.2/§9).

A no-network fixture that fabricates ranked :class:`CatalogCandidate` rows from
the input :class:`AppMetadata`. It exists so the discovery epic (E5) and the
orchestrator can be built/tested under TDD before the real Play-Market clone
(T-5.2) lands. Registered under ``(catalog, "stub")``.
"""

from __future__ import annotations

import hashlib

from iosforge.providers.base import AppMetadata, CatalogCandidate
from iosforge.providers.registry import ProviderKind, registry

#: Registration key (see :class:`StubAndroidCatalogProvider.name`).
STUB_NAME = "stub"


def _score_from(app: AppMetadata, rank: int) -> float:
    """Derive a stable [0, 1] match score from the metadata + rank.

    Pure function of the inputs so ``search`` is deterministic: a digest of the
    source app's identity seeds the top score; lower ranks decay linearly.
    """
    digest = hashlib.sha256(
        f"{app.source_ref}|{app.name}|{app.publisher}".encode()
    ).digest()
    base = 0.6 + (digest[0] / 255.0) * 0.4  # in [0.6, 1.0]
    score = base - rank * 0.1
    return round(max(0.0, min(1.0, score)), 4)


class StubAndroidCatalogProvider:
    """Fixture catalog provider: deterministic candidates from the input app."""

    def name(self) -> str:
        return STUB_NAME

    def search(self, app: AppMetadata, *, limit: int = 10) -> list[CatalogCandidate]:
        slug = (app.name or app.source_ref).strip().lower().replace(" ", ".") or "app"
        count = max(0, min(limit, 3))
        return [
            CatalogCandidate(
                package_id=f"com.stub.{slug}.{i}",
                title=app.name or "Stub App",
                publisher=app.publisher,
                score=_score_from(app, i),
                source=STUB_NAME,
                extra={"rank": i},
            )
            for i in range(count)
        ]


registry.add(ProviderKind.CATALOG, STUB_NAME, StubAndroidCatalogProvider, override=True)
