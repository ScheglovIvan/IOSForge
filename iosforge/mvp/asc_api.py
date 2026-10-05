"""A thin App Store Connect API client, keyed by a job's own signing credential.

Each cloned app ships under its own Apple account, so every call is authenticated with
that job's uploaded ``.p8`` key (see :mod:`iosforge.mvp.asc_credentials`). The token is a
short-lived ES256 JWT minted per client, exactly as Apple's ``app-store-connect`` CLI does.

The client is deliberately small: it exists to read an app's editable version and to write
the store listing (description, keywords, subtitle, support/marketing/privacy URLs) so the
operator never has to retype metadata into the web UI. It is NOT a general SDK.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from iosforge.common.logging import get_logger
from iosforge.mvp.asc_credentials import AscCredentials

log = get_logger("mvp.asc_api")

_BASE = "https://api.appstoreconnect.apple.com/v1"
_AUDIENCE = "appstoreconnect-v1"
_TOKEN_TTL = 600

# The top-level App Store categories (iOS), for the admin's category pickers.
CATEGORY_IDS: tuple[str, ...] = (
    "UTILITIES",
    "NAVIGATION",
    "MUSIC",
    "PRODUCTIVITY",
    "PHOTO_AND_VIDEO",
    "ENTERTAINMENT",
    "LIFESTYLE",
    "HEALTH_AND_FITNESS",
    "TRAVEL",
    "REFERENCE",
    "EDUCATION",
    "BUSINESS",
    "FINANCE",
    "SOCIAL_NETWORKING",
    "SHOPPING",
    "WEATHER",
    "SPORTS",
    "NEWS",
    "FOOD_AND_DRINK",
    "GRAPHICS_AND_DESIGN",
    "DEVELOPER_TOOLS",
    "MEDICAL",
    "BOOKS",
)

# The age-rating questionnaire answered for an app with no objectionable content
# (everything "None"/off), which resolves to a 4+ rating. The exact attribute set
# Apple's 2025 questionnaire accepts, discovered by probing the API.
AGE_RATING_NO_OBJECTIONABLE: dict[str, Any] = {
    **{
        field: "NONE"
        for field in (
            "alcoholTobaccoOrDrugUseOrReferences",
            "contests",
            "gamblingSimulated",
            "gunsOrOtherWeapons",
            "horrorOrFearThemes",
            "matureOrSuggestiveThemes",
            "medicalOrTreatmentInformation",
            "profanityOrCrudeHumor",
            "sexualContentGraphicAndNudity",
            "sexualContentOrNudity",
            "violenceCartoonOrFantasy",
            "violenceRealistic",
            "violenceRealisticProlongedGraphicOrSadistic",
        )
    },
    **{
        field: False
        for field in (
            "advertising",
            "ageAssurance",
            "gambling",
            "healthOrWellnessTopics",
            "lootBox",
            "messagingAndChat",
            "parentalControls",
            "socialMedia",
            "unrestrictedWebAccess",
            "userGeneratedContent",
        )
    },
    "kidsAgeBand": None,
}


class AscApiError(RuntimeError):
    """An App Store Connect API request failed (message is the user-facing reason)."""


def mint_token(creds: AscCredentials, *, now: int | None = None) -> str:
    """Sign a short-lived ES256 JWT for ``creds``."""
    issued = int(time.time()) if now is None else now
    return jwt.encode(
        {"iss": creds.issuer_id, "iat": issued, "exp": issued + _TOKEN_TTL, "aud": _AUDIENCE},
        creds.private_key_p8,
        algorithm="ES256",
        headers={"kid": creds.key_id, "typ": "JWT"},
    )


@dataclass
class EditableListing:
    """The editable version + app-info localisations we write a listing into."""

    app_id: str
    version_id: str
    version_state: str
    version_localization_id: str | None
    info_localization_id: str | None
    locale: str


class AscClient:
    """Authenticated App Store Connect calls for one app account."""

    def __init__(self, creds: AscCredentials, *, timeout: float = 60.0) -> None:
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {mint_token(creds)}"}, timeout=timeout
        )

    def __enter__(self) -> AscClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{_BASE}{path}"
        resp = self._client.request(method, url, **kwargs)
        if resp.status_code >= 400:
            raise AscApiError(f"{method} {path} → {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            return {}
        return dict(resp.json())

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._request("GET", path, params=params or None)

    def patch(self, path: str, *, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", path, json=body)

    def find_app(self, bundle_id: str = "") -> dict[str, Any]:
        """The account's app, matched by ``bundle_id`` when given, else the first one."""
        data = self.get("/apps", **{"limit": 50}).get("data", [])
        if not data:
            raise AscApiError("this App Store Connect account has no apps")
        if bundle_id:
            for app in data:
                if app.get("attributes", {}).get("bundleId") == bundle_id:
                    return dict(app)
        return dict(data[0])

    def editable_listing(self, *, bundle_id: str = "", locale: str = "en-US") -> EditableListing:
        """Locate the version + app-info localisation ids a listing edit targets.

        Prefers the version whose state is still editable (anything other than a
        live/approved state); falls back to the newest version so the ids can still be
        read for a dry run.
        """
        app = self.find_app(bundle_id)
        app_id = str(app["id"])

        versions = self.get(f"/apps/{app_id}/appStoreVersions", **{"limit": 10}).get("data", [])
        if not versions:
            raise AscApiError("the app has no versions to edit")
        editable = _pick_editable(versions)
        version_id = str(editable["id"])
        state = str(
            editable["attributes"].get("appStoreState")
            or editable["attributes"].get("appVersionState")
            or ""
        )

        ver_loc = _match_locale(
            self.get(f"/appStoreVersions/{version_id}/appStoreVersionLocalizations").get(
                "data", []
            ),
            locale,
        )
        info_loc = self._info_localization(app_id, locale)

        return EditableListing(
            app_id=app_id,
            version_id=version_id,
            version_state=state,
            version_localization_id=str(ver_loc["id"]) if ver_loc else None,
            info_localization_id=str(info_loc["id"]) if info_loc else None,
            locale=locale,
        )

    def _info_localization(self, app_id: str, locale: str) -> dict[str, Any] | None:
        infos = self.get(f"/apps/{app_id}/appInfos", **{"limit": 5}).get("data", [])
        info = _pick_editable(infos) if infos else None
        if info is None:
            return None
        locs = self.get(f"/appInfos/{info['id']}/appInfoLocalizations").get("data", [])
        return _match_locale(locs, locale)

    def write_version_localization(
        self, localization_id: str, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        """Write description/keywords/promotional text/support+marketing URLs."""
        return self.patch(
            f"/appStoreVersionLocalizations/{localization_id}",
            body={
                "data": {
                    "type": "appStoreVersionLocalizations",
                    "id": localization_id,
                    "attributes": attributes,
                }
            },
        )

    def write_info_localization(
        self, localization_id: str, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        """Write subtitle / privacy policy URL (and name where still editable)."""
        return self.patch(
            f"/appInfoLocalizations/{localization_id}",
            body={
                "data": {
                    "type": "appInfoLocalizations",
                    "id": localization_id,
                    "attributes": attributes,
                }
            },
        )

    def set_categories(self, info_id: str, *, primary: str, secondary: str = "") -> dict[str, Any]:
        """Set the app's primary (and optional secondary) App Store category."""
        relationships: dict[str, Any] = {
            "primaryCategory": {"data": {"type": "appCategories", "id": primary}}
        }
        if secondary:
            relationships["secondaryCategory"] = {
                "data": {"type": "appCategories", "id": secondary}
            }
        return self.patch(
            f"/appInfos/{info_id}",
            body={"data": {"type": "appInfos", "id": info_id, "relationships": relationships}},
        )

    def set_content_rights(self, app_id: str, declaration: str) -> dict[str, Any]:
        """Set the app's content-rights declaration (third-party content or not)."""
        return self.patch(
            f"/apps/{app_id}",
            body={
                "data": {
                    "type": "apps",
                    "id": app_id,
                    "attributes": {"contentRightsDeclaration": declaration},
                }
            },
        )

    def set_age_rating(self, declaration_id: str, attributes: dict[str, Any]) -> dict[str, Any]:
        """Write the age-rating questionnaire answers."""
        return self.patch(
            f"/ageRatingDeclarations/{declaration_id}",
            body={
                "data": {
                    "type": "ageRatingDeclarations",
                    "id": declaration_id,
                    "attributes": attributes,
                }
            },
        )

    def attach_build(self, version_id: str, build_id: str) -> dict[str, Any]:
        """Attach a processed build to a version."""
        return self.patch(
            f"/appStoreVersions/{version_id}/relationships/build",
            body={"data": {"type": "builds", "id": build_id}},
        )

    def set_copyright(self, version_id: str, copyright_line: str) -> dict[str, Any]:
        """Set the version's copyright line (``<year> <rights holder>``).

        App Store Connect refuses to start a review without it.
        """
        return self.patch(
            f"/appStoreVersions/{version_id}",
            body={
                "data": {
                    "type": "appStoreVersions",
                    "id": version_id,
                    "attributes": {"copyright": copyright_line},
                }
            },
        )

    def set_review_contact(self, version_id: str, attributes: dict[str, Any]) -> dict[str, Any]:
        """Create or update the App Review contact for a version.

        Apple requires all of first name, last name, phone (``+<country> number``) and email.
        Creates the review detail on first write, updates it after.
        """
        existing = self.get(f"/appStoreVersions/{version_id}/appStoreReviewDetail").get("data")
        if existing:
            review_id = str(existing["id"])
            return self.patch(
                f"/appStoreReviewDetails/{review_id}",
                body={
                    "data": {
                        "type": "appStoreReviewDetails",
                        "id": review_id,
                        "attributes": attributes,
                    }
                },
            )
        return self._request(
            "POST",
            "/appStoreReviewDetails",
            json={
                "data": {
                    "type": "appStoreReviewDetails",
                    "attributes": attributes,
                    "relationships": {
                        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}
                    },
                }
            },
        )

    def account_holder(self) -> dict[str, str]:
        """The account holder's name and email, for pre-filling the review contact.

        The App Store Connect API does not expose a phone number, so only name and email
        are returned; the operator supplies the phone.
        """
        for user in self.get("/users", **{"limit": 20}).get("data", []):
            attrs = user.get("attributes", {})
            if "ACCOUNT_HOLDER" in (attrs.get("roles") or []):
                return {
                    "first_name": str(attrs.get("firstName") or ""),
                    "last_name": str(attrs.get("lastName") or ""),
                    "email": str(attrs.get("username") or ""),
                }
        return {"first_name": "", "last_name": "", "email": ""}


_UNEDITABLE = {
    "READY_FOR_SALE",
    "PENDING_APPLE_RELEASE",
    "PENDING_DEVELOPER_RELEASE",
    "PROCESSING_FOR_APP_STORE",
    "IN_REVIEW",
    "REPLACED_WITH_NEW_VERSION",
}


def _pick_editable(items: list[dict[str, Any]]) -> dict[str, Any]:
    """First item whose state is editable, else the newest item."""
    for item in items:
        state = str(
            item.get("attributes", {}).get("appStoreState")
            or item.get("attributes", {}).get("state")
            or item.get("attributes", {}).get("appVersionState")
            or ""
        )
        if state and state not in _UNEDITABLE:
            return item
    return items[0]


def _match_locale(localizations: list[dict[str, Any]], locale: str) -> dict[str, Any] | None:
    """The localisation for ``locale``; the first one when that locale is absent."""
    for loc in localizations:
        if loc.get("attributes", {}).get("locale") == locale:
            return loc
    return localizations[0] if localizations else None
