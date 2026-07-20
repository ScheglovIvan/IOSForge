"""Per-job build profile and identity resolution.

Every job carries a build profile in ``Job.source_app_metadata`` (JSONB, no
migration):

- ``build_profile``: ``"test"`` (default) or ``"real"``.
- ``override_bundle_id``: exact bundle id for the ``real`` build (empty → derived).
- ``override_app_name``: display name (empty → the analysed ``app_spec.app_name``).

``resolve_identity`` folds these + settings into a single :class:`BuildIdentity`
that the pipeline threads into Apphud config and the CodeMagic build so the bundle
id / display name / store mode stay consistent everywhere.

test  → Apphud sandbox mode (StoreKit sandbox purchases, unsigned build).
real  → real bundle id/name written into the binary, Apphud production mode; the
        build stays UNSIGNED until Apple signing credentials are configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iosforge.common.config import Settings

_TEST = "test"
_REAL = "real"


def _bundle_slug(value: str) -> str:
    """Alphanumeric-only slug for a bundle id segment (no separators)."""
    return "".join(c for c in value.lower() if c.isalnum()) or "app"


@dataclass(frozen=True)
class BuildIdentity:
    profile: str
    app_name: str
    bundle_id: str
    sandbox: bool


def resolve_identity(
    metadata: dict[str, Any] | None, spec: dict[str, Any] | None, settings: Settings
) -> BuildIdentity:
    """Resolve the effective build profile + bundle id + display name for a job."""
    meta = metadata or {}
    spec = spec or {}
    profile = str(meta.get("build_profile") or _TEST).lower()
    if profile not in (_TEST, _REAL):
        profile = _TEST

    app_name = str(meta.get("override_app_name") or spec.get("app_name") or "App").strip() or "App"

    override_bundle = str(meta.get("override_bundle_id") or "").strip()
    bundle_id = override_bundle or f"{settings.codemagic_bundle_prefix}.{_bundle_slug(app_name)}"

    sandbox = profile == _TEST
    return BuildIdentity(profile=profile, app_name=app_name, bundle_id=bundle_id, sandbox=sandbox)
