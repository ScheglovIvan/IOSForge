"""Tests for the provider registry-factory (T-3.1, SPEC §9).

Core requirement: a new implementation is resolved by a *string* from
``ProviderConfig``/config without the orchestrator (or any caller) importing the
concrete class. These tests register *fake* implementations against an isolated
registry and resolve them by name only.
"""

from __future__ import annotations

import pytest

from iosforge.providers import (
    EmulatorProvider,
    ProviderKind,
    ProviderNotRegistered,
    ProviderRegistry,
)
from iosforge.providers.base import ScreenShot, WalkthroughResult, WalkthroughStrategy
from iosforge.providers.registry import ProviderAlreadyRegistered


# --------------------------------------------------------------------------- #
# Fakes — implement the Protocols without inheriting from any concrete class.
# --------------------------------------------------------------------------- #
class FakeEmulator:
    """A fake EmulatorProvider (structural conformance to the Protocol)."""

    def __init__(self) -> None:
        self.installed = False
        self.torn_down = False

    def name(self) -> str:
        return "fake-emulator"

    def install(self, apk: object) -> None:  # ArtifactRef in real impls
        self.installed = True

    def walkthrough(self, strategy: WalkthroughStrategy) -> WalkthroughResult:
        return WalkthroughResult(screens=[], screen_map={"nodes": []}, logs="ok")

    def teardown(self) -> None:
        self.torn_down = True


class AnotherFakeEmulator(FakeEmulator):
    def name(self) -> str:
        return "another-fake"


@pytest.fixture
def reg() -> ProviderRegistry:
    """A fresh, isolated registry per test (no cross-test bleed)."""
    return ProviderRegistry()


def test_register_and_resolve_by_name(reg: ProviderRegistry) -> None:
    reg.add(ProviderKind.EMULATOR, "fake-emulator", FakeEmulator)

    instance = reg.resolve(ProviderKind.EMULATOR, "fake-emulator")

    assert isinstance(instance, FakeEmulator)
    # The resolved object structurally satisfies the Protocol (runtime check).
    assert isinstance(instance, EmulatorProvider)
    assert instance.name() == "fake-emulator"


def test_resolve_from_config_string_without_orchestrator(reg: ProviderRegistry) -> None:
    """The DoD case: resolve the active impl by the ProviderConfig string alone.

    ``config`` is the {provider_type: active_implementation} projection of
    ``ProviderConfig`` rows — no orchestrator, no DB, no concrete import here.
    """
    reg.add(ProviderKind.EMULATOR, "fake-emulator", FakeEmulator)
    reg.add(ProviderKind.EMULATOR, "another-fake", AnotherFakeEmulator)

    # Simulated ProviderConfig: provider_type -> active_implementation.
    config = {"emulator": "fake-emulator"}
    provider = reg.resolve_from_config(ProviderKind.EMULATOR, config)
    assert provider.name() == "fake-emulator"

    # Switching the active implementation = editing the config string only.
    config["emulator"] = "another-fake"
    provider2 = reg.resolve_from_config(ProviderKind.EMULATOR, config)
    assert provider2.name() == "another-fake"


def test_resolve_each_call_returns_fresh_instance(reg: ProviderRegistry) -> None:
    reg.add(ProviderKind.EMULATOR, "fake-emulator", FakeEmulator)

    a = reg.resolve(ProviderKind.EMULATOR, "fake-emulator")
    b = reg.resolve(ProviderKind.EMULATOR, "fake-emulator")
    assert a is not b


def test_register_decorator_form(reg: ProviderRegistry) -> None:
    @reg.register(ProviderKind.EMULATOR, "deco")
    class Deco(FakeEmulator):
        def name(self) -> str:
            return "deco"

    assert reg.resolve(ProviderKind.EMULATOR, "deco").name() == "deco"


def test_unknown_name_raises(reg: ProviderRegistry) -> None:
    with pytest.raises(ProviderNotRegistered):
        reg.resolve(ProviderKind.EMULATOR, "does-not-exist")


def test_unknown_kind_in_config_raises(reg: ProviderRegistry) -> None:
    with pytest.raises(ProviderNotRegistered):
        reg.resolve_from_config(ProviderKind.CATALOG, {})  # no active impl set


def test_config_pointing_to_unregistered_name_raises(reg: ProviderRegistry) -> None:
    with pytest.raises(ProviderNotRegistered):
        reg.resolve_from_config(ProviderKind.EMULATOR, {"emulator": "ghost"})


def test_duplicate_registration_raises_without_override(reg: ProviderRegistry) -> None:
    reg.add(ProviderKind.EMULATOR, "fake-emulator", FakeEmulator)
    with pytest.raises(ProviderAlreadyRegistered):
        reg.add(ProviderKind.EMULATOR, "fake-emulator", AnotherFakeEmulator)


def test_override_allows_shadowing(reg: ProviderRegistry) -> None:
    reg.add(ProviderKind.EMULATOR, "fake-emulator", FakeEmulator)
    reg.add(ProviderKind.EMULATOR, "fake-emulator", AnotherFakeEmulator, override=True)
    assert reg.resolve(ProviderKind.EMULATOR, "fake-emulator").name() == "another-fake"


def test_kinds_are_isolated(reg: ProviderRegistry) -> None:
    """The same name under different kinds does not collide."""
    reg.add(ProviderKind.EMULATOR, "x", FakeEmulator)
    reg.add(ProviderKind.CATALOG, "x", FakeEmulator)
    assert reg.is_registered(ProviderKind.EMULATOR, "x")
    assert reg.is_registered(ProviderKind.CATALOG, "x")
    assert reg.names(ProviderKind.EMULATOR) == ["x"]


def test_isolated_registries_do_not_bleed() -> None:
    """Two registries (or two tests) never share registrations."""
    with ProviderRegistry.isolated() as r1, ProviderRegistry.isolated() as r2:
        r1.add(ProviderKind.EMULATOR, "only-in-r1", FakeEmulator)
        assert r1.is_registered(ProviderKind.EMULATOR, "only-in-r1")
        assert not r2.is_registered(ProviderKind.EMULATOR, "only-in-r1")


def test_clear_drops_registrations(reg: ProviderRegistry) -> None:
    reg.add(ProviderKind.EMULATOR, "fake-emulator", FakeEmulator)
    reg.clear()
    assert not reg.is_registered(ProviderKind.EMULATOR, "fake-emulator")


def test_walkthrough_result_shape(reg: ProviderRegistry) -> None:
    """Sanity: a resolved fake produces the §5.4 walkthrough output shape."""
    reg.add(ProviderKind.EMULATOR, "fake-emulator", FakeEmulator)
    provider = reg.resolve(ProviderKind.EMULATOR, "fake-emulator")
    result = provider.walkthrough(WalkthroughStrategy())
    assert isinstance(result, WalkthroughResult)
    assert result.logs == "ok"
    assert "nodes" in result.screen_map
    assert all(isinstance(s, ScreenShot) for s in result.screens)
