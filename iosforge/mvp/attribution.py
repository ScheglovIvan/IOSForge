"""Attribution (MMP) wiring — Tenjin config passthrough for each generated clone.

Traffic sources are **not** baked into the app: Meta / Google / TikTok / Apple Search
Ads and any other network are connected in the Tenjin dashboard, so a source added
later attributes without an app update. Only the iOS SDK key reaches the binary.

Two things must nevertheless be present at build time, and this module stages both:

- ``NSUserTrackingUsageDescription`` — merged into the generator's
  ``ios_permissions.json`` (the CodeMagic template already applies that file). Without
  ATT consent attribution degrades to SKAN campaign-level aggregates.
- ``SKAdNetworkItems`` — the consolidated plist from the MMP dashboard is copied next
  to the app so the build merges it into ``Info.plist``. A network whose id is missing
  there gets **zero** SKAN attribution until the app ships again, which is exactly why
  the list is data (maintained upstream) rather than a hardcoded constant.

Returns ``None`` when attribution is disabled or no SDK key is configured (the app is
then generated without any attribution SDK).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.attribution")

_PROVIDER = "tenjin"

# Apple posts a copy of every SKAdNetwork postback to this endpoint, which is how the
# MMP sees SKAN conversions at all. Baked into Info.plist, so it must be present at
# build time — without it SKAN attribution never reaches Tenjin.
_SKAN_ENDPOINT = "https://tenjin-skan.com"


def load_key(settings: Settings) -> str:
    """Return the Tenjin iOS SDK key from the secrets file, env or settings field."""
    path = settings.tenjin_secrets_path
    if path and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line.startswith("TENJIN_API_KEY") and "=" in line:
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:  # an empty placeholder must not shadow the env fallback
                    return value
    return settings.tenjin_api_key or os.environ.get("TENJIN_API_KEY", "")


def provision(
    *,
    settings: Settings,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Return the attribution config codegen injects for this clone (no remote calls).

    ``api_key`` overrides the resolved SDK key (per-job, so each clone reports to its
    own Tenjin app). Returns ``None`` when attribution is disabled or unconfigured.
    """
    if not settings.attribution_provision:
        return None
    key = api_key or load_key(settings)
    if not key:
        log.info("attribution.skip", reason="no api_key")
        return None

    result = {
        "provider": _PROVIDER,
        "sdk_key": key,
        "att_usage_description": settings.att_usage_description,
        "skan_report_endpoint": _SKAN_ENDPOINT,
    }
    log.bind(stage="attribution").info("attribution.done", provider=_PROVIDER)
    return result


def stage_ios_assets(
    settings: Settings, flutter_app: Path, config: dict[str, Any]
) -> dict[str, bool]:
    """Stage the ATT description + SKAdNetwork list next to the generated app.

    Both land inside ``flutter_app/`` so they survive the GitHub push and are applied
    by the CodeMagic build. Returns which assets were staged.
    """
    staged = {"att": False, "skan_endpoint": False, "skadnetwork": False}

    reason = str(config.get("att_usage_description") or "").strip()
    endpoint = str(config.get("skan_report_endpoint") or "").strip()
    if reason or endpoint:
        perms_file = flutter_app / "ios_permissions.json"
        perms: dict[str, Any] = {}
        if perms_file.is_file():
            try:
                loaded = json.loads(perms_file.read_text())
                perms = loaded if isinstance(loaded, dict) else {}
            except ValueError:
                perms = {}
        if reason:
            perms["NSUserTrackingUsageDescription"] = reason
            staged["att"] = True
        if endpoint:
            perms["NSAdvertisingAttributionReportEndpoint"] = endpoint
            staged["skan_endpoint"] = True
        perms_file.write_text(json.dumps(perms, indent=2, ensure_ascii=False))

    src = settings.skadnetwork_ids_path
    if src and Path(src).is_file():
        shutil.copy2(Path(src), flutter_app / "skadnetwork_ids.plist")
        staged["skadnetwork"] = True
    else:
        log.warning("attribution.skadnetwork_missing", path=src)

    log.info("attribution.staged", **staged)
    return staged
