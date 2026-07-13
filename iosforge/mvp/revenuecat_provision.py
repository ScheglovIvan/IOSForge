"""RevenueCat provisioning (v2 API) — one app per generated clone.

The account's v2 secret key is project-scoped (it cannot create projects), so every
clone becomes a new **app** inside a single shared project
(``settings.revenuecat_project_id``); products / entitlements / offerings are
project-level and isolated per clone by a name suffix (``<lookup>_<slug>``).

Runs BEFORE codegen so the app's **public SDK key** (``appl_…``) can be injected
into the Flutter app. App Store linking (ASC key / receipt validation) is out of
scope for now — the app is created with a bundle id only, so purchases are testable
via StoreKit local/sandbox but do not validate against a live store yet.

Best-effort: the app + public key are required (a failure returns ``None`` and the
pipeline continues without RC); products/entitlement/offering are created on a
best-effort basis and never fail the build.
"""

from __future__ import annotations

from typing import Any

import httpx

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.revenuecat")


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


def _create(
    client: httpx.Client, bound: Any, kind: str, url: str, body: dict[str, Any]
) -> str | None:
    """POST a create call; return the new resource id, or None on any failure."""
    try:
        resp = client.post(url, json=body)
        resp.raise_for_status()
        return str(resp.json().get("id") or "") or None
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else ""
        bound.warning("revenuecat.create_failed", kind=kind, error=str(exc), detail=detail)
        return None


def provision(
    spec: dict[str, Any], *, settings: Settings, timeout: float = 30.0
) -> dict[str, Any] | None:
    """Create an RC app (+ products/entitlement/offering) for this clone.

    Returns a config dict (``app_id``, ``apple_public_key``, ``entitlement``,
    ``offering``, ``products``, ``bundle_id``) for codegen to wire, or ``None`` when
    RC is disabled/unconfigured or the app+key could not be created.
    """
    if not settings.revenuecat_provision:
        return None
    if not settings.revenuecat_api_key or not settings.revenuecat_project_id:
        log.info("revenuecat.skip", reason="no api_key or project_id")
        return None

    pid = settings.revenuecat_project_id
    app_name = str(spec.get("app_name") or "App")
    slug = _slug(app_name)
    bundle_id = bundle_id_for(spec, settings)
    bound = log.bind(stage="revenuecat", project_id=pid, bundle_id=bundle_id)

    headers = {
        "Authorization": f"Bearer {settings.revenuecat_api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(base_url=settings.revenuecat_api_base, headers=headers, timeout=timeout) as c:
        try:
            app = c.post(
                f"/projects/{pid}/apps",
                json={
                    "name": app_name[:255],
                    "type": "app_store",
                    "app_store": {"bundle_id": bundle_id},
                },
            )
            app.raise_for_status()
            app_id = str(app.json()["id"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            bound.warning("revenuecat.app_create_failed", error=str(exc))
            return None

        try:
            keys = c.get(f"/projects/{pid}/apps/{app_id}/public_api_keys")
            keys.raise_for_status()
            items = keys.json().get("items", [])
            apple_public_key = str(items[0]["key"]) if items else ""
        except (httpx.HTTPError, KeyError, ValueError, IndexError) as exc:
            bound.warning("revenuecat.public_key_failed", app_id=app_id, error=str(exc))
            apple_public_key = ""

        bound.info("revenuecat.app_created", app_id=app_id, has_key=bool(apple_public_key))

        entitlement = f"premium_{slug}"
        offering = f"default_{slug}"
        product_ids: list[str] = []
        store_ids: list[str] = []
        packages = _packages(spec)

        if packages:
            for pkg in packages:
                pname = str(pkg.get("name") or "premium")
                store_id = f"{bundle_id}.{_store_slug(pname)}"
                ptype = "subscription" if pkg.get("period") else "one_time"
                created = _create(
                    c,
                    bound,
                    "product",
                    f"/projects/{pid}/products",
                    {
                        "store_identifier": store_id,
                        "app_id": app_id,
                        "type": ptype,
                        "display_name": pname[:255],
                    },
                )
                if created:
                    product_ids.append(created)
                    store_ids.append(store_id)

            ent_id = _create(
                c,
                bound,
                "entitlement",
                f"/projects/{pid}/entitlements",
                {"lookup_key": entitlement, "display_name": f"Premium ({app_name})"[:255]},
            )
            if ent_id and product_ids:
                _create(
                    c,
                    bound,
                    "attach_entitlement",
                    f"/projects/{pid}/entitlements/{ent_id}/actions/attach_products",
                    {"product_ids": product_ids},
                )

            off_id = _create(
                c,
                bound,
                "offering",
                f"/projects/{pid}/offerings",
                {"lookup_key": offering, "display_name": f"Default ({app_name})"[:255]},
            )
            if off_id:
                for store_id in store_ids:
                    _create(
                        c,
                        bound,
                        "package",
                        f"/projects/{pid}/offerings/{off_id}/packages",
                        {"lookup_key": "$rc_custom", "display_name": store_id[:255]},
                    )

        test_key = settings.revenuecat_test_store_key
        result = {
            "project_id": pid,
            "app_id": app_id,
            "apple_public_key": apple_public_key,
            # The key codegen configures the SDK with: the Test Store key (testable now,
            # no Apple signing) when provided, else the per-clone App Store key.
            "sdk_key": test_key or apple_public_key,
            "mode": "test_store" if test_key else "app_store",
            "bundle_id": bundle_id,
            "entitlement": entitlement if packages else "",
            "offering": offering if packages else "",
            "products": store_ids,
        }
        bound.info(
            "revenuecat.done",
            app_id=app_id,
            mode=result["mode"],
            products=len(store_ids),
            entitlement=result["entitlement"],
        )
        return result
