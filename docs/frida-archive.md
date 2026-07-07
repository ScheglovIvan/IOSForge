# Frida Capture Archive — Contract v1.1

Shared, versioned contract between the **Frida dynamic-capture tool** (producer) and
**IOSForge** (consumer). The capture is the **single source of truth** for cloning: everything
the generator needs must be in the archive — there is no re-capture.

## Principles

1. **Raw over synthesis.** The archive carries observations (requests, bodies, hierarchies,
   strings), not final semantic conclusions. IOSForge derives the App Spec v3 (`app_spec.json`)
   from the raw with its own versioned prompt, so analysis stays re-runnable. Any capture-side
   synthesis lives in `capture_hint.json` as a non-authoritative prior.
2. **Capabilities are honest.** `manifest.capabilities` declares which signals exist. IOSForge
   reads it and degrades: missing signal → `source: "inferred"`, never fabricated.
3. **Correlation-first.** Everything joins by `request_id`; every screen has a stable in-run `id`.
4. **Deterministic & flat.** Sorted keys, relative paths, one JSON object per `.jsonl` line.
   Archive root == run directory contents (unpacks straight into the analysis working dir).
5. **Sanitized.** Secrets/PII are redacted to `<REDACTED>` with **keys and types preserved**
   (the contract needs structure, not secrets). Redaction ships with the collector, never later.

## Layout (flat, at archive root)

```
manifest.json          # schema_version, app identity, device, counts, capabilities, integrity
screens.json           # screen map (canonical schema below)
screens/<id>.png       # one screenshot per screen
source/<id>.json       # native view/a11y dump per screen (v1.1: rich node tree, see below)
network.jsonl          # one transaction (or request_only) per line
json_bodies.jsonl      # response bodies (text/JSON only), joined by request_id
links.json             # derived: app_endpoints + ad/analytics hosts
media.json             # content URLs (v1.1: + byte-backed path/sha256/bytes/role)
fonts.json             # v1.1: fonts used per screen; custom → fonts/<name>.ttf, system → null
fonts/<name>.ttf       # v1.1: bundled custom font files
media/<sha256>.<ext>   # v1.1: deduped media bytes (mp4/jpg/png/gif) referenced by media.json
subscriptions.json     # StoreKit-1 products + purchase attempts
ads_raw.json           # ad SDK ready-hooks + ad_events
sdks.json              # derived: detected SDKs + host/class evidence  (optional)
capture_hint.json      # optional non-authoritative capture-side synthesis (was app_report.json)
```

## manifest.json

```jsonc
{ "schema_version": "1.0",
  "capture_tool": "iosforge-frida-capture/<ver>",
  "app":     { "bundle_id": "…", "appstore_id": "…"|null, "version": "…", "build": "…", "platform": "ios" },
  "device":  "iPhone… / iOS …",
  "session": { "start": "<ISO-8601>", "end": "<ISO-8601>" },
  "counts":  { "screens": 8, "requests": 67, "transactions": 43, "media": 212 },
  "capabilities": {
    "network": "transaction",          // transaction | request_only | none
    "response_bodies": "json_only",    // json_only | all | none
    "response_meta": true,             // status/mime/headers/latency present on transactions
    "correlation": true,               // request_id joins network↔bodies↔media↔ads
    "view_hierarchy": "uikit",         // uikit | none  (per-screen: see screens.json.ui_kind)
    "accessibility_ids": "synthetic",  // identifier | synthetic | none
    "event_timeline": false,
    "storekit": "v1",                  // v1 | v2 | none
    "entitlements": "partial",         // full | partial | none  (partial = via RevenueCat traffic)
    "storage": false, "auth": false, "design_tokens": false,
    "sanitized": true,
    "screen_id_stable_cross_run": false,
    "coverage": "manual",              // manual | auto_bfs
    "coverage_metric": null
  },
  "redaction": ["req.Authorization", "body.app_user_id", "query.sdk_key"],
  "integrity": { "algo": "sha256", "files": { "screens.json": "…" } } }
```

`capabilities` is normative: consumers **must** branch on it. `entitlements: "partial"` means
entitlements are observable from the RevenueCat `/v1/subscribers` response body, not StoreKit.

## screens.json

