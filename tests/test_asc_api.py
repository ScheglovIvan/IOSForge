"""App Store Connect API client: token signing and edit-target selection."""

from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from iosforge.mvp import asc_api
from iosforge.mvp.asc_credentials import AscCredentials


def _p8() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _creds() -> AscCredentials:
    return AscCredentials(
        issuer_id="11111111-2222-3333-4444-555555555555",
        key_id="ABCD1234EF",
        key_name="test",
        private_key_p8=_p8(),
        certificate_private_key="",
    )


def test_token_is_a_valid_es256_jwt_with_the_right_claims() -> None:
    creds = _creds()
    token = asc_api.mint_token(creds, now=1_000_000)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == creds.key_id
    payload = jwt.decode(token, options={"verify_signature": False}, audience="appstoreconnect-v1")
    assert payload["iss"] == creds.issuer_id
    assert payload["aud"] == "appstoreconnect-v1"
    assert payload["exp"] > payload["iat"]


def test_pick_editable_prefers_an_editable_state() -> None:
    items = [
        {"id": "live", "attributes": {"appStoreState": "READY_FOR_SALE"}},
        {"id": "draft", "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"}},
    ]
    assert asc_api._pick_editable(items)["id"] == "draft"


def test_pick_editable_falls_back_to_the_first_when_none_editable() -> None:
    items = [
        {"id": "live", "attributes": {"appStoreState": "READY_FOR_SALE"}},
        {"id": "review", "attributes": {"appStoreState": "IN_REVIEW"}},
    ]
    assert asc_api._pick_editable(items)["id"] == "live"


def test_match_locale_prefers_the_exact_locale() -> None:
    locs = [
        {"id": "fr", "attributes": {"locale": "fr-FR"}},
        {"id": "en", "attributes": {"locale": "en-US"}},
    ]
    assert asc_api._match_locale(locs, "en-US")["id"] == "en"


def test_match_locale_falls_back_to_first_when_absent() -> None:
    locs = [{"id": "fr", "attributes": {"locale": "fr-FR"}}]
    assert asc_api._match_locale(locs, "en-US")["id"] == "fr"
    assert asc_api._match_locale([], "en-US") is None


def test_find_app_matches_bundle_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = asc_api.AscClient.__new__(asc_api.AscClient)
    payload = {
        "data": [
            {"id": "1", "attributes": {"bundleId": "com.a"}},
            {"id": "2", "attributes": {"bundleId": "com.b"}},
        ]
    }
    monkeypatch.setattr(client, "get", lambda path, **kw: payload)
    assert client.find_app("com.b")["id"] == "2"
    assert client.find_app()["id"] == "1"


def test_find_app_raises_when_account_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = asc_api.AscClient.__new__(asc_api.AscClient)
    monkeypatch.setattr(client, "get", lambda path, **kw: {"data": []})
    with pytest.raises(asc_api.AscApiError):
        client.find_app("com.b")


def test_age_rating_payload_is_all_clean() -> None:
    from iosforge.mvp.asc_api import AGE_RATING_NO_OBJECTIONABLE as ar

    # every content descriptor says NONE, every behaviour says off, and it is not for kids
    assert ar["violenceRealistic"] == "NONE"
    assert ar["sexualContentOrNudity"] == "NONE"
    assert ar["gambling"] is False
    assert ar["userGeneratedContent"] is False
    assert ar["unrestrictedWebAccess"] is False
    assert ar["kidsAgeBand"] is None
    assert "seventeenPlus" not in ar  # not a real attribute — the API rejects it


def test_category_ids_cover_the_common_choices() -> None:
    from iosforge.mvp.asc_api import CATEGORY_IDS

    assert "UTILITIES" in CATEGORY_IDS
    assert "NAVIGATION" in CATEGORY_IDS
    assert "MUSIC" in CATEGORY_IDS
    assert len(set(CATEGORY_IDS)) == len(CATEGORY_IDS)  # no duplicates


def test_set_categories_builds_the_relationship_body(monkeypatch) -> None:
    client = asc_api.AscClient.__new__(asc_api.AscClient)
    captured = {}

    def fake_patch(path, *, body):
        captured["path"] = path
        captured["body"] = body
        return {}

    monkeypatch.setattr(client, "patch", fake_patch)
    client.set_categories("INFO1", primary="UTILITIES", secondary="MUSIC")
    rel = captured["body"]["data"]["relationships"]
    assert rel["primaryCategory"]["data"]["id"] == "UTILITIES"
    assert rel["secondaryCategory"]["data"]["id"] == "MUSIC"


def test_set_categories_omits_an_empty_secondary(monkeypatch) -> None:
    client = asc_api.AscClient.__new__(asc_api.AscClient)
    captured = {}
    monkeypatch.setattr(client, "patch", lambda path, *, body: captured.update(body=body) or {})
    client.set_categories("INFO1", primary="NAVIGATION")
    assert "secondaryCategory" not in captured["body"]["data"]["relationships"]


def test_set_review_contact_creates_when_absent(monkeypatch) -> None:
    client = asc_api.AscClient.__new__(asc_api.AscClient)
    calls = {}

    def fake_request(method, path, **kw):
        calls["method"] = method
        calls["path"] = path
        calls["json"] = kw.get("json")
        return {}

    monkeypatch.setattr(client, "get", lambda path, **kw: {"data": None})
    monkeypatch.setattr(client, "_request", fake_request)
    client.set_review_contact("VER1", {"contactFirstName": "Ivan"})
    assert calls["method"] == "POST"
    assert calls["path"] == "/appStoreReviewDetails"
    rel = calls["json"]["data"]["relationships"]["appStoreVersion"]["data"]
    assert rel["id"] == "VER1"


def test_set_review_contact_updates_when_present(monkeypatch) -> None:
    client = asc_api.AscClient.__new__(asc_api.AscClient)
    captured = {}
    monkeypatch.setattr(client, "get", lambda path, **kw: {"data": {"id": "REV9"}})
    monkeypatch.setattr(
        client, "patch", lambda path, *, body: captured.update(path=path, body=body) or {}
    )
    client.set_review_contact("VER1", {"contactPhone": "+100"})
    assert captured["path"] == "/appStoreReviewDetails/REV9"
    assert captured["body"]["data"]["id"] == "REV9"


def test_account_holder_returns_the_holder_not_a_developer(monkeypatch) -> None:
    client = asc_api.AscClient.__new__(asc_api.AscClient)
    monkeypatch.setattr(
        client,
        "get",
        lambda path, **kw: {
            "data": [
                {
                    "attributes": {
                        "firstName": "Dev",
                        "lastName": "Eloper",
                        "username": "dev@x.com",
                        "roles": ["DEVELOPER"],
                    }
                },
                {
                    "attributes": {
                        "firstName": "Mart",
                        "lastName": "Nael",
                        "username": "mart@x.com",
                        "roles": ["ACCOUNT_HOLDER", "ADMIN"],
                    }
                },
            ]
        },
    )
    holder = client.account_holder()
    assert holder == {"first_name": "Mart", "last_name": "Nael", "email": "mart@x.com"}


def test_set_copyright_patches_the_version(monkeypatch) -> None:
    client = asc_api.AscClient.__new__(asc_api.AscClient)
    captured = {}
    monkeypatch.setattr(
        client, "patch", lambda path, *, body: captured.update(path=path, body=body) or {}
    )
    client.set_copyright("VER1", "2026 Example OU")
    assert captured["path"] == "/appStoreVersions/VER1"
    assert captured["body"]["data"]["attributes"]["copyright"] == "2026 Example OU"
