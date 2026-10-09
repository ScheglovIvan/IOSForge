"""Native distribution integrations of the SwiftUI scaffold (Phase 5).

Renders, as contract code, the app's distribution integrations (wrapped as the core
capability modules by :mod:`iosforge.mvp.swiftui_capabilities`):
Apphud subscriptions and Tenjin attribution through SwiftPM (``ApphudSDK``,
``TenjinSDK``), the App Store encryption declaration, SKAdNetwork identifiers and the
attribution report endpoint. The job-specific inputs (``apphud_config.json`` /
``attribution_config.json`` from provisioning, the export-compliance flag, the
SKAdNetwork plist) are frozen into ``Config/integrations.json`` inside the app so the
compile gate re-renders the same contract every time. Headless screen-id mode never
starts an SDK: ``Subscriptions`` serves fixture products derived from
``app_spec.monetization.packages`` so paywalls render offline for the Vision Judge.
"""

from __future__ import annotations

import json
import plistlib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

INTEGRATIONS_JSON = "Config/integrations.json"
APPHUD_PACKAGE = ("ApphudSDK", "https://github.com/apphud/ApphudSDK", "4.6.0")
TENJIN_PACKAGE = ("TenjinSDK", "https://github.com/tenjin/tenjin-ios-sdk", "1.12.11")
SKAN_REPORT_ENDPOINT = "https://tenjin-skan.com"


@dataclass(frozen=True)
class FixtureProduct:
    """A subscription product shown in headless mode (from app_spec packages)."""

    product_id: str
    title: str
    price: str
    period: str


@dataclass
class Integrations:
    """Frozen per-job integration inputs of one generated app."""

    apphud_key: str = ""
    apphud_placement: str = ""
    tenjin_key: str = ""
    att_usage_description: str = ""
    export_compliance_exempt: bool | None = None
    skadnetwork_ids: list[str] = field(default_factory=list)
    products: list[FixtureProduct] = field(default_factory=list)
    remote_api_url: str = ""

    @property
    def subscriptions(self) -> bool:
        return bool(self.apphud_key)

    @property
    def attribution(self) -> bool:
        return bool(self.tenjin_key)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def skadnetwork_ids(plist_path: Path) -> list[str]:
    """``SKAdNetworkIdentifier`` values from an MMP ``SKAdNetworkItems`` plist (or [])."""
    if not plist_path.is_file():
        return []
    data = plistlib.loads(plist_path.read_bytes())
    items = data.get("SKAdNetworkItems", []) if isinstance(data, dict) else data
    return [
        str(item["SKAdNetworkIdentifier"])
        for item in items or []
        if isinstance(item, dict) and item.get("SKAdNetworkIdentifier")
    ]


def fixture_products(spec: dict[str, Any], store_ids: list[str]) -> list[FixtureProduct]:
    """Headless paywall products: app_spec packages, ids from Apphud provisioning if known."""
    packages = (spec.get("monetization") or {}).get("packages") or []
    products: list[FixtureProduct] = []
    for index, package in enumerate(p for p in packages if isinstance(p, dict)):
        product_id = store_ids[index] if index < len(store_ids) else f"premium.{index + 1}"
        products.append(
            FixtureProduct(
                product_id=product_id,
                title=str(package.get("name") or "Premium"),
                price=str(package.get("price") or ""),
                period=str(package.get("period") or ""),
            )
        )
    return products


def collect(
    spec: dict[str, Any],
    *,
    apphud_config: Path,
    attribution_config: Path,
    skadnetwork_plist: Path,
    export_compliance_exempt: bool | None,
    remote_api_url: str = "",
) -> Integrations:
    """Gather the job's integration inputs (provisioning outputs + build metadata)."""
    apphud = _read_json(apphud_config)
    tenjin = _read_json(attribution_config)
    return Integrations(
        apphud_key=str(apphud.get("sdk_key") or ""),
        apphud_placement=str(apphud.get("placement") or ""),
        tenjin_key=str(tenjin.get("sdk_key") or ""),
        att_usage_description=str(tenjin.get("att_usage_description") or ""),
        export_compliance_exempt=export_compliance_exempt,
        skadnetwork_ids=skadnetwork_ids(skadnetwork_plist) if tenjin.get("sdk_key") else [],
        products=fixture_products(spec, [str(p) for p in apphud.get("products") or []]),
        remote_api_url=remote_api_url,
    )