```jsonc
{ "bundle_id": "com.x.y", "screen_count": 8, "screens": [
  { "id": "0003",
    "screenshot": "screens/0003.png",
    "ui_kind": "uikit",                       // uikit → elements present · swiftui → elements: []
    "view_controller": "SeriesDetailVC",
    "signature": "a1b2c3…",                   // structural hash → dedup / coverage
    "texts": ["Play", "Season 1", …],
    "elements": [
      { "class": "UIButton", "text": "Play",
        "accessibility_id": "playBtn",         // accessibilityIdentifier || synthetic hash
        "id_synthetic": false,
        "bounds": [x, y, w, h],
        "role": "button", "value": null } ],
    "native_ads": [ … ],
    "from": "0001",                            // incoming edge (invert of navigates_to; null for root)
    "navigates_to": [ { "to": "0009", "via_element": "playBtn" } ] } ] }
```

- **SwiftUI screens** (`ui_kind: "swiftui"`) expose no reliable tree → `elements: []`; IOSForge
  recovers layout from the screenshot via vision (`source: "inferred"`).
- `accessibility_id` is stable **within a run** (one archive = one capture = one source of truth);
  cross-run stability is not required (`capabilities.screen_id_stable_cross_run: false`).

`source/<id>.json` is the full native dump (`{bundle, size, elements[], texts[], ads[]}`) — a
richer layer the adapter merges into the elements above.

## network.jsonl

One line per network task. `phase` distinguishes a completed transaction from a request seen only
at `resume`. **All fields are always present**; response fields are `null` when `phase == "request_only"`.

| field | type | notes |
|---|---|---|
| `id` | str | == `request_id`, join key everywhere |
| `phase` | enum | `transaction` (has response) · `request_only` (request only) |
| `t` | int | epoch **milliseconds** |
| `screen` | str | active screen id when the task started |
| `initiator` | enum | `resume` · `completion` · `delegate` (capture path) |
| `method` `url` | str | request line; `url` sanitized (secret query values redacted) |
| `host` `path` | str | parsed from `url` |
| `query` | obj | parsed; multi-values collapse to the first (rare) |
| `req_headers` `resp_headers` | obj | sanitized by header name |
| `req_body` | str\|null | body as a (sanitized) JSON string; null if none |
| `status` | int\|null | HTTP status `100`–`599` (`200`/`304`/…); **`null` when no HTTP response was observed** (transport error / delegate completion without `NSHTTPURLResponse`) — never `0` |
| `mime` | str\|null | response content-type |
| `size` | int\|null | response body bytes |
| `body_captured` | bool | true if the response body was stored (JSON/text only) |
| `truncated` | bool | true if the stored body was cut at the 400 KB cap |
| `resp_body_ref` | str\|null | `"json_bodies.jsonl#<request_id>"` for text/JSON; else null |
| `latency_ms` | int\|null | real (task start → completion) |

`json_bodies.jsonl`: `{ "request_id", "screen", "mime", "body" }` — text/JSON responses only,
sanitized. When `truncated: true` the `body` may be **invalid JSON** — consumers must guard the parse.

### Examples

