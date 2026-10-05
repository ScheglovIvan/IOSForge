"""Legal pages: the policy must describe what the app really does."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from iosforge.mvp import legal_pages as lp

_CARPLAY_PUBSPEC = """name: carplay_sounds_clone
dependencies:
  flutter:
    sdk: flutter
  google_fonts: ^6.2.1
  apphud: ^3.2.0
  tenjin_plugin: ^1.2.0
  geolocator: ^12.0.0
dev_dependencies:
  flutter_test:
    sdk: flutter
"""

_CARPLAY_PERMS = {
    "NSLocationWhenInUseUsageDescription": "Show your position on the map.",
    "NSAppleMusicUsageDescription": "Show the track playing.",
    "NSUserTrackingUsageDescription": "Measure which ads bring people here.",
    "ITSAppUsesNonExemptEncryption": False,
}


def _facts(**over: object) -> lp.AppLegalFacts:
    kwargs: dict = {
        "app_name": "Dashboard for CarPlay",
        "bundle_id": "com.techq.dashboardforcarplay",
        "contact_email": "support@example.com",
        "pubspec": _CARPLAY_PUBSPEC,
        "permissions": _CARPLAY_PERMS,
    }
    kwargs.update(over)
    return lp.facts_from_build(**kwargs)  # type: ignore[arg-type]


def test_every_declared_permission_becomes_a_disclosure() -> None:
    # a policy that omits a permission the app requests is worse than no policy
    facts = _facts()
    titles = {p.title for p in facts.practices}
    assert "Location" in titles
    assert "Media library" in titles
    assert "Tracking permission" in titles


def test_a_permission_the_app_does_not_request_is_not_claimed() -> None:
    # the microphone belongs to the other app; claiming it here would be a lie
    titles = {p.title for p in _facts().practices}
    assert "Microphone" not in titles
    assert "Camera" not in titles


def test_sdks_in_the_pubspec_become_named_processors() -> None:
    names = {p.title for p in _facts().processors}
    assert {"Apphud", "Tenjin", "Google Fonts"} <= names


def test_an_sdk_that_is_absent_is_not_listed() -> None:
    spec = _CARPLAY_PUBSPEC.replace("  tenjin_plugin: ^1.2.0\n", "")
    assert "Tenjin" not in {p.title for p in _facts(pubspec=spec).processors}


def test_dev_dependencies_are_not_read_as_processors() -> None:
    spec = _CARPLAY_PUBSPEC + "  apphud: ^3.2.0\n"
    facts = _facts(pubspec=spec)
    assert [p.title for p in facts.processors].count("Apphud") == 1


def test_microphone_app_discloses_that_nothing_is_recorded() -> None:
    facts = _facts(permissions={"NSMicrophoneUsageDescription": "Measure sound."})
    mic = next(p for p in facts.practices if p.title == "Microphone")
    assert "nothing is recorded" in mic.detail.lower()


def test_remote_endpoints_are_disclosed_when_passed() -> None:
    # the VIN lookup sends the number to a US government API — a real transfer
    extra = lp.DataPractice("Vehicle lookup", "The VIN is sent to the NHTSA vPIC service.")
    facts = _facts(remote_endpoints=[extra])
    assert extra in facts.practices


def test_rendered_privacy_page_is_html_with_the_app_and_the_date() -> None:
    page = lp.render_privacy(_facts(), updated=date(2026, 7, 24))
    assert page.startswith("<!doctype html>")
    assert "Dashboard for CarPlay" in page
    assert "24 July 2026" in page
    assert "com.techq.dashboardforcarplay" in page
    assert page.count("<html") == 1 and page.rstrip().endswith("</html>")


def test_page_links_the_apple_eula_for_terms() -> None:
    page = lp.render_privacy(_facts(), updated=date(2026, 7, 24))
    assert lp.APPLE_EULA_URL in page


def test_tracking_note_appears_only_when_tracking_is_requested() -> None:
    with_att = lp.render_privacy(_facts(), updated=date(2026, 7, 24))
    assert "Privacy &amp; Security" in with_att
    without = lp.render_privacy(
        _facts(permissions={"NSMicrophoneUsageDescription": "x"}), updated=date(2026, 7, 24)
    )
    assert "Privacy &amp; Security" not in without


def test_app_name_is_escaped_not_injected() -> None:
    page = lp.render_privacy(
        _facts(app_name='Bad<script>alert("x")</script>'), updated=date(2026, 7, 24)
    )
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_contact_email_is_reachable_from_the_page() -> None:
    page = lp.render_privacy(_facts(contact_email="legal@example.com"), updated=date.today())
    assert "mailto:legal@example.com" in page


def test_build_pages_serves_policy_and_support_at_clean_urls() -> None:
    files = lp.build_pages(_facts())
    assert set(files) == {
        "index.html",
        "privacy.html",
        "privacy/index.html",
        "support.html",
        "support/index.html",
    }
    # /privacy and /privacy.html must be the same document
    assert files["privacy.html"] == files["privacy/index.html"]
    assert files["support.html"] == files["support/index.html"]


def test_support_page_carries_a_reachable_contact() -> None:
    page = lp.render_support(_facts(contact_email="help@example.com"))
    assert "mailto:help@example.com" in page
    assert "Support" in page


def test_repo_full_name_reads_the_owner_and_repo() -> None:
    assert lp.repo_full_name("https://github.com/ScheglovIvan/sounds-for-carplay") == (
        "ScheglovIvan/sounds-for-carplay"
    )
    assert lp.repo_full_name("https://github.com/a/b.git") == "a/b"
    assert lp.repo_full_name("https://github.com/a/b/") == "a/b"


def test_repo_full_name_rejects_nonsense() -> None:
    with pytest.raises(lp.LegalPagesError):
        lp.repo_full_name("nonsense")


def test_permissions_file_missing_is_not_fatal(tmp_path: Path) -> None:
    assert lp.load_permissions(tmp_path / "absent.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert lp.load_permissions(broken) == {}


def test_permissions_file_is_read(tmp_path: Path) -> None:
    path = tmp_path / "ios_permissions.json"
    path.write_text(json.dumps(_CARPLAY_PERMS))
    assert lp.load_permissions(path)["NSAppleMusicUsageDescription"]


def test_publish_refuses_without_a_token() -> None:
    from iosforge.common.config import Settings

    with pytest.raises(lp.LegalPagesError):
        lp.publish_pages(
            settings=Settings(), token="", repo_full_name="a/b", files={"index.html": "x"}
        )


def test_pages_branch_is_not_the_code_branch() -> None:
    # the app push force-updates the default branch; the legal pages must survive it
    assert lp.PAGES_BRANCH == "gh-pages"
