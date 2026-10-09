"""Keys of the capability modules the SwiftUI codegen can build (feasibility router).

The SCOPE stage routes every ``app_spec.capabilities`` entry to a tier and, for tier 2,
to one of these module keys; the operator confirms the routing, :func:`apply_scope`
stamps it on the spec and the scaffold renders the matching descriptor from
:mod:`iosforge.mvp.swiftui_capabilities`. This module holds only the routing metadata
(no Swift), so the scope stage stays light.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleInfo:
    """What a registry module implements and which capability kinds it serves."""

    key: str
    kinds: tuple[str, ...]
    summary: str


MODULES: dict[str, ModuleInfo] = {
    info.key: info
    for info in (
        ModuleInfo(
            "subscriptions_apphud",
            ("subscriptions",),
            "auto-renewable subscriptions and paywall products through Apphud (StoreKit)",
        ),
        ModuleInfo(
            "remote_api",
            ("remote_api",),
            "a light HTTP / AI API the app sends the user's input to and renders the answer "
            "(URLSession, endpoint from the capability config, no SDK)",
        ),
        ModuleInfo(
            "photos_cleaner",
            ("photos_cleaner",),
            "finds duplicate photos in the library and deletes the extra copies (PhotoKit)",
        ),
        ModuleInfo(
            "contacts_cleaner",
            ("contacts_cleaner",),
            "finds duplicate contacts and merges them by deleting the copies (Contacts)",
        ),
        ModuleInfo(
            "storage_scan",
            ("storage_scan",),
            "shows device capacity, free space and the app cache, and clears that cache",
        ),
        ModuleInfo(
            "content_feed",
            ("content_feed",),
            "a content library (articles, sounds, lessons, presets) from the operator's feed or a "
            "bundled seed, premium items gated by the subscription",
        ),
        ModuleInfo(
            "casting",
            ("casting", "screen_mirroring"),
            "TVs on the local network: Bonjour discovery (Google Cast / AirPlay services), a "
            "connection and a stream start; AirPlay route picker; screen mirroring through a "
            "ReplayKit broadcast extension",
        ),
        ModuleInfo(
            "attribution_tenjin",
            ("other",),
            "install attribution through Tenjin after the App Tracking Transparency prompt",
        ),
    )
}


def prompt_lines() -> str:
    """The registry as bullet lines for the scope prompt."""
    return "\n".join(
        f"   - `{info.key}` ({', '.join(info.kinds)}) — {info.summary}" for info in MODULES.values()
    )
