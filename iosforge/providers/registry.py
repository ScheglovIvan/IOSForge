"""Provider registry-factory (T-3.1, SPEC §9).

The registry is the single mechanism that lets every external capability of the
pipeline be **swappable**: each abstraction (emulator, catalog, apk source,
codegen, build, prompt set) has a :class:`~typing.Protocol`, and concrete
implementations register themselves under ``(kind, name)``. The active
implementation is resolved by a *string* taken from
:class:`~iosforge.db.models.ProviderConfig` (``provider_type`` ->
``active_implementation``) or from a plain config mapping — **without touching
the orchestrator or any other service** when a new implementation is added
(SPEC §9 hard requirement).

Design
------
* ``register(kind, name)`` — decorator/method that records a *factory* (the
  class itself, or any zero-arg callable) under ``(kind, name)``.
* ``resolve(kind, name)`` — returns a fresh instance of the registered factory.
* ``resolve_from_config(kind, config)`` — looks up the active implementation
  name for ``kind`` and resolves it. ``config`` is a mapping
  ``{provider_type: active_implementation}`` (the projection of
  ``ProviderConfig`` rows) so the registry has **no DB dependency**.
* Unknown ``(kind, name)`` -> :class:`ProviderNotRegistered`.

The default module-level :data:`registry` is shared; tests use
:meth:`ProviderRegistry.isolated` (a fresh registry) to avoid cross-test bleed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")


class ProviderKind(StrEnum):
    """The swappable provider abstractions of the pipeline (SPEC §9).

    The string values are the canonical ``ProviderConfig.provider_type`` keys.
    ``COMPLIANCE`` is reserved for ``ComplianceMetric`` (SPEC §5.5/§9, declared
    later in T-9.1) — it will plug into this same registry mechanism.
    """

    CATALOG = "catalog"
    APK_SOURCE = "apk_source"
    EMULATOR = "emulator"
    CODEGEN = "codegen"
    BUILD = "build"
    PROMPTSET = "promptset"
    # Reserved (T-9.1): the >=95% similarity metric registers here too.
    COMPLIANCE = "compliance"


class ProviderError(Exception):
    """Base class for registry errors."""


class ProviderNotRegistered(ProviderError, KeyError):
    """Raised when ``(kind, name)`` has no registered implementation."""

    def __init__(self, kind: ProviderKind | str, name: str) -> None:
        self.kind = kind
        self.name = name
        super().__init__(f"no provider registered for kind={kind!r} name={name!r}")


class ProviderAlreadyRegistered(ProviderError):
    """Raised when registering a duplicate ``(kind, name)`` without override."""

    def __init__(self, kind: ProviderKind | str, name: str) -> None:
        self.kind = kind
        self.name = name
        super().__init__(f"provider already registered for kind={kind!r} name={name!r}")


class ProviderRegistry:
    """A registry-factory of provider implementations keyed by ``(kind, name)``.

    Implementations register a *factory* — typically the class itself — and are
    resolved to fresh instances on demand. Resolution is by name only, so the
    orchestrator depends on the registry + the Protocol, never on a concrete
    class (SPEC §9).
    """

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], Callable[[], Any]] = {}

    # --- registration -----------------------------------------------------
    def register(
        self,
        kind: ProviderKind | str,
        name: str,
        *,
        override: bool = False,
    ) -> Callable[[Callable[[], T]], Callable[[], T]]:
        """Decorator: register a factory under ``(kind, name)``.

        ``factory`` is any zero-arg callable returning a provider instance; a
        class (called with no args) is the common case. Re-registering the same
        key raises unless ``override=True`` (lets a deployment shadow a default).
        """
        key = (str(kind), name)

        def _decorator(factory: Callable[[], T]) -> Callable[[], T]:
            if not override and key in self._factories:
                raise ProviderAlreadyRegistered(kind, name)
            self._factories[key] = factory
            return factory

        return _decorator

    def add(
        self,
        kind: ProviderKind | str,
        name: str,
        factory: Callable[[], Any],
        *,
        override: bool = False,
    ) -> None:
        """Imperative form of :meth:`register` (e.g. for tests/fixtures)."""
        self.register(kind, name, override=override)(factory)

    # --- resolution -------------------------------------------------------
    def resolve(self, kind: ProviderKind | str, name: str) -> Any:  # noqa: ANN401
        """Return a fresh instance of the implementation under ``(kind, name)``.

        Raises :class:`ProviderNotRegistered` if the key is unknown.
        """
        try:
            factory = self._factories[(str(kind), name)]
        except KeyError:
            raise ProviderNotRegistered(kind, name) from None
        return factory()

    def resolve_from_config(
        self,
        kind: ProviderKind | str,
        config: Mapping[str, str],
    ) -> Any:  # noqa: ANN401
        """Resolve the **active** implementation for ``kind`` from ``config``.

        ``config`` maps ``provider_type -> active_implementation`` — the
        projection of :class:`~iosforge.db.models.ProviderConfig` rows. The
        registry never touches the DB; the caller passes the projection in. A
        missing key or an unregistered name both raise
        :class:`ProviderNotRegistered`.
        """
        active = config.get(str(kind))
        if active is None:
            raise ProviderNotRegistered(kind, "<active implementation unset>")
        return self.resolve(kind, active)

    # --- introspection / lifecycle ---------------------------------------
    def is_registered(self, kind: ProviderKind | str, name: str) -> bool:
        """Whether ``(kind, name)`` has a registered factory."""
        return (str(kind), name) in self._factories

    def names(self, kind: ProviderKind | str) -> list[str]:
        """All registered implementation names for ``kind`` (for the admin UI)."""
        return [n for (k, n) in self._factories if k == str(kind)]

    def clear(self) -> None:
        """Drop all registrations (test teardown)."""
        self._factories.clear()

    @classmethod
    @contextmanager
    def isolated(cls) -> Iterator[ProviderRegistry]:
        """Yield a fresh, empty registry (test isolation).

        Use instead of the shared module-level :data:`registry` so registrations
        made in one test never leak into another.
        """
        yield cls()


#: Shared, process-wide registry. Implementations register against this; the
#: orchestrator resolves the active provider against it. Tests use
#: :meth:`ProviderRegistry.isolated` to avoid touching shared state.
registry = ProviderRegistry()


__all__ = [
    "ProviderAlreadyRegistered",
    "ProviderError",
    "ProviderKind",
    "ProviderNotRegistered",
    "ProviderRegistry",
    "registry",
]
