"""Capability modules of the SwiftUI scaffold: the registry the contract is rendered from.

A capability module is contract code: Swift files under its own scaffold directory
(restored byte-for-byte by ``enforce_contract``), optional SwiftPM packages linked into
the app target, Info.plist properties, permission prompters it needs, a startup call
in the app entry point and one rule for the screen tasks ("screens call this service,
never the SDK"). Screens only ever talk to the module's Swift API; headless screen-id
mode never starts a module.

Every app gets the core modules (subscriptions, attribution: real SDK when configured,
an offline stub otherwise). An ``app_spec.capabilities`` entry routed to tier 2 adds the
module named by its ``module`` key (:mod:`iosforge.mvp.capability_registry`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from iosforge.mvp import capability_registry
from iosforge.mvp import swiftui_integrations as integ
from iosforge.mvp.swiftui_functional import FunctionalCheck

MONETIZATION_DIR = "App/Monetization"
CAPABILITIES_DIR = "App/Capabilities"


@dataclass(frozen=True)
class SwiftPackage:
    """A SwiftPM dependency linked into the app target."""

    name: str
    url: str
    version: str
    product: str = ""

    @property
    def product_name(self) -> str:
        return self.product or self.name


@dataclass(frozen=True)
class CapabilityContext:
    """What a module renders from: the spec, the frozen job inputs, its spec entry."""

    spec: dict[str, Any]
    integrations: integ.Integrations
    capability: dict[str, Any] = field(default_factory=dict)

    @property
    def config(self) -> dict[str, Any]:
        value = self.capability.get("config")
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class CapabilityDescriptor:
    """One capability module of the scaffold."""

    key: str
    directory: str
    render: Callable[[CapabilityContext], dict[str, str]]
    screen_api_rule: str
    startup: str = ""
    packages: Callable[[CapabilityContext], tuple[SwiftPackage, ...]] = lambda ctx: ()
    info_properties: Callable[[CapabilityContext], list[str]] = lambda ctx: []
    prompters: Callable[[CapabilityContext], tuple[str, ...]] = lambda ctx: ()
    rule_applies: Callable[[CapabilityContext], bool] = lambda ctx: True
    functional_check: Callable[[CapabilityContext], FunctionalCheck | None] = lambda ctx: None


def _subscriptions_packages(ctx: CapabilityContext) -> tuple[SwiftPackage, ...]:
    if not ctx.integrations.subscriptions:
        return ()
    return (SwiftPackage(*integ.APPHUD_PACKAGE),)


def _attribution_packages(ctx: CapabilityContext) -> tuple[SwiftPackage, ...]:
    if not ctx.integrations.attribution:
        return ()
    return (SwiftPackage(*integ.TENJIN_PACKAGE),)


SUBSCRIPTIONS = CapabilityDescriptor(
    key="subscriptions_apphud",
    directory=MONETIZATION_DIR,
    render=lambda ctx: {
        f"{MONETIZATION_DIR}/Subscriptions.swift": integ.render_subscriptions(ctx.integrations)
    },
    startup="Subscriptions.start()",
    packages=_subscriptions_packages,
    screen_api_rule=(
        "Subscriptions only through the scaffold API (`App/Monetization/Subscriptions.swift`):\n"
        "  `await Subscriptions.products()` (`SubscriptionProduct`: id, title, price, period), "
        "buy with\n  `await Subscriptions.purchase(product.id)`, restore with "
        "`await Subscriptions.restore()`, call\n  `Subscriptions.paywallShown()` in `.onAppear` "
        "of a paywall and gate premium features with\n  `Subscriptions.hasPremium`. Never "
        "import StoreKit or ApphudSDK in screens."
    ),
)

ATTRIBUTION = CapabilityDescriptor(
    key="attribution_tenjin",
    directory=MONETIZATION_DIR,
    render=lambda ctx: {
        f"{MONETIZATION_DIR}/Attribution.swift": integ.render_attribution(ctx.integrations)
    },
    startup="await Attribution.start()",
    packages=_attribution_packages,
    info_properties=lambda ctx: integ.attribution_properties(ctx.integrations),
    prompters=lambda ctx: ("tracking",) if ctx.integrations.attribution else (),
    rule_applies=lambda ctx: ctx.integrations.attribution,
    screen_api_rule=(
        "Install attribution starts by itself from the app entry point (`Attribution`); "
        "screens never\n  import TenjinSDK or AppTrackingTransparency and never ask for "
        "tracking themselves."
    ),
)

CORE: tuple[CapabilityDescriptor, ...] = (SUBSCRIPTIONS, ATTRIBUTION)

REGISTRY: dict[str, CapabilityDescriptor] = {d.key: d for d in CORE}


def register(descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
    """Add a module to the registry (its key must be routable by the scope stage)."""
    if descriptor.key not in capability_registry.MODULES:
        raise ValueError(f"module {descriptor.key!r} is missing from capability_registry")
    REGISTRY[descriptor.key] = descriptor
    return descriptor


@dataclass(frozen=True)
class Selected:
    """A module chosen for the app, with the context it renders from."""

    descriptor: CapabilityDescriptor
    context: CapabilityContext


def routed_modules(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """``(module key, capability entry)`` for every tier-2 capability of ``spec``."""
    load_modules()
    routed: list[tuple[str, dict[str, Any]]] = []
    for capability in spec.get("capabilities") or []:
        if not isinstance(capability, dict) or capability.get("tier") != 2:
            continue
        key = capability.get("module")
        if isinstance(key, str) and key in REGISTRY:
            routed.append((key, capability))
    return routed


def select(spec: dict[str, Any], integrations: integ.Integrations) -> list[Selected]:
    """The app's modules: the core ones, then each routed module once, in spec order."""
    load_modules()
    chosen = [Selected(d, CapabilityContext(spec, integrations)) for d in CORE]
    seen = {d.key for d in CORE}
    for key, capability in routed_modules(spec):
        if key in seen:
            continue
        seen.add(key)
        chosen.append(Selected(REGISTRY[key], CapabilityContext(spec, integrations, capability)))
    return chosen


