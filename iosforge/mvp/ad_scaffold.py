"""Deterministic Flutter ad-integration bundle (google_mobile_ads) from the spec.

Emitted alongside the app when monetization has ads. NOT agentic — a ready drop-in
``AdService`` + config + platform snippets driven by ``monetization.ad_placements``,
so the generated client (or a developer) wires ads at the discovered placements and
disables them for the RevenueCat ``pro`` entitlement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger

log = get_logger("mvp.ad_scaffold")

GOOGLE_MOBILE_ADS_VERSION = "^5.1.0"
_PLACEHOLDER_UNIT = "ca-app-pub-XXXXXXXXXXXXXXXX/XXXXXXXXXX"
_PLACEHOLDER_APP_ID = "ca-app-pub-XXXXXXXXXXXXXXXX~XXXXXXXXXX"

_DEFAULT_PLACEMENTS = [
    {"format": "interstitial", "screen_context": "default", "trigger": "screen transition"},
    {"format": "banner", "screen_context": "home", "trigger": "home load"},
]

_AD_SERVICE_DART = """\
import 'package:google_mobile_ads/google_mobile_ads.dart';

import 'ad_config.dart';

/// Central ad service (google_mobile_ads). Initialize in `main()`, then call the
/// show* methods at the placements listed in INTEGRATION.md. Set [adsEnabled] to
/// false when the user holds the RevenueCat `pro` entitlement.
class AdService {
  AdService._();
  static final AdService instance = AdService._();

  bool adsEnabled = true;
  InterstitialAd? _interstitial;
  RewardedAd? _rewarded;

  Future<void> init() async {
    await MobileAds.instance.initialize();
  }

  String _unit(String placement) =>
      AdConfig.unitIds[placement] ?? AdConfig.unitIds['default']!;

  BannerAd? banner(String placement) {
    if (!adsEnabled) return null;
    return BannerAd(
      adUnitId: _unit(placement),
      size: AdSize.banner,
      request: const AdRequest(),
      listener: const BannerAdListener(),
    )..load();
  }

  Future<void> loadInterstitial(String placement) async {
    if (!adsEnabled) return;
    await InterstitialAd.load(
      adUnitId: _unit(placement),
      request: const AdRequest(),
      adLoadCallback: InterstitialAdLoadCallback(
        onAdLoaded: (ad) => _interstitial = ad,
        onAdFailedToLoad: (_) => _interstitial = null,
      ),
    );
  }

  void showInterstitial() {
    if (!adsEnabled) return;
    _interstitial?.show();
    _interstitial = null;
  }

  Future<void> loadRewarded(String placement) async {
    if (!adsEnabled) return;
    await RewardedAd.load(
      adUnitId: _unit(placement),
      request: const AdRequest(),
      rewardedAdLoadCallback: RewardedAdLoadCallback(
        onAdLoaded: (ad) => _rewarded = ad,
        onAdFailedToLoad: (_) => _rewarded = null,
      ),
    );
  }