```jsonc
// transaction, JSON (completion)
{"id":"r7","phase":"transaction","t":1783331021044,"screen":"0002","initiator":"completion",
 "method":"POST","url":"https://ms.applovin.com/5.0/i?sdk_key=<REDACTED>&p=1%3A578efbfe",
 "host":"ms.applovin.com","path":"/5.0/i","query":{"p":"1:578efbfe","sdk_key":"<REDACTED>"},
 "req_headers":{"Authorization":"<REDACTED>","Content-Type":"application/json"},
 "req_body":"{\"device\":{\"idfv\":\"<REDACTED>\"}}",
 "status":200,"mime":"application/json","resp_headers":{"Content-Type":"application/json"},
 "size":12858,"body_captured":true,"truncated":false,"resp_body_ref":"json_bodies.jsonl#r7","latency_ms":292}

// request_only (all response fields null, still present)
{"id":"r12","phase":"request_only","t":1783331022310,"screen":"0002","initiator":"resume",
 "method":"POST","url":"https://api16-…tiktokpangle.us/api/ad/union/sdk/strategies/adn?aid=5001121",
 "host":"api16-…tiktokpangle.us","path":"/api/ad/union/sdk/strategies/adn","query":{"aid":"5001121"},
 "req_headers":{"x-auth-token":"<REDACTED>"},"req_body":null,
 "status":null,"mime":null,"resp_headers":null,"size":null,
 "body_captured":false,"truncated":false,"resp_body_ref":null,"latency_ms":null}

// non-JSON transaction (delegate) — status/mime present, no json_bodies entry
{"id":"r5","phase":"transaction","t":1783331020490,"screen":"0002","initiator":"delegate",
 "method":"POST","url":"https://muf.app-ads-services.com/muf/psm/groupids?version=1.0",
 "host":"muf.app-ads-services.com","path":"/muf/psm/groupids","query":{"version":"1.0"},
 "req_headers":{"Content-Type":"application/x-protobuf"},"req_body":null,
 "status":200,"mime":"application/x-protobuf","resp_headers":{"Content-Type":"application/x-protobuf"},
 "size":66919,"body_captured":true,"truncated":false,"resp_body_ref":null,"latency_ms":476}
```

## media.json / links.json / subscriptions.json / ads_raw.json / sdks.json

```jsonc
// media.json — one per content URL
{"url":"https://cdn…/Anime_34.jpeg","request_id":"r31","screen":"0002",
 "kind":"image","content_type":"image/jpeg","signed_url":false}

// links.json — derived index
{"app_endpoints":[{"url":"…","host":"…","first_screen":"0000"}],
 "ad_analytics_hosts":[{"host":"…","hits":12}]}

// subscriptions.json — StoreKit 1
{"products":[{"id":"…","price":"…","period":"…","intro":"…"}],"purchase_attempts":[],"note":"…"}

// ads_raw.json
{"ready_hooks":{…},"ad_events":[{"provider":"AppLovin","format":"interstitial","screen":"0002","request_id":"r7"}]}

// sdks.json — derived, optional
{"sdks":[{"sdk":"RevenueCat","evidence":["api.revenuecat.com"]},
         {"sdk":"AppLovin","evidence":["ms.applovin.com"]}]}
```

## Sanitization (mandatory, ships with the collector)

Replace **values** with `<REDACTED>`; keep keys, types and structure.

- **Headers** (req + resp), by name: `authorization`, `proxy-authorization`, `cookie`, `set-cookie`,
  `*-api-key`, `*-auth-token`, `*-access-token`, `x-csrf-token`, `token`, `refresh-token`.
- **Query params**, by name: `*key*`, `token`, `secret`, `sig`, `auth`, `password`, `access_token`.
- **JSON body keys**: `password`, `token`, `secret`, `api_key`, `*_token`, `email`, `phone`, `cvv`,
  `otp`, `card*`, `app_user_id`, `idfv`, `idfa`.
- URL path segments carrying an identity (e.g. RevenueCat `$RCAnonymousID:…`) are redacted in both
  `url` and `path`.
- `manifest.redaction` lists what was stripped (audit). The presence of an `Authorization` header is
  itself the signal (“requires bearer auth”) — the value is not needed.

## Capability tiers → how IOSForge degrades

| tier | archive provides | IOSForge behaviour |
|---|---|---|
| 0 | screens + elements only | UI + navigation; backend `inferred`, `TODO(contract)` |
| 1 | + `network` transactions (JSON) | real API contract → `handoff/openapi.yaml`; `data_model` observed |
| 2 | + hierarchy / events / assets / storage / auth | max UI fidelity + observed design tokens + EARS from real flows |

`entitlements: "partial"` (RevenueCat body) and `sdks.json` let monetization + backend-deliverable
selection (RevenueCat / Firebase / AppLovin / Pangle) be **evidence-based**, not guessed. Non-JSON
responses (protobuf/gRPC/binary) contribute endpoint + `mime` + `size` only; no schema is derived.

## JSON Schema (normative — Draft 2020-12)

Machine-checkable schemas live beside the App Spec contract in `iosforge/mvp/frida_contract.py`
(`MANIFEST_SCHEMA`, `SCREENS_SCHEMA`, `NETWORK_RECORD_SCHEMA`, and the v1.1 `SOURCE_SCHEMA`,
`FONTS_SCHEMA`, `MEDIA_ENTRY_SCHEMA`) with `validate_archive()`. The prose above is the human
reference; the Python module is the enforced form (mirrors `spec_contract.py`).

