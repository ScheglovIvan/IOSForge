"""Store listing generation: fresh copy from the spec, within Apple's field limits."""

from __future__ import annotations

from iosforge.mvp import store_listing as sl

_SPEC = {
    "app_name": "Speaker Cleaner — Eject Water",
    "one_liner": "Plays scientifically tuned low-frequency tones that vibrate your phone "
    "speaker to push out trapped water and dust.",
    "monetization": {"kind": "subscription"},
    "screens": [
        {"name": "Splash / Launch"},
        {"name": "Paywall - Speaker Deep Clean Pro"},
        {"name": "Water Eject"},
        {"name": "Dust Shake"},
        {"name": "Deep Clean"},
        {"name": "dB Meter"},
        {"name": "Settings"},
        {"name": "Language Selection"},
    ],
}

_PRIVACY = "https://scheglovivan.github.io/speaker-cleaner-2/privacy"


def _gen(**over: object) -> sl.ListingCopy:
    kwargs: dict = {
        "app_name": "Speaker Cleaner — Eject Water",
        "privacy_policy_url": _PRIVACY,
    }
    kwargs.update(over)
    return sl.generate(_SPEC, **kwargs)  # type: ignore[arg-type]


def test_all_fields_are_within_app_store_limits() -> None:
    c = _gen()
    for name, (used, limit) in c.field_lengths.items():
        assert used <= limit, f"{name} is {used}, over {limit}"


def test_description_carries_the_one_liner_and_both_legal_links() -> None:
    c = _gen()
    assert "low-frequency tones" in c.description
    assert _PRIVACY in c.description
    assert sl.APPLE_EULA_URL in c.description


def test_subscription_disclosure_included_only_when_monetized() -> None:
    with_sub = _gen(has_subscription=True)
    assert "renews automatically" in with_sub.description
    without = _gen(has_subscription=False)
    assert "renews automatically" not in without.description


def test_keywords_are_a_comma_field_under_100_chars() -> None:
    c = _gen(keyword_seed=["water", "eject", "clean", "dust", "speaker"])
    assert c.keywords
    assert len(c.keywords) <= sl.KEYWORDS_MAX
    assert "water" in c.keywords.split(",")
    # no spaces wasted after commas
    assert ", " not in c.keywords


def test_keyword_seed_comes_first() -> None:
    c = _gen(keyword_seed=["fix"])
    assert c.keywords.split(",")[0] == "fix"


def test_bullets_skip_boilerplate_screens() -> None:
    c = _gen()
    assert "Water Eject" in c.description
    assert "Paywall" not in c.description
    assert "Splash" not in c.description


def test_subtitle_derived_from_one_liner_is_trimmed_and_flagged() -> None:
    c = _gen()
    assert len(c.subtitle) <= sl.SUBTITLE_MAX
    assert any("subtitle" in w for w in c.warnings)


def test_explicit_subtitle_is_used_verbatim_when_it_fits() -> None:
    c = _gen(subtitle="Eject water & clean speakers")
    assert c.subtitle == "Eject water & clean speakers"
    assert not any("subtitle" in w for w in c.warnings)


def test_a_name_over_30_chars_is_shortened_with_a_warning() -> None:
    c = _gen(app_name="An Extremely Long Application Name That Overflows")
    assert len(c.app_name) <= sl.NAME_MAX
    assert any("name" in w for w in c.warnings)


def test_missing_one_liner_warns_rather_than_inventing() -> None:
    c = sl.generate({"app_name": "X", "screens": []}, app_name="X", privacy_policy_url=_PRIVACY)
    assert any("one-liner" in w for w in c.warnings)


def test_round_trips_through_its_dict() -> None:
    c = _gen(support_url="https://example.com/support")
    again = sl.from_dict(c.to_dict())
    assert again == c


def test_from_dict_ignores_unknown_keys() -> None:
    data = _gen().to_dict()
    data["lengths"] = {"whatever": 1}  # admin adds this for display
    restored = sl.from_dict(data)
    assert restored.app_name == "Speaker Cleaner — Eject Water"


def test_field_lengths_reports_used_and_max() -> None:
    c = _gen()
    used, limit = c.field_lengths["keywords"]
    assert limit == sl.KEYWORDS_MAX
    assert used == len(c.keywords)