def write(app_dir: Path, integrations: Integrations) -> None:
    """Freeze ``integrations`` into the app (``Config/integrations.json``)."""
    path = app_dir / INTEGRATIONS_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(integrations), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _product(raw: object) -> FixtureProduct | None:
    if not isinstance(raw, dict):
        return None
    values = {f.name: str(raw.get(f.name) or "") for f in fields(FixtureProduct)}
    return FixtureProduct(**values) if values["product_id"] else None


def load(app_dir: Path) -> Integrations:
    """Integration inputs frozen into ``app_dir``; defaults for a missing or damaged file.

    The file lives inside the model-editable app, so unknown keys, wrong types and
    broken JSON are ignored instead of failing the compile gate. Delivery re-freezes
    it from provisioning before archiving, so a tampered copy never ships.
    """
    try:
        data = _read_json(app_dir / INTEGRATIONS_JSON)
    except (OSError, ValueError):
        data = {}
    defaults = Integrations()
    values: dict[str, Any] = {}
    for name in (
        "apphud_key",
        "apphud_placement",
        "tenjin_key",
        "att_usage_description",
        "remote_api_url",
    ):
        value = data.get(name)
        values[name] = value if isinstance(value, str) else getattr(defaults, name)
    exempt = data.get("export_compliance_exempt")
    values["export_compliance_exempt"] = exempt if isinstance(exempt, bool) else None
    ids = data.get("skadnetwork_ids")
    values["skadnetwork_ids"] = [str(i) for i in ids] if isinstance(ids, list) else []
    raw_products = data.get("products")
    products = [_product(p) for p in raw_products] if isinstance(raw_products, list) else []
    values["products"] = [p for p in products if p is not None]
    return Integrations(**values)


