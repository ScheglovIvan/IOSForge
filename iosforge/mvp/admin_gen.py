"""Generate the admin/backend deliverable (Firebase + Rowy + RevenueCat + Stream).

Emitted alongside the Flutter client when ``app_spec.backend.admin_panel_needed``
is true. Deterministic (no secrets, no network): derives a Firestore schema from
``content.data_model``, RevenueCat products from ``monetization.packages``, a Rowy
table config, security-rules and seed skeletons, and a PROVISION runbook. Actually
creating the Firebase project (provision mode) is a separate credentialed step.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger
from iosforge.mvp import ad_scaffold, backend_gen

log = get_logger("mvp.admin_gen")

_PRIVATE_ENTITIES = {"user", "transaction", "subscription", "watchlistentry", "rewardtask"}


def _field_type(field: str) -> str:
    f = field.lower()
    if f.endswith("[]") or f.endswith("s") and f in ("genres", "tags", "relations"):
        return "array"
    if f.startswith(("is", "has", "auto")) or f in ("isguest", "islocked"):
        return "boolean"
    if any(t in f for t in ("count", "balance", "amount", "cost", "number", "day", "sec", "price")):
        return "number"
    if f.endswith("at") or "date" in f or "expiry" in f:
        return "timestamp"
    return "string"


def _collections(spec: dict[str, Any]) -> dict[str, dict[str, str]]:
    data_model = spec.get("content", {}).get("data_model", []) or []
    out: dict[str, dict[str, str]] = {}
    for entity in data_model:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("entity", "")).strip()
        if not name:
            continue
        fields = entity.get("fields", []) or []
        out[name] = {str(f).replace("[]", ""): _field_type(str(f)) for f in fields}
    return out


def _has_video(spec: dict[str, Any], collections: dict[str, dict[str, str]]) -> bool:
    blob = json.dumps(collections).lower()
    if "video" in blob or "stream" in blob:
        return True
    return spec.get("app_type") in ("content-subscription", "media")


def _firestore_rules(collections: dict[str, dict[str, str]]) -> str:
    """Real per-entity rules: owner-scoped user data, ledger writes only via
    Cloud Functions (admin SDK bypasses rules), public-read catalog."""
    lines = [
        "rules_version = '2';",
        "service cloud.firestore {",
        "  match /databases/{database}/documents {",
        "    function isSignedIn() { return request.auth != null; }",
        "    function isOwner(uid) { return isSignedIn() && request.auth.uid == uid; }",
        "    function ownsDoc() { return isSignedIn() && resource.data.userId == request.auth.uid; }",
        "",
    ]
    for name in collections:
        low = name.lower()
        if low.endswith("user"):
            lines += [
                f"    match /{name}/{{uid}} {{",
                "      allow read: if isOwner(uid);",
                "      allow write: if false;  // coins/pro mutated only by Cloud Functions",
                "      match /unlocks/{ep} { allow read: if isOwner(uid); allow write: if false; }",
                "    }",
            ]
        elif any(low.endswith(e) for e in ("transaction", "subscription", "rewardtask", "wallet")):
            lines += [
                f"    match /{name}/{{id}} {{",
                "      allow read: if ownsDoc();",
                "      allow write: if false;  // ledger — Cloud Functions only",
                "    }",
            ]
        elif low.endswith("watchlistentry"):
            lines += [
                f"    match /{name}/{{id}} {{",
                "      allow read: if ownsDoc();",
                "      allow create: if isSignedIn() && request.resource.data.userId == request.auth.uid;",
                "      allow update, delete: if ownsDoc();",
                "    }",
            ]
        else:  # catalog: public read, content written by admin SDK / functions only
            lines += [
                f"    match /{name}/{{id}} {{",
                "      allow read: if true;",
                "      allow write: if false;",
                "    }",
            ]
    lines += [
        "    match /{document=**} { allow read, write: if false; }",
        "  }",
        "}",
        "",
    ]
    return "\n".join(lines)


def _rowy_config(collections: dict[str, dict[str, str]]) -> dict[str, Any]:
    type_map = {
        "string": "SingleLineText",
        "number": "Number",
        "boolean": "Checkbox",
        "timestamp": "DateTime",
        "array": "Array",
    }
    return {
        "tables": [
            {
                "id": name,
                "name": name.capitalize(),
                "collection": name,
                "roles": ["ADMIN", "EDITOR"],
                "columns": [
                    {"key": field, "name": field, "type": type_map.get(ftype, "SingleLineText")}
                    for field, ftype in fields.items()
                ],
            }
            for name, fields in collections.items()
        ]
    }


def _revenuecat_products(spec: dict[str, Any]) -> dict[str, Any]:
    packages = spec.get("monetization", {}).get("packages", []) or []
    products = []
    for p in packages:
        if not isinstance(p, dict):
            continue
        period = str(p.get("period", "")).lower()
        ptype = "subscription" if period in ("week", "month", "year") else "consumable"
        products.append(
            {
                "identifier": str(p.get("name", "product")).lower().replace(" ", "_")[:64],
                "display_name": p.get("name"),
                "price": p.get("price"),
                "period": p.get("period"),
                "type": ptype,
                "includes": p.get("includes", []),
            }
        )
    return {
        "entitlements": ["pro"],
        "offerings": [{"identifier": "default", "packages": [p["identifier"] for p in products]}],
        "products": products,
    }


def _has_ads(spec: dict[str, Any]) -> bool:
    mon = spec.get("monetization", {})
    if isinstance(mon, dict):
        if str(mon.get("model", "")).lower() in ("ads", "mixed"):
            return True
        if mon.get("ad_placements"):
            return True
    return False


def _ads_config(spec: dict[str, Any]) -> dict[str, Any]:
    mon = spec.get("monetization", {}) if isinstance(spec.get("monetization"), dict) else {}
    placements = mon.get("ad_placements", []) or []
    ad_units = [
        {
            "placement": str(p.get("screen_context") or p.get("trigger") or "default"),
            "format": p.get("format"),
            "unit_id_ios": "ca-app-pub-XXXXXXXXXXXXXXXX/XXXXXXXXXX",
            "unit_id_android": "ca-app-pub-XXXXXXXXXXXXXXXX/XXXXXXXXXX",
        }
        for p in placements
        if isinstance(p, dict)
    ] or [
        {
            "placement": "default",
            "format": "interstitial",
            "unit_id_ios": "ca-app-pub-XXXXXXXXXXXXXXXX/XXXXXXXXXX",
            "unit_id_android": "ca-app-pub-XXXXXXXXXXXXXXXX/XXXXXXXXXX",
        },
        {
            "placement": "home",
            "format": "banner",
            "unit_id_ios": "ca-app-pub-XXXXXXXXXXXXXXXX/XXXXXXXXXX",
            "unit_id_android": "ca-app-pub-XXXXXXXXXXXXXXXX/XXXXXXXXXX",
        },
    ]
    return {
        "admob": {
            "app_id_ios": "ca-app-pub-XXXXXXXXXXXXXXXX~XXXXXXXXXX",
            "app_id_android": "ca-app-pub-XXXXXXXXXXXXXXXX~XXXXXXXXXX",
        },
        "ad_units": ad_units,
        "mediation": {
            "note": "Detected networks are best-effort from screen creatives only; configure "
            "mediation (AppLovin MAX / ironSource / AdMob) in the AdMob console. Exact SDK "
            "requires APK static analysis (not performed).",
            "detected_networks": mon.get("ad_networks", []),
        },
        "frequency": "interstitial every 3 screens (tune in Remote Config)",
        "pro_disables_ads": True,
    }


def _seed_script(collections: dict[str, dict[str, str]]) -> str:
    return (
        '"""Seed Firestore with placeholder documents (run after provisioning).\n\n'
        "Usage: GOOGLE_APPLICATION_CREDENTIALS=sa.json python seed_empty.py\n"
        '"""\n\n'
        "import firebase_admin\n"
        "from firebase_admin import firestore\n\n"
        "firebase_admin.initialize_app()\n"
        "db = firestore.client()\n\n"
        f"COLLECTIONS = {json.dumps(collections, indent=2)}\n\n"
        "def placeholder(ftype):\n"
        "    return {'string': 'PLACEHOLDER', 'number': 0, 'boolean': False,\n"
        "            'timestamp': firestore.SERVER_TIMESTAMP, 'array': []}[ftype]\n\n"
        "for name, fields in COLLECTIONS.items():\n"
        "    for i in range(3):\n"
        "        doc = {k: placeholder(t) for k, t in fields.items()}\n"
        "        db.collection(name).add(doc)\n"
        "        print(f'seeded {name} #{i}')\n"
    )


