"""Global no-ads rule for the SwiftUI codegen, enforced deterministically.

IOSForge clones monetise through subscriptions (Apphud), never advertising, and must
pass App Review clean, so ad removal cannot depend on per-screen ``layout_notes``.
Structural signals win over text: ad component types/roles, ad SDK imports and ad
SDK types are reliable; phrase matching is limited to unambiguous ad markers
("Loading ads", "may contain ads", "Advertisement", bare "Ad"/"Sponsored" labels)
and stays conservative — when in doubt content is kept (a leftover artefact is
caught by review / Phase 4, corrupted copy is silent). :func:`strip_ad_components`
drops ad components and pure text carriers of those markers from the model's spec
and never rewrites any other component; :func:`ad_violations` makes ad SDK
imports/types and those markers in generated sources a compile-gate error.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from iosforge.mvp.swiftui_permissions import blank_comments_and_strings, lex_swift, line_of

AD_SDK_MODULES = (
    "GoogleMobileAds",
    "GoogleInteractiveMediaAds",
    "AppLovinSDK",
    "UnityAds",
    "IronSource",
    "FBAudienceNetwork",
    "VungleAdsSDK",
    "ChartboostSDK",
    "InMobiSDK",
    "MTGSDK",
    "PAGAdSDK",
    "BUAdSDK",
    "YandexMobileAds",
    "AdColony",
    "StartApp",
    "DTBiOSSDK",
    "MolocoSDK",
    "BidMachine",
    "HyBid",
    "IASDKCore",
    "MyTargetSDK",
    "Tapjoy",
    "OpenWrapSDK",
    "SmaatoSDK",
    "HyprMX",
    "OguryAds",
)

_AD_TOKENS = {
    "ad",
    "ads",
    "advert",
    "adverts",
    "advertising",
    "advertisement",
    "interstitial",
    "admob",
}
_TEXT_CARRIERS = {
    "text",
    "label",
    "caption",
    "disclaimer",
    "footnote",
    "notice",
    "loading",
    "spinner",
    "overlay",
    "loading_status",
}
_AD_PHRASE = r"loading\s+ads?|(?:may|might|can)\s+contain\s+ads?"
_AD_TEXT = re.compile(rf"\b(?:{_AD_PHRASE})\b", re.I)
_MONETIZATION = {
    "paywall",
    "upsell",
    "offer",
    "plan",
    "promo",
    "price",
    "pricing",
    "subscription",
    "pro",
    "premium",
    "trial",
    "purchase",
    "benefit",
}
_AD_LABEL = re.compile(r"^\s*(?:ad|ads|sponsored|advertisement)\s*$", re.I)
_AD_IMPORT = re.compile(
    rf"^\s*(?:@_exported\s+)?import\s+(?:(?:struct|class|enum|protocol|func|typealias|var|let)"
    rf"\s+)?((?:{'|'.join(AD_SDK_MODULES)})(?:Mediation\w*|Adapter\w*)?)\b",
    re.M,
)
_AD_TYPES = re.compile(
    r"\bGAD[A-Z][a-z]\w*|\bMA(?:AdView|InterstitialAd|RewardedAd|NativeAdLoader)\b|"
    r"\bIS(?:BannerView|Interstitial\w*)\b|\bFB(?:AdView|InterstitialAd|NativeAd)\b"
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(value: object) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", _CAMEL.sub(" ", str(value or "")).lower())) - {""}


def is_ad_component(component: dict[str, Any]) -> bool:
    """True when the component itself is advertising (drop it entirely).

    That is an ad type/role (``ad_banner``, ``NativeAdView``, ``interstitial``, ...) or a
    pure text carrier (disclaimer, label, loading overlay) whose data is ad text.
    Components whose type/role names monetization (paywall, upsell, offer, ...) are
    never dropped, even when it mentions ads ("remove_ads_upsell", "ad_free_offer").
    """
    kind = _words(component.get("type")) | _words(component.get("role"))
    if kind & _MONETIZATION:
        return False
    if kind & _AD_TOKENS:
        return True
    data = str(component.get("data") or "")
    return bool(kind & _TEXT_CARRIERS) and bool(_AD_TEXT.search(data) or _AD_LABEL.match(data))


def strip_ad_components(spec: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Copy of ``spec`` without advertising, plus how many components were dropped."""
    clean = copy.deepcopy(spec)
    removed = 0
    for screen in clean.get("screens", []):
        if not isinstance(screen, dict):
            continue
        components = screen.get("components")
        if not isinstance(components, list):
            continue
        kept: list[Any] = []
        for component in components:
            if isinstance(component, dict) and is_ad_component(component):
                removed += 1
                continue
            kept.append(component)
        screen["components"] = kept
    monetization = clean.get("monetization")
    if isinstance(monetization, dict):
        for key in ("ad_networks", "ad_placements", "ads"):
            if monetization.get(key):
                monetization[key] = []
    return clean, removed


def ad_violations(app_dir: Path) -> list[str]:
    """Ad SDK imports/types and ad UI strings in the generated app (empty = clean)."""
    fix = "the clone ships without ads — remove it and let the layout reflow"
    violations: list[str] = []
    for swift in sorted((app_dir / "App").rglob("*.swift")):
        rel = swift.relative_to(app_dir).as_posix()
        raw = swift.read_text(encoding="utf-8", errors="replace")
        code = blank_comments_and_strings(raw)
        for match in _AD_IMPORT.finditer(code):
            violations.append(
                f"{rel}:{line_of(code, match.start())}: error: ad SDK `{match.group(1)}` — {fix}"
            )
        for match in _AD_TYPES.finditer(code):
            violations.append(
                f"{rel}:{line_of(code, match.start())}: error: ad SDK type "
                f"`{match.group(0)}` — {fix}"
            )
        for kind, start, end in lex_swift(raw):
            text = raw[start:end]
            if kind == "string" and (_AD_TEXT.search(text) or _AD_LABEL.match(text)):
                snippet = " ".join(text.split())[:60]
                violations.append(
                    f'{rel}:{line_of(raw, start)}: error: ad UI text "{snippet}" — {fix}'
                )
    return violations