def render_files(selected: list[Selected]) -> dict[str, str]:
    """Contract files of every selected module."""
    files: dict[str, str] = {}
    for item in selected:
        files.update(item.descriptor.render(item.context))
    return files


def packages(selected: list[Selected]) -> list[SwiftPackage]:
    """Distinct SwiftPM packages of the selected modules."""
    found: dict[str, SwiftPackage] = {}
    for item in selected:
        for package in item.descriptor.packages(item.context):
            found.setdefault(package.name, package)
    return list(found.values())


def project_packages(selected: list[Selected]) -> list[str]:
    """Top-level XcodeGen ``packages:`` lines."""
    lines: list[str] = []
    for package in packages(selected):
        lines += [f"  {package.name}:", f"    url: {package.url}", f'    from: "{package.version}"']
    return ["packages:", *lines] if lines else []


def target_dependencies(selected: list[Selected]) -> list[str]:
    """Target ``dependencies:`` lines linking the selected modules' packages."""
    lines: list[str] = []
    for package in packages(selected):
        lines += [f"      - package: {package.name}", f"        product: {package.product_name}"]
    return ["    dependencies:", *lines] if lines else []


def info_properties(selected: list[Selected], integrations: integ.Integrations) -> list[str]:
    """Info.plist property lines: the app-level declarations, then each module's."""
    lines = integ.app_properties(integrations)
    for item in selected:
        lines += item.descriptor.info_properties(item.context)
    return lines


def prompter_kinds(selected: list[Selected]) -> list[str]:
    """Permission kinds the selected modules need prompters for."""
    kinds: list[str] = []
    for item in selected:
        kinds += [k for k in item.descriptor.prompters(item.context) if k not in kinds]
    return kinds


def startup_calls(selected: list[Selected]) -> list[str]:
    """Swift statements the app entry point runs at launch, in module order."""
    return [item.descriptor.startup for item in selected if item.descriptor.startup]


def screen_rules(selected: list[Selected]) -> list[str]:
    """One rule per selected module for the screen-generation prompts."""
    return [
        item.descriptor.screen_api_rule
        for item in selected
        if item.descriptor.screen_api_rule and item.descriptor.rule_applies(item.context)
    ]


def functional_checks(selected: list[Selected]) -> list[FunctionalCheck]:
    """The functional checks of the selected modules (modules without one add none)."""
    checks: list[FunctionalCheck] = []
    for item in selected:
        check = item.descriptor.functional_check(item.context)
        if check is not None:
            checks.append(check)
    return checks


def capability_screen(ctx: CapabilityContext) -> str:
    """The first screen that surfaces the module's capability (where its check runs)."""
    screens = ctx.capability.get("screens") or []
    return str(screens[0]) if screens else ""


def directories() -> tuple[str, ...]:
    """Every scaffold directory a module may own (all contract directories)."""
    return tuple(dict.fromkeys([MONETIZATION_DIR, CAPABILITIES_DIR]))


_LOAD_LOCK = threading.Lock()
_LOADED: list[bool] = []


def load_modules() -> None:
    """Register the built-in capability modules once (lazily, to avoid import cycles)."""
    if _LOADED:
        return
    with _LOAD_LOCK:
        if _LOADED:
            return
        from iosforge.mvp import swiftui_cleaners, swiftui_remote_api

        swiftui_remote_api.register()
        swiftui_cleaners.register()
        _LOADED.append(True)