  void showRewarded(void Function(RewardItem reward) onReward) {
    if (!adsEnabled) return;
    _rewarded?.show(onUserEarnedReward: (_, reward) => onReward(reward));
    _rewarded = null;
  }
}
"""


def _placement_key(p: dict[str, Any]) -> str:
    return str(p.get("screen_context") or p.get("trigger") or p.get("format") or "default")


def _placements(spec: dict[str, Any]) -> list[dict[str, Any]]:
    mon = spec.get("monetization", {}) if isinstance(spec.get("monetization"), dict) else {}
    pls = [p for p in (mon.get("ad_placements") or []) if isinstance(p, dict) and p.get("format")]
    return pls or _DEFAULT_PLACEMENTS


def has_ads(spec: dict[str, Any]) -> bool:
    mon = spec.get("monetization", {})
    if not isinstance(mon, dict):
        return False
    return str(mon.get("model", "")).lower() in ("ads", "mixed") or bool(mon.get("ad_placements"))


def _ad_config_dart(placements: list[dict[str, Any]]) -> str:
    entries = {"default": _PLACEHOLDER_UNIT}
    for p in placements:
        entries[_placement_key(p)] = _PLACEHOLDER_UNIT
    lines = [
        "/// Generated from app_spec `monetization.ad_placements`.",
        "/// Replace the placeholder AdMob unit ids with the ones from",
        "/// `admin/ads/admob.config.json` before release.",
        "class AdConfig {",
        "  static const Map<String, String> unitIds = {",
    ]
    lines += [f"    '{k}': '{v}'," for k, v in entries.items()]
    lines += [
        "  };",
        "",
        "  /// Frequency cap (also served from Remote Config `ads_frequency_interstitial`).",
        "  static const int interstitialEveryNScreens = 3;",
        "}",
        "",
    ]
    return "\n".join(lines)


def _integration_md(placements: list[dict[str, Any]]) -> str:
    lines = [
        "# Ad integration (google_mobile_ads)",
        "",
        "1. Merge `pubspec_snippet.yaml` into the app's `pubspec.yaml`.",
        "2. Add `ios_info_plist_snippet.xml` to `ios/Runner/Info.plist` and",
        "   `android_manifest_snippet.xml` inside `<application>` of the Android manifest.",
        "3. Drop `ad_service.dart` + `ad_config.dart` under `lib/services/` and replace the",
        "   placeholder unit ids with the real ones from `admin/ads/admob.config.json`.",
        "4. In `main()`: `await AdService.instance.init();`",
        "5. Bind the RevenueCat `pro` entitlement: `AdService.instance.adsEnabled = !user.isPro;`",
        "",
        "## Placements (from the analysis)",
    ]
    for p in placements:
        fmt = p.get("format")
        key = _placement_key(p)
        trigger = p.get("trigger", "")
        if fmt == "banner":
            call = f"AdService.instance.banner('{key}')  // embed in the screen"
        elif fmt == "rewarded":
            call = f"loadRewarded('{key}')` then `showRewarded(...)`"
        else:
            call = f"loadInterstitial('{key}')` then `showInterstitial()`"
        lines.append(f"- **{fmt}** at `{key}` (trigger: {trigger or 'n/a'}) → `{call}`")
    lines.append("")
    return "\n".join(lines)


def build_ad_bundle(spec: dict[str, Any], out_dir: Path) -> Path | None:
    """Write a ready Flutter ad-integration bundle into ``out_dir``; None if no ads."""
    if not has_ads(spec):
        return None
    placements = _placements(spec)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "ad_service.dart").write_text(_AD_SERVICE_DART)
    (out_dir / "ad_config.dart").write_text(_ad_config_dart(placements))
    (out_dir / "pubspec_snippet.yaml").write_text(
        "# Merge into pubspec.yaml under dependencies:\n"
        "dependencies:\n"
        f"  google_mobile_ads: {GOOGLE_MOBILE_ADS_VERSION}\n"
    )
    (out_dir / "ios_info_plist_snippet.xml").write_text(
        "<!-- Add to ios/Runner/Info.plist -->\n"
        "<key>GADApplicationIdentifier</key>\n"
        f"<string>{_PLACEHOLDER_APP_ID}</string>\n"
    )
    (out_dir / "android_manifest_snippet.xml").write_text(
        "<!-- Add inside <application> of android/app/src/main/AndroidManifest.xml -->\n"
        "<meta-data\n"
        '    android:name="com.google.android.gms.ads.APPLICATION_ID"\n'
        f'    android:value="{_PLACEHOLDER_APP_ID}"/>\n'
    )
    (out_dir / "INTEGRATION.md").write_text(_integration_md(placements))
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "formats": sorted({str(p.get("format")) for p in placements}),
                "placements": len(placements),
                "google_mobile_ads": GOOGLE_MOBILE_ADS_VERSION,
            },
            indent=2,
        )
    )

    log.info("ad_scaffold.done", placements=len(placements))
    return out_dir
