"""``content_feed`` capability module: a content library gated by the subscription (Phase F).

Reuses the core ``Subscriptions`` module instead of a new SDK. Contract code
``App/Capabilities/ContentFeed.swift``: ``ContentFeed.load()`` returns the items of the
operator's JSON feed (``content_feed_url`` build setting → ``Integrations.content_feed_url``;
a list of ``{id, title, subtitle, body, premium}`` or ``{"items": [...]}``), falling back to
the bundled seed rendered from ``app_spec.content.content_to_seed``; ``ContentFeed.open(_:)``
lets a free item through and refuses a premium one unless ``Subscriptions.hasPremium``
(the screen then shows the paywall). Functional mode reads the feed from the local stub
(``IOSFORGE_CONTENT_FEED_URL``) and journals ``content.loaded`` / ``content.opened`` /
``content.locked``. As with ``remote_api``, a feed URL seen in the original's traffic is
never used.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp.swiftui_functional import (
    FunctionalCheck,
    MockContext,
    Step,
    register_mock,
    swift_literal,
)
from iosforge.mvp.swiftui_templates import DO_NOT_EDIT

KEY = "content_feed"
UNCONFIGURED_URL = "https://feed-not-configured.invalid"
FREE_TITLE = "IOSFORGE-FEED-FREE"
PRO_TITLE = "IOSFORGE-FEED-PRO"
ITEM_ID = "iosforge.content.item"
STUB_ITEMS = [
    {
        "id": "free-1",
        "title": FREE_TITLE,
        "subtitle": "Free sample",
        "body": "Free.",
        "premium": False,
    },
    {
        "id": "pro-1",
        "title": PRO_TITLE,
        "subtitle": "Premium sample",
        "body": "Pro.",
        "premium": True,
    },
]


def _feed_url(value: object) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"https://[^\s<>?#@]+(\?[^\s<>#@]*)?", text):
        return UNCONFIGURED_URL
    return text


def seed_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Bundled items from ``content.content_to_seed`` (premium per ``free_vs_premium``)."""
    content = spec.get("content") or {}
    premium_features = {
        str(row.get("feature", "")).lower()
        for row in (spec.get("monetization") or {}).get("free_vs_premium") or []
        if isinstance(row, dict) and "prem" in str(row.get("tier", "")).lower()
    }
    items: list[dict[str, Any]] = []
    for index, row in enumerate(content.get("content_to_seed") or []):
        if not isinstance(row, dict):
            continue
        title = str(row.get("item") or row.get("example") or f"Item {index + 1}")
        items.append(
            {
                "id": f"seed-{index + 1}",
                "title": title,
                "subtitle": str(row.get("format") or ""),
                "body": str(row.get("example") or ""),
                "premium": any(
                    feature and feature in title.lower() for feature in premium_features
                ),
            }
        )
    return items[:24]


def _item_literal(item: dict[str, Any]) -> str:
    return (
        f"        ContentItem(id: {swift_literal(str(item['id']))}, "
        f"title: {swift_literal(str(item['title']))}, "
        f"subtitle: {swift_literal(str(item['subtitle']))}, "
        f"body: {swift_literal(str(item['body']))}, "
        f"premium: {'true' if item['premium'] else 'false'}),"
    )