## v1.1 (additive) — rich hierarchy + fonts + media bytes

v1.1 is a **minor** bump: back-compatible, consumers accept any `1.x`, and every v1.0 archive still
validates unchanged. It lifts copy fidelity by shipping structure and real assets instead of leaving
the generator to guess from flat screenshots. Three additions, each gated by a capability flag:

- **`source/<id>.json` becomes a rich view hierarchy** when `capabilities.view_hierarchy:"uikit-rich"`:
  a recursive `root`/`children` node tree. Each node carries `class`, `frame` (`{x,y,w,h}` in
  **points**, origin top-left, screen coordinates), plus optional `text`, `font`
  (`postscript_name`/`family`/`point_size`/`weight` 100–900/`italic`), `colors`
  (`text`/`background`/`tint` as sRGB `#RRGGBBAA`), `layer`
  (`corner_radius`/`border_width`/`border_color`/`opacity`/`shadow`), `content_mode`, `asset_ref`
  (`bundle_asset` or `media_request_id` — joins into `media.json`), and `scroll`
  (`item_count`/`cell_class`/`axis`). Top-level: `scale` (px = pt·scale), `screen_size`,
  `safe_area_insets`, `node_count`.
- **`fonts.json`** when `capabilities.fonts:true`: an array of
  `{postscript_name, family, file, is_system}`. Custom (non-system) fonts **must** reference a
  bundled `fonts/<name>.ttf`; system fonts set `file:null`, `is_system:true`.
- **`media.json` entries carry bytes** when `capabilities.media_bytes:true`: v1.0 url-only entries
  gain `path` (`media/<sha256>.<ext>`, deduped by content), `sha256`, `bytes`, optional
  `role` (`splash_background`/`paywall_hero`/`thumbnail`/`icon`/… or `null`), and `skipped_large`.
  Media is captured from **three sources** (`media_source` capability): `network` (over the wire),
  `bundle` (embedded in the `.app` — `NSBundle` files, compiled `Assets.car`, `AVPlayer file://`,
  Lottie/Rive), and `runtime-snapshot` (a node's rendered `CALayer.contents`/`UIImageView.image`,
  the fallback). Bundle/snapshot entries have **no `url`** and `request_id:null`; every entry has a
  stable `id`. `counts.media_bundle`/`media_network`/`media_runtime` break the totals down.

- **Node ↔ bytes join.** Each image/video/animation node gets a `node_id`, and its `asset_ref`
  gains `media_id` (points at `media.json[].id`) plus `resolved` (`false` when the bytes could not
  be recovered for that node). This is why bundle splash/paywall assets — which never touch the
  network — can still be embedded in the clone: the generator resolves `node.asset_ref.media_id →
  media/<sha256>`. **Limitation (honest):** for SwiftUI apps, splash/paywall are overlays inside a
  single `UIHostingController`, invisible to the view hierarchy and VC chain — their bytes are in
  the archive (by name, `source:bundle`, with a `role`), but per-node hero join is unreachable via
  Frida introspection (`resolved:false`, `screen` may be `null`). The node join is exact on UIKit
  screens; for SwiftUI the consumer composites from `screens/<id>.png` + bundle bytes by `role`/name.

New capability flags: `fonts`, `media_bytes`, `layout_geometry` (booleans), `media_source` (array);
`view_hierarchy` gains `"uikit-rich"`; `ui_kind` gains `"hybrid"`. On ingest IOSForge validates the
v1.1 payload and materializes `fonts.json`/`fonts/*.ttf` and `media/*` into the run dir,
cross-checking that every referenced `font.file` and `media.path` is present in the archive
(integrity already sha256-verifies the full file-set); the per-screen network index carries each
media entry's `id`/`source`/`path`/`role` for downstream codegen.

## Versioning

`schema_version` is `MAJOR.MINOR`. Consumers reject an unknown **major**. Adding an optional field
or capability is a **minor** bump; renaming/removing a required field or changing a type is **major**.
