"""Native iOS delivery on a Mac worker (Phase 5): archive → export → (gated) upload.

Replaces the CodeMagic workflow for SwiftUI apps. ``xcodebuild archive`` builds the
generated XcodeGen project for a generic iOS device; with a team id and the job's App
Store Connect API key it signs automatically (``-allowProvisioningUpdates``, which
fetches or creates the distribution certificate and App Store profile like CodeMagic's
``fetch-signing-files``) and ``-exportArchive`` writes a signed IPA. Without them it
produces an unsigned archive and an unsigned IPA (Payload zip). Uploading to App Store
Connect is an external action: it only runs when ``Settings.ios_delivery_upload`` is on,
otherwise the result is ``ready_for_upload`` with the exact command recorded.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from iosforge.common.config import Settings, get_settings
from iosforge.common.logging import get_logger
from iosforge.mvp import asc_credentials, simulator, xcode
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_gen import identity_from_project
from iosforge.mvp.swiftui_scaffold import target_name

log = get_logger("mvp.ios_delivery")

EXPORT_METHOD = "app-store-connect"


class UploadBlocked(RuntimeError):
    """Uploading to App Store Connect is disabled (external action, needs explicit opt-in)."""


@dataclass
class DeliveryResult:
    """What one delivery produced and how far it went."""

    status: str
    signed: bool
    version: str
    build_number: str
    archive: str = ""
    ipa: str = ""
    upload_command: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    log: str = ""


def build_number(now: float | None = None) -> str:
    """Monotonic build number from the UTC clock (``YYMMDDHHMM``), no App Store lookup."""
    return time.strftime("%y%m%d%H%M", time.gmtime(now if now is not None else time.time()))


def export_options(team_id: str, *, method: str = EXPORT_METHOD) -> bytes:
    """ExportOptions.plist for automatic App Store signing, exporting (not uploading)."""
    return plistlib.dumps(
        {
            "method": method,
            "teamID": team_id,
            "signingStyle": "automatic",
            "destination": "export",
            "uploadSymbols": True,
            "manageAppVersionAndBuildNumber": False,
        }
    )


def auth_key(settings: Settings, job_id: str | None) -> xcode.AuthKey | None:
    """The job's App Store Connect API key as an xcodebuild auth key (or None)."""
    if job_id is None:
        return None
    creds = asc_credentials.load(settings, job_id)
    if creds is None:
        return None
    key_path = asc_credentials._p8_path(settings, job_id)
    if not key_path.is_file():
        key_path = Path(settings.asc_api_key_p8_path)
    return xcode.AuthKey(key_path=key_path, key_id=creds.key_id, issuer_id=creds.issuer_id)


def unsigned_ipa(archive_path: Path, out: Path) -> Path:
    """Zip the archived ``.app`` into ``Payload/`` (an unsigned IPA, like CodeMagic's)."""
    apps = sorted((archive_path / "Products" / "Applications").glob("*.app"))
    if not apps:
        raise RuntimeError(f"no .app inside {archive_path}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(apps[0].rglob("*")):
            zf.write(path, Path("Payload") / apps[0].name / path.relative_to(apps[0]))
    return out


def deliver(
    app_dir: Path,
    out_dir: Path,
    *,
    settings: Settings,
    job_id: str | None = None,
    version: str = "1.0",
    build: str | None = None,
) -> DeliveryResult:
    """Archive the SwiftUI app and export an IPA; upload only when explicitly enabled."""
    simulator.require_toolchain()
    identity = identity_from_project(app_dir)
    scheme = target_name(identity.app_name)
    number = build or build_number()
    auth = auth_key(settings, job_id)
    signed = bool(settings.xcode_team_id and auth)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / f"{scheme}.xcarchive"
    if archive_path.exists():
        shutil.rmtree(archive_path)
    settings_args = {"MARKETING_VERSION": version, "CURRENT_PROJECT_VERSION": number}
    if signed:
        settings_args |= {
            "CODE_SIGN_STYLE": "Automatic",
            "DEVELOPMENT_TEAM": settings.xcode_team_id,
            "CODE_SIGNING_ALLOWED": "YES",
        }
    else:
        settings_args |= {"CODE_SIGNING_ALLOWED": "NO", "CODE_SIGNING_REQUIRED": "NO"}
    xcode.generate_project(app_dir)
    built = xcode.archive(
        app_dir,
        scheme,
        archive_path,
        build_settings=settings_args,
        auth=auth if signed else None,
        timeout=settings.xcode_archive_timeout_s,
    )
    result = DeliveryResult("failed", signed, version, number, log=built.log)
    if not built.ok:
        result.errors = built.errors
        return result
    result.archive = str(archive_path)
    if not signed:
        result.ipa = str(unsigned_ipa(archive_path, out_dir / f"{scheme}-unsigned.ipa"))
        result.status = "unsigned"
        return result
    assert auth is not None
    options = out_dir / "ExportOptions.plist"
    options.write_bytes(export_options(settings.xcode_team_id))
    exported = xcode.export_archive(archive_path, out_dir / "export", options, auth=auth)
    result.log += exported.log
    if not exported.ok:
        result.errors = exported.errors
        return result
    ipa = next((out_dir / "export").glob("*.ipa"))
    result.ipa = str(ipa)
    result.upload_command = xcode.upload_command(ipa, auth)
    result.status = "ready_for_upload"
    if settings.ios_delivery_upload:
        upload(ipa, auth, settings=settings)
        result.status = "uploaded"
    return result


def upload(ipa: Path, auth: xcode.AuthKey, *, settings: Settings) -> None:
    """Upload a signed IPA to App Store Connect — only with ``ios_delivery_upload`` on."""
    if not settings.ios_delivery_upload:
        raise UploadBlocked("ios_delivery_upload is off: uploading to App Store Connect is gated")
    res = xcode._run(xcode.upload_command(ipa, auth), timeout=3600)
    if res.returncode != 0:
        raise RuntimeError(f"altool upload failed: {(res.stderr or res.stdout)[-1500:]}")


def main(argv: list[str] | None = None) -> int:
    """Deliver an existing SwiftUI run (``--run-dir``): archive, export, report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--version", default="1.0")
    args = parser.parse_args(argv)
    if not xcode.toolchain_available():
        print("xcodegen / xcodebuild / xcrun not found: delivery only runs on a Mac worker")
        return 2
    paths = RunPaths.at(args.run_dir)
    result = deliver(
        paths.xcode_app,
        paths.run_dir / "delivery",
        settings=get_settings(),
        job_id=args.job_id,
        version=args.version,
    )
    summary = {k: v for k, v in asdict(result).items() if k != "log"}
    (paths.run_dir / "delivery" / "delivery.json").write_text(json.dumps(summary, indent=2))
    (paths.run_dir / "delivery" / "archive.log").write_text(result.log)
    print(json.dumps(summary, indent=2))
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
