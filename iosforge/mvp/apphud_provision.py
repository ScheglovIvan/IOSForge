"""Apphud subscription wiring — config passthrough for each generated clone.

Apphud exposes no management/provisioning REST API: the app, its in-app products
and the paywall placements are set up once in the Apphud dashboard. So — unlike
the former RevenueCat flow — nothing is created remotely here. This module derives
the deterministic product ids (matching the CodeMagic bundle-id scheme) from the
spec's ``monetization.packages`` and returns the config codegen injects into the
generated app: the public SDK key, the placement lookup key and the product ids.

The Apphud SDK auto-detects sandbox vs production from the StoreKit environment, so
``mode`` is informational only (``sandbox`` for the test profile, ``production``
otherwise). Returns ``None`` when Apphud is disabled or no SDK key is configured
(the paywall then falls back to a local stand-in).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.apphud")


def load_key(settings: Settings) -> str:
    """Return the Apphud SDK key from the secrets file, env or settings field."""
    path = settings.apphud_secrets_path
    if path and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line.startswith("APPHUD_API_KEY") and "=" in line:
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:  # an empty placeholder must not shadow the env fallback
                    return value
    return settings.apphud_api_key or os.environ.get("APPHUD_API_KEY", "")


def _slug(value: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in value.lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "app"


def _store_slug(value: str) -> str:
    """Slug valid in an App Store product id (alphanumeric + underscore only, no hyphen)."""
    out = "".join(c if c.isalnum() else "_" for c in value.lower()).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "item"


def bundle_id_for(spec: dict[str, Any], settings: Settings) -> str:
    """Deterministic bundle id for the clone (matches the CodeMagic naming scheme)."""
    name = str(spec.get("app_name") or spec.get("package") or "app")
    return f"{settings.codemagic_bundle_prefix}.{_slug(name).replace('-', '')}"


def _packages(spec: dict[str, Any]) -> list[dict[str, Any]]:
    mon = spec.get("monetization")
    pkgs = mon.get("packages") if isinstance(mon, dict) else None
    return [p for p in pkgs if isinstance(p, dict)] if isinstance(pkgs, list) else []


def provision(
    spec: dict[str, Any],
    *,
    settings: Settings,
    bundle_id: str | None = None,
    app_name: str | None = None,
    sandbox: bool | None = None,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """Return the Apphud config codegen injects for this clone (no remote calls).

    ``bundle_id`` / ``app_name`` override the values derived from ``spec`` (the
    build-profile identity). ``sandbox`` selects the informational mode label
    (``True`` → sandbox, the test profile). ``api_key`` overrides the resolved SDK
    key (per-job, so each clone uses its own app's key). Returns ``None`` when Apphud
    is disabled or no SDK key is configured.
    """
    if not settings.apphud_provision:
        return None
    key = api_key or load_key(settings)
    if not key:
        log.info("apphud.skip", reason="no api_key")
        return None

    app_name = str(app_name or spec.get("app_name") or "App")
    slug = _slug(app_name)
    bundle_id = bundle_id or bundle_id_for(spec, settings)
    if sandbox is None:
        sandbox = False
    placement = settings.apphud_placement or f"main_{slug}"

    store_ids = [
        f"{bundle_id}.{_store_slug(str(p.get('name') or 'premium'))}" for p in _packages(spec)
    ]

    result = {
        "sdk_key": key,
        "placement": placement if store_ids else "",
        "products": store_ids,
        "bundle_id": bundle_id,
        "mode": "sandbox" if sandbox else "production",
    }
    log.bind(stage="apphud", bundle_id=bundle_id).info(
        "apphud.done",
        mode=result["mode"],
        products=len(store_ids),
        placement=result["placement"],
    )
    return result
