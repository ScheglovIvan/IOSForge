"""Generate an App Store listing (description, keywords, subtitle) from an app's spec.

The store listing is a managed artifact like the icon and slides: the pipeline produces a
draft from what the app actually is — its one-liner, its screens, its monetisation — and the
operator reviews and pushes it. It is NOT copied from the original app's page (that is a
review rejection under Guideline 4.3); the wording here is generated fresh and only the
factual feature set and the search terms carry over.

Every field is clamped to App Store Connect's limits so a push can never be rejected for
length: name 30, subtitle 30, keywords 100, promotional text 170, description 4000.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

APPLE_EULA_URL = "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/"

NAME_MAX = 30
SUBTITLE_MAX = 30
KEYWORDS_MAX = 100
PROMO_MAX = 170
DESCRIPTION_MAX = 4000

_SUBSCRIPTION_DISCLOSURE = (
    "Subscription details\n"
    "Payment is charged to your Apple ID at confirmation of purchase. A subscription "
    "renews automatically unless auto-renew is turned off at least 24 hours before the "
    "current period ends; your account is charged for renewal within 24 hours before the "
    "period ends. Manage or cancel anytime in your App Store account settings. Any unused "
    "portion of a free trial is forfeited when you buy a subscription."
)


@dataclass
class ListingCopy:
    """A complete, length-checked store listing for one app."""

    app_name: str
    subtitle: str
    keywords: str
    promotional_text: str
    description: str
    support_url: str
    marketing_url: str
    privacy_policy_url: str
    terms_url: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def field_lengths(self) -> dict[str, tuple[int, int]]:
        """Each length-limited field as ``(used, max)`` for the admin to display."""
        return {
            "app_name": (len(self.app_name), NAME_MAX),
            "subtitle": (len(self.subtitle), SUBTITLE_MAX),
            "keywords": (len(self.keywords), KEYWORDS_MAX),
            "promotional_text": (len(self.promotional_text), PROMO_MAX),
            "description": (len(self.description), DESCRIPTION_MAX),
        }


_STOPWORDS = {
    "the",
    "and",
    "for",
    "your",
    "you",
    "with",
    "that",
    "this",
    "from",
    "into",
    "onto",
    "when",
    "then",
    "them",
    "they",
    "app",
    "apps",
    "our",
    "out",
    "get",
    "not",
    "are",
    "can",
    "all",
    "any",
    "each",
    "every",
    "just",
    "like",
    "more",
    "most",
    "than",
    "what",
    "who",
    "why",
    "how",
    "its",
    "his",
    "her",
    "one",
    "two",
    "let",
    "lets",
    "set",
    "add",
    "give",
    "gives",
    "plays",
    "play",
    "make",
    "makes",
    "keep",
    "keeps",
    "use",
    "uses",
    "run",
    "runs",
    "put",
    "puts",
    "turn",
    "turns",
    "help",
    "helps",
    "want",
    "need",
    "feel",
    "feels",
    "does",
    "done",
    "have",
    "has",
    "was",
    "will",
    "over",
    "before",
    "after",
    "while",
}


def _clamp(text: str, limit: int) -> tuple[str, bool]:
    """Trim ``text`` to ``limit`` on a word boundary; flag when it had to be cut."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.-"), True


def _screen_names(spec: dict[str, Any]) -> list[str]:
    return [
        str(s.get("name") or s.get("id") or "")
        for s in (spec.get("screens") or [])
        if isinstance(s, dict)
    ]


def _feature_terms(spec: dict[str, Any]) -> list[str]:
    """Distinct search-worthy words drawn from the spec's own text, longest first."""
    text = " ".join(
        [
            str(spec.get("one_liner") or ""),
            str(spec.get("purpose") or ""),
            str(spec.get("summary") or ""),
            " ".join(_screen_names(spec)),
            " ".join(
                str(f.get("name") if isinstance(f, dict) else f)
                for f in (spec.get("features") or [])
            ),
        ]
    ).lower()
    seen: dict[str, None] = {}
    for word in re.findall(r"[a-z][a-z]{2,}", text):
        if word in _STOPWORDS or word in seen:
            continue
        seen[word] = None
    return list(seen)


