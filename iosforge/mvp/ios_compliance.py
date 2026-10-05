"""Per-job iOS export-compliance flag staged into the generated app.

Apps that only use the OS's standard HTTPS/TLS (no bundled cryptography) are exempt
from US export compliance. Declaring ``ITSAppUsesNonExemptEncryption = false`` in
Info.plist makes App Store Connect skip the "Missing Compliance" prompt for every
build. This is opt-in **per job** (``export_compliance_exempt`` in the job metadata) —
a new app may genuinely implement encryption and must answer for itself, so it is never
a template-wide default.

The value is merged into ``ios_permissions.json`` (the CodeMagic build already applies
that file to Info.plist; the injector understands booleans).
"""

from __future__ import annotations

import json
from pathlib import Path

from iosforge.common.logging import get_logger

log = get_logger("mvp.ios_compliance")

_KEY = "ITSAppUsesNonExemptEncryption"


def apply(perms: dict[str, object], exempt: bool | None) -> dict[str, object]:
    """Return ``perms`` with the compliance key set to False, or unchanged when not exempt."""
    if exempt:
        perms[_KEY] = False
    return perms


def stage(flutter_app: Path, exempt: bool | None) -> bool:
    """Merge ``ITSAppUsesNonExemptEncryption=false`` into the app's ios_permissions.json.

    No-op (returns False) unless ``exempt`` is truthy. Lands inside ``flutter_app/`` so it
    survives the GitHub push and is applied by the CodeMagic build.
    """
    if not exempt:
        return False
    perms_file = flutter_app / "ios_permissions.json"
    perms: dict[str, object] = {}
    if perms_file.is_file():
        try:
            loaded = json.loads(perms_file.read_text())
            perms = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            perms = {}
    apply(perms, True)
    perms_file.write_text(json.dumps(perms, indent=2, ensure_ascii=False))
    log.info("ios_compliance.staged", key=_KEY)
    return True