def generate_admin(
    spec: dict[str, Any],
    out_dir: Path,
    *,
    collection_prefix: str = "",
    project_id: str = "app",
) -> Path:
    """Write the ``admin/`` deliverable into ``out_dir``; return it.

    ``collection_prefix`` namespaces every Firestore collection (multi-tenant
    shared-project mode) so unrelated apps don't collide in one project.
    ``project_id`` is baked into the deployable ``firebase.json``/``.firebaserc``.
    """
    collections = _collections(spec)
    if collection_prefix:
        collections = {f"{collection_prefix}{name}": fields for name, fields in collections.items()}
    has_video = _has_video(spec, collections)
    has_ads = _has_ads(spec)
    functions_dir = backend_gen.generate_backend(
        spec, out_dir, collection_prefix=collection_prefix, project_id=project_id
    )
    has_backend = functions_dir is not None
    app_name = str(spec.get("app_name", "app"))

    (out_dir / "firestore").mkdir(parents=True, exist_ok=True)
    (out_dir / "rowy").mkdir(parents=True, exist_ok=True)
    (out_dir / "revenuecat").mkdir(parents=True, exist_ok=True)
    (out_dir / "seed").mkdir(parents=True, exist_ok=True)
    (out_dir / "config").mkdir(parents=True, exist_ok=True)

    (out_dir / "firestore" / "collections.schema.json").write_text(
        json.dumps({"collections": collections}, indent=2, ensure_ascii=False)
    )
    (out_dir / "firestore" / "firestore.rules").write_text(_firestore_rules(collections))
    (out_dir / "firestore" / "indexes.json").write_text(json.dumps({"indexes": []}, indent=2))
    (out_dir / "rowy" / "rowy.config.json").write_text(
        json.dumps(_rowy_config(collections), indent=2, ensure_ascii=False)
    )
    (out_dir / "revenuecat" / "products.json").write_text(
        json.dumps(_revenuecat_products(spec), indent=2, ensure_ascii=False)
    )
    (out_dir / "revenuecat" / "extension.md").write_text(
        "# RevenueCat + Firebase\n\nInstall the official RevenueCat Firebase Extension to sync "
        "subscriber entitlements into Firestore (`customers/{uid}`). Configure products from "
        "`products.json` in the RevenueCat dashboard and map them to the `pro` entitlement.\n"
    )
    (out_dir / "config" / "remote_config.template.json").write_text(
        json.dumps(
            {
                "paywall_variant": "default",
                "free_episode_count": 3,
                "ads_enabled": has_ads,
                "ads_frequency_interstitial": 3,
                "ads_disabled_for_pro": True,
            },
            indent=2,
        )
    )
    (out_dir / "seed" / "seed_empty.py").write_text(_seed_script(collections))

    if has_ads:
        (out_dir / "ads").mkdir(parents=True, exist_ok=True)
        (out_dir / "ads" / "admob.config.json").write_text(
            json.dumps(_ads_config(spec), indent=2, ensure_ascii=False)
        )
        (out_dir / "ads" / "README.md").write_text(
            "# Ads (AdMob + mediation)\n\n"
            "- `admob.config.json` — placeholder AdMob app ids + one ad unit per placement.\n"
            "- One `ad_units` entry per `monetization.ad_placements` from the App Spec.\n"
            "- Frequency is served from Remote Config (`ads_frequency_interstitial`).\n"
            "- Ads are disabled for the RevenueCat `pro` entitlement "
            "(`ads_disabled_for_pro`).\n"
            "- `mediation.detected_networks` is best-effort from screen creatives; the exact "
            "mediation SDK requires APK static analysis (not performed).\n"
            "- `flutter/` — ready google_mobile_ads client bundle (drop into the Flutter app).\n"
        )
        ad_scaffold.build_ad_bundle(spec, out_dir / "ads" / "flutter")

    if has_video:
        (out_dir / "stream").mkdir(parents=True, exist_ok=True)
        (out_dir / "stream" / "README.md").write_text(
            "# Video delivery (Cloudflare Stream / Mux / Bunny)\n\n"
            "Video content detected. Do NOT store episodes in Firebase Storage.\n\n"
            "- Upload episodes to Cloudflare Stream (or Mux/Bunny); enable signed URLs.\n"
            "- Store only `streamId` / `videoUrl` on the episode document in Firestore.\n"
            "- Generate short-lived signed playback URLs from a Cloud Function.\n"
        )

    manifest = {
        "provider": "firebase_rowy",
        "app_name": app_name,
        "generated_at": datetime.now(UTC).isoformat(),
        "includes_video_stream": has_video,
        "includes_ads": has_ads,
        "includes_backend": has_backend,
        "collection_prefix": collection_prefix,
        "collections": list(collections),
        "provision_ready": False,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    (out_dir / "README.md").write_text(
        f"# {app_name} — Admin & Backend deliverable\n\n"
        "Stack: **Firebase** (data/auth/storage/functions/push/analytics) + **Rowy** "
        "(content admin) + **RevenueCat** (subscriptions) "
        + ("+ **Cloudflare Stream** (video)" if has_video else "")
        + ".\n\n"
        "- `firestore/` — collections schema, security rules, indexes.\n"
        "- `rowy/` — Rowy table config (operator admin UI).\n"
        "- `revenuecat/` — products + Firebase Extension notes.\n"
        + ("- `stream/` — video delivery notes.\n" if has_video else "")
        + ("- `ads/` — AdMob config + placements + mediation notes.\n" if has_ads else "")
        + "- `config/` — Remote Config template.\n"
        "- `seed/seed_empty.py` — placeholder seeding.\n"
        "- `PROVISION.md` — credentialed setup steps.\n"
    )
    (out_dir / "PROVISION.md").write_text(
        "# Provisioning (credentialed — run manually)\n\n"
        "1. Create/select a Firebase project (needs a GCP service account with "
        "`firebase.projects.create` + Blaze billing; mind the per-account project quota).\n"
        "2. Enable Firestore, Auth, Storage, Cloud Functions, FCM.\n"
        "3. Deploy `firestore/firestore.rules` and `firestore/indexes.json`.\n"
        "4. Point Rowy at the project and import `rowy/rowy.config.json`.\n"
        "5. Install the RevenueCat Firebase Extension; create products from "
        "`revenuecat/products.json`.\n"
        + (
            "6. Set up Cloudflare Stream and a signed-URL Cloud Function (see `stream/`).\n"
            if has_video
            else ""
        )
        + (
            "7. Create AdMob app + ad units from `ads/admob.config.json`; set mediation.\n"
            if has_ads
            else ""
        )
        + "8. Run `seed/seed_empty.py` to fill placeholders.\n"
    )

    log.info(
        "admin_gen.done",
        app=app_name,
        collections=len(collections),
        video=has_video,
        ads=has_ads,
    )
    return out_dir
