"""Global no-ads rule for the SwiftUI codegen, enforced deterministically.

IOSForge clones monetise through subscriptions (Apphud), never advertising, and must
pass App Review clean, so ad removal cannot depend on per-screen ``layout_notes``.
:func:`strip_ad_components` removes advertising components from the app_spec copy
the model works from, and :func:`ad_violations` makes any ad SDK import, ad SDK
type or ad UI text in the generated sources a compile-gate error, whatever the
screenshots and native view trees (which still show the original's ads) suggested.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from iosforge.mvp.swiftui_permissions import blank_comments_and_strings

AD_SDK_MODULES = (
    "GoogleMobileAds",
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
)

_AD_TOKENS = {
    "ad",
    "ads",
    "advert",
    "adverts",
    "advertising",
    "advertisement",
    "interstitial",
    "rewarded",
    "sponsored",
    "admob",
}
_AD_TEXT = re.compile(
    r"\b(?:loading\s+ads?|(?:may|might|can)\s+contain\s+ads?|advertisement|sponsored|"
    r"watch\s+(?:an?\s+)?(?:ad|video\s+ad)|remove\s+ads|ad[-\s]free|no\s+ads)\b",
    re.I,
)
_AD_LABEL = re.compile(r"^\s*(?:ad|ads)\s*$", re.I)
_AD_IMPORT = re.compile(rf"^\s*import\s+({'|'.join(AD_SDK_MODULES)})\b", re.M)
_AD_TYPES = re.compile(
    r"\bGAD[A-Z]\w*|\bMA(?:AdView|InterstitialAd|RewardedAd|NativeAdLoader)\b|"
    r"\bIS(?:BannerView|Interstitial\w*)\b|\bFB(?:AdView|InterstitialAd|NativeAd)\b"
)
_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


def is_ad_component(component: dict[str, Any]) -> bool:
    """True when an app_spec component's type or role names advertising."""
    words = re.split(
        r"[^a-z0-9]+", f"{component.get('type', '')} {component.get('role', '')}".lower()
    )
    data = str(component.get("data") or "")
    return bool(_AD_TOKENS.intersection(words)) or bool(_AD_TEXT.search(data))


def strip_ad_components(spec: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Copy of ``spec`` without advertising components, plus how many were removed."""
    clean = copy.deepcopy(spec)
    removed = 0
    for screen in clean.get("screens", []):
        if not isinstance(screen, dict):
            continue
        components = screen.get("components")
        if isinstance(components, list):
            kept = [c for c in components if not (isinstance(c, dict) and is_ad_component(c))]
            removed += len(components) - len(kept)
            screen["components"] = kept
    monetization = clean.get("monetization")
    if isinstance(monetization, dict):
        for key in ("ad_networks", "ad_placements", "ads"):
            if monetization.get(key):
                monetization[key] = []
    return clean, removed


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


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
                f"{rel}:{_line_of(code, match.start())}: error: ad SDK `{match.group(1)}` — {fix}"
            )
        for match in _AD_TYPES.finditer(code):
            violations.append(
                f"{rel}:{_line_of(code, match.start())}: error: ad SDK type "
                f"`{match.group(0)}` — {fix}"
            )
        for match in _STRING.finditer(raw):
            if code[match.start()] != '"':
                continue
            text = match.group(1)
            if _AD_TEXT.search(text) or _AD_LABEL.match(text):
                violations.append(
                    f'{rel}:{_line_of(raw, match.start())}: error: ad UI text "{text[:60]}" — {fix}'
                )
    return violations