def build_keywords(spec: dict[str, Any], *, seed: list[str] | None = None) -> str:
    """A ``keyword,keyword`` field ≤100 chars, seeded terms first then spec terms.

    App Store already indexes the words in the app's name, so the caller's ``seed`` should
    be complementary search terms rather than repeats of the title.
    """
    terms: list[str] = []
    for word in [*(seed or []), *_feature_terms(spec)]:
        word = word.strip().lower()
        if word and word not in terms:
            terms.append(word)
    field_value = ""
    for word in terms:
        candidate = f"{field_value},{word}" if field_value else word
        if len(candidate) > KEYWORDS_MAX:
            break
        field_value = candidate
    return field_value


def _bullets(spec: dict[str, Any], *, limit: int = 8) -> list[str]:
    """Feature bullets from explicit spec features, falling back to screen names."""
    out: list[str] = []
    for f in spec.get("features") or []:
        name = str(f.get("name") if isinstance(f, dict) else f).strip()
        if name and name not in out:
            out.append(name)
    if not out:
        skip = re.compile(
            r"splash|onboard|paywall|loading|language|theme|units|settings|social", re.I
        )
        for name in _screen_names(spec):
            clean = name.split("—")[0].split("(")[0].strip()
            if clean and not skip.search(clean) and clean not in out:
                out.append(clean)
    return out[:limit]


def generate(
    spec: dict[str, Any],
    *,
    app_name: str,
    privacy_policy_url: str,
    terms_url: str = APPLE_EULA_URL,
    support_url: str = "",
    marketing_url: str = "",
    subtitle: str = "",
    keyword_seed: list[str] | None = None,
    lead: str = "",
    has_subscription: bool = True,
) -> ListingCopy:
    """Assemble a store listing draft from the app's spec and its published legal URLs."""
    warnings: list[str] = []
    one_liner = str(
        spec.get("one_liner") or spec.get("purpose") or spec.get("summary") or ""
    ).strip()

    name, cut = _clamp(app_name, NAME_MAX)
    if cut:
        warnings.append("app name was shortened to fit 30 characters")

    sub_source = subtitle or one_liner
    sub, cut = _clamp(sub_source, SUBTITLE_MAX)
    if cut and not subtitle:
        warnings.append("subtitle was derived from the one-liner and trimmed to 30 characters")

    keywords = build_keywords(spec, seed=keyword_seed)

    promo_source = lead or one_liner or f"{name} — do more with your device."
    promo, _ = _clamp(promo_source, PROMO_MAX)

    paragraphs: list[str] = []
    opener = lead or one_liner
    if opener:
        paragraphs.append(opener)

    bullets = _bullets(spec)
    if bullets:
        paragraphs.append("What you get\n" + "\n".join(f"- {b}" for b in bullets))

    if has_subscription:
        paragraphs.append(_SUBSCRIPTION_DISCLOSURE)

    paragraphs.append(f"Terms of Use: {terms_url}\nPrivacy Policy: {privacy_policy_url}")
    description, cut = _clamp("\n\n".join(paragraphs), DESCRIPTION_MAX)
    if cut:
        warnings.append("description was trimmed to 4000 characters")

    if not one_liner and not lead:
        warnings.append("the spec has no one-liner; description opener is generic")
    if not keywords:
        warnings.append("no keywords could be derived from the spec")

    return ListingCopy(
        app_name=name,
        subtitle=sub,
        keywords=keywords,
        promotional_text=promo,
        description=description,
        support_url=support_url,
        marketing_url=marketing_url,
        privacy_policy_url=privacy_policy_url,
        terms_url=terms_url,
        warnings=warnings,
    )


def from_dict(data: dict[str, Any]) -> ListingCopy:
    """Rebuild a :class:`ListingCopy` from its stored JSON."""
    known = {f for f in ListingCopy.__dataclass_fields__}
    return ListingCopy(**{k: v for k, v in data.items() if k in known})