def render_feed(ctx: caps.CapabilityContext) -> str:
    """``App/Capabilities/ContentFeed.swift`` with the operator feed and the bundled seed."""
    seed = seed_items(ctx.spec)
    rows = "\n".join(_item_literal(item) for item in seed)
    bundled = f"[\n{rows}\n    ]" if rows else "[]"
    feed = _feed_url(ctx.integrations.content_feed_url)
    return f"""import Foundation

/// One piece of content in the library.
struct ContentItem: Identifiable, Codable, Hashable {{
    let id: String
    let title: String
    let subtitle: String
    let body: String
    let premium: Bool
}}

/// The content library: the operator's feed (else the bundled seed), gated by
/// `Subscriptions.hasPremium`. Screens call only these functions. {DO_NOT_EDIT}
@MainActor
enum ContentFeed {{
    static let feedURL = {swift_literal(feed)}
    static let bundled: [ContentItem] = {bundled}

    private struct Envelope: Decodable {{
        let items: [ContentItem]
    }}

    /// The library items (the feed when configured and reachable, else the bundled seed).
    static func load() async -> [ContentItem] {{
        guard !Headless.isActive else {{ return bundled }}
        let source = Functional.value("CONTENT_FEED_URL") ?? feedURL
        if source != {swift_literal(UNCONFIGURED_URL)}, let url = URL(string: source),
           let (data, response) = try? await URLSession.shared.data(from: url),
           ((response as? HTTPURLResponse)?.statusCode ?? 0) < 300 {{
            let decoder = JSONDecoder()
            let items = (try? decoder.decode([ContentItem].self, from: data))
                ?? (try? decoder.decode(Envelope.self, from: data).items)
            if let items, !items.isEmpty {{
                Functional.record("content.loaded", ["count": String(items.count), "source": "feed"])
                return items
            }}
        }}
        Functional.record("content.loaded", ["count": String(bundled.count), "source": "bundled"])
        return bundled
    }}

    /// True when `item` may be shown now; a premium item without a subscription is refused.
    static func open(_ item: ContentItem) -> Bool {{
        if item.premium && !Subscriptions.hasPremium {{
            Functional.record("content.locked", ["id": item.id])
            return false
        }}
        Functional.record("content.opened", ["id": item.id])
        return true
    }}
}}
"""


SCREEN_RULE = (
    "Content only through the scaffold module (`App/Capabilities/ContentFeed.swift`): load the\n"
    "  library with `let items = await ContentFeed.load()` in `.task`, show every item's `title`\n"
    "  (and a lock on `premium` ones), make each item a `Button` labelled with its title and\n"
    f'  marked `.accessibilityIdentifier("{ITEM_ID}")`, and on tap call\n'
    "  `ContentFeed.open(item)`: true → show the item (detail / player), false → `router.show` the\n"
    "  paywall. Never hard-code the library in screens and never bypass `open`; headless mode\n"
    "  shows the bundled items."
)


def _check(ctx: caps.CapabilityContext) -> FunctionalCheck | None:
    screen = caps.capability_screen(ctx)
    if not screen:
        return None
    return FunctionalCheck(
        name=str(ctx.capability.get("name") or KEY),
        screen_id=screen,
        steps=(
            Step("wait_text", FREE_TITLE, timeout=20),
            Step("tap", re.escape(PRO_TITLE), timeout=10),
            Step("pause", timeout=2),
        ),
        expect_events=("content.loaded", "content.locked"),
        mock=KEY,
    )


class _FeedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(STUB_ITEMS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return None


@contextmanager
def feed_server() -> Iterator[str]:
    """A local feed on 127.0.0.1 serving :data:`STUB_ITEMS`."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/feed.json"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _mock(check: FunctionalCheck, context: MockContext) -> Iterator[dict[str, str]]:
    with feed_server() as url:
        yield {"IOSFORGE_CONTENT_FEED_URL": url}


DESCRIPTOR = caps.CapabilityDescriptor(
    key=KEY,
    directory=caps.CAPABILITIES_DIR,
    render=lambda ctx: {f"{caps.CAPABILITIES_DIR}/ContentFeed.swift": render_feed(ctx)},
    screen_api_rule=SCREEN_RULE,
    info_properties=lambda ctx: [
        "        NSAppTransportSecurity:",
        "          NSAllowsLocalNetworking: true",
    ],
    functional_check=_check,
)


def register() -> None:
    """Register the content module and its feed stub."""
    caps.register(DESCRIPTOR)
    register_mock(KEY, _mock)
