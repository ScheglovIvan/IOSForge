"""Deterministic stub for :class:`PromptSetProvider` (T-3.3, SPEC §6/§9).

Returns a fixed in-memory prompt-set version (no admin/DB access), so the
codegen worker (E8) can be built under TDD before the real versioned store
(E6) exists. Registered under ``(promptset, "stub")``.
"""

from __future__ import annotations

from iosforge.providers.base import PromptSetVersion
from iosforge.providers.registry import ProviderKind, registry

STUB_NAME = "stub"

#: Fixed bodies of the stub prompt set (purpose -> body).
_STUB_BODIES = {
    "codegen": "Generate the target sources from the provided screens.",
    "selftest": "Compare the rendered screens against the references.",
}
#: Versions the stub pretends to hold, newest first.
_STUB_VERSIONS = [2, 1]
_LATEST = _STUB_VERSIONS[0]


class StubPromptSetProvider:
    """Fixture prompt-set provider: one deterministic version, no admin/DB."""

    def name(self) -> str:
        return STUB_NAME

    def get(self, prompt_set: str, *, version: int | None = None) -> PromptSetVersion:
        resolved = _LATEST if version is None else version
        return PromptSetVersion(
            prompt_set=prompt_set,
            version=resolved,
            bodies=dict(_STUB_BODIES),
            author="stub",
        )

    def list_versions(self, prompt_set: str) -> list[int]:
        return list(_STUB_VERSIONS)


registry.add(ProviderKind.PROMPTSET, STUB_NAME, StubPromptSetProvider, override=True)