def _swift(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _fixture_literals(products: list[FixtureProduct]) -> str:
    if not products:
        return "        []"
    rows = ",\n".join(
        f"            SubscriptionProduct(id: {_swift(p.product_id)}, title: {_swift(p.title)}, "
        f"price: {_swift(p.price)}, period: {_swift(p.period)})"
        for p in products
    )
    return f"        [\n{rows},\n        ]"


_SUBSCRIPTIONS_HEADER = """import Foundation
{imports}
/// A subscription product as screens show it. Generated by the IOSForge scaffold — do not edit.
struct SubscriptionProduct: Identifiable, Hashable {{
    let id: String
    let title: String
    let price: String
    let period: String
}}

/// The app's single subscription API (Apphud when configured). Paywalls only use this.
/// In headless screen-id mode it never touches StoreKit and serves `fixtures`.
/// Generated by the IOSForge scaffold — do not edit.
@MainActor
enum Subscriptions {{
    static let placement = {placement}

    static let fixtures: [SubscriptionProduct] =
{fixtures}
"""

_SUBSCRIPTIONS_STUB = """
    static func start() {}

    static func products() async -> [SubscriptionProduct] { fixtures }

    static func purchase(_ productID: String) async -> Bool { false }

    @discardableResult
    static func restore() async -> Bool { false }

    static var hasPremium: Bool { false }

    static func paywallShown() {}
}
"""

_SUBSCRIPTIONS_APPHUD = """
    static let apiKey = {key}
    private static var paywall: ApphudPaywall?

    static func start() {{
        guard !Headless.isActive, !apiKey.isEmpty else {{ return }}
        Apphud.start(apiKey: apiKey)
    }}

    static func products() async -> [SubscriptionProduct] {{
        guard !Headless.isActive else {{ return fixtures }}
        let placements = await Apphud.placements()
        let match = placements.first {{ $0.identifier == placement }} ?? placements.first
        guard let found = match?.paywall else {{ return fixtures }}
        paywall = found
        var result: [SubscriptionProduct] = []
        for item in found.products {{
            let product = try? await item.product()
            result.append(
                SubscriptionProduct(
                    id: item.productId,
                    title: product?.displayName ?? item.productId,
                    price: product?.displayPrice ?? "",
                    period: ""
                )
            )
        }}
        return result.isEmpty ? fixtures : result
    }}

    static func purchase(_ productID: String) async -> Bool {{
        guard !Headless.isActive,
              let item = paywall?.products.first(where: {{ $0.productId == productID }}) else {{
            return false
        }}
        return await Apphud.purchase(item).success
    }}

    @discardableResult
    static func restore() async -> Bool {{
        guard !Headless.isActive else {{ return false }}
        return await Apphud.restorePurchases()?.success ?? false
    }}

    static var hasPremium: Bool {{
        !Headless.isActive && Apphud.hasPremiumAccess()
    }}

    static func paywallShown() {{
        guard !Headless.isActive, let paywall else {{ return }}
        Apphud.paywallShown(paywall)
    }}
}}
"""


def render_subscriptions(integrations: Integrations) -> str:
    """``App/Monetization/Subscriptions.swift``: Apphud-backed or a fixture-only stub."""
    header = _SUBSCRIPTIONS_HEADER.format(
        imports="import ApphudSDK\n" if integrations.subscriptions else "",
        placement=_swift(integrations.apphud_placement),
        fixtures=_fixture_literals(integrations.products),
    )
    if integrations.subscriptions:
        return header + _SUBSCRIPTIONS_APPHUD.format(key=_swift(integrations.apphud_key))
    return header + _SUBSCRIPTIONS_STUB


def render_attribution(integrations: Integrations) -> str:
    """``App/Monetization/Attribution.swift``: Tenjin after the ATT gate, or a no-op."""
    if not integrations.attribution:
        return """import Foundation

/// Install attribution (not configured for this app).
/// Generated by the IOSForge scaffold — do not edit.
enum Attribution {
    static func start() async {}
}
"""
    return f"""import Foundation
import TenjinSDK

/// Install attribution: ATT through the permission gate, then Tenjin on every launch.
/// Never runs in headless screen-id mode. Generated by the IOSForge scaffold — do not edit.
enum Attribution {{
    static let sdkKey = {_swift(integrations.tenjin_key)}

    @MainActor
    static func start() async {{
        guard !Headless.isActive, !sdkKey.isEmpty else {{ return }}
        let granted = await Permissions.request(TrackingPermission.self)
        TenjinSDK.initialize(sdkKey)
        if granted {{
            TenjinSDK.optIn()
        }} else {{
            TenjinSDK.optOut()
        }}
        TenjinSDK.connect()
    }}
}}
"""


def app_properties(integrations: Integrations) -> list[str]:
    """App-level Info.plist properties (the App Store encryption declaration)."""
    if integrations.export_compliance_exempt is None:
        return []
    uses = "false" if integrations.export_compliance_exempt else "true"
    return [f"        ITSAppUsesNonExemptEncryption: {uses}"]


def attribution_properties(integrations: Integrations) -> list[str]:
    """Info.plist properties of attribution (ATT text, SKAN report endpoint and ids)."""
    if not integrations.attribution:
        return []
    lines: list[str] = []
    if integrations.att_usage_description:
        usage = _swift(integrations.att_usage_description)
        lines.append(f"        NSUserTrackingUsageDescription: {usage}")
    lines.append(f"        NSAdvertisingAttributionReportEndpoint: {SKAN_REPORT_ENDPOINT}")
    if integrations.skadnetwork_ids:
        lines.append("        SKAdNetworkItems:")
        lines += [
            f"          - SKAdNetworkIdentifier: {_swift(identifier)}"
            for identifier in integrations.skadnetwork_ids
        ]
    return lines
