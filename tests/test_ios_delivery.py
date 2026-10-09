"""Native delivery: archive → IPA, signing switch, gated upload (xcodebuild mocked)."""

from __future__ import annotations

import plistlib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import ios_delivery, simulator, swiftui_gen, xcode

SPEC: dict[str, Any] = {
    "app_name": "Demo",
    "screens": [{"id": "0011", "name": "Home", "route": "/"}],
    "navigation": {"type": "stack", "map": []},
}


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(path, SPEC, app_name="Demo", bundle_id="com.ex.d")
    monkeypatch.setattr(simulator, "require_toolchain", lambda: None)
    monkeypatch.setattr(xcode, "generate_project", lambda app_dir: None)
    return path


def _fake_archive(calls: list[dict[str, Any]]) -> Any:
    def archive(app_dir: Path, scheme: str, archive_path: Path, **kw: Any) -> xcode.BuildOutcome:
        calls.append({"scheme": scheme, **kw})
        bundle = archive_path / "Products" / "Applications" / f"{scheme}.app"
        bundle.mkdir(parents=True)
        (bundle / scheme).write_bytes(b"binary")
        return xcode.BuildOutcome(True, [], "** ARCHIVE SUCCEEDED **")

    return archive


def test_build_number_and_export_options() -> None:
    assert ios_delivery.build_number(0) == "7001010000"
    options = plistlib.loads(ios_delivery.export_options("TEAM123"))
    assert options["method"] == "app-store-connect" and options["teamID"] == "TEAM123"
    assert options["destination"] == "export" and options["signingStyle"] == "automatic"


def test_unsigned_delivery_produces_an_unsigned_ipa(
    app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(xcode, "archive", _fake_archive(calls))
    result = ios_delivery.deliver(app, tmp_path / "out", settings=Settings(), build="42")
    assert result.status == "unsigned" and not result.signed and result.build_number == "42"
    assert calls[0]["build_settings"]["CODE_SIGNING_ALLOWED"] == "NO" and calls[0]["auth"] is None
    with zipfile.ZipFile(result.ipa) as zf:
        assert zf.namelist() == ["Payload/Demo.app/Demo"]


def test_signed_delivery_exports_and_stops_before_upload(
    app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    auth = xcode.AuthKey(tmp_path / "k.p8", "KEYID", "ISSUER")
    monkeypatch.setattr(ios_delivery, "auth_key", lambda s, j: auth)
    monkeypatch.setattr(xcode, "archive", _fake_archive(calls))

    def export(
        archive_path: Path, export_dir: Path, options: Path, **kw: Any
    ) -> xcode.BuildOutcome:
        export_dir.mkdir(parents=True)
        (export_dir / "Demo.ipa").write_bytes(b"ipa")
        assert plistlib.loads(options.read_bytes())["teamID"] == "TEAM"
        return xcode.BuildOutcome(True, [], "EXPORT SUCCEEDED")

    monkeypatch.setattr(xcode, "export_archive", export)
    ran: list[Any] = []
    monkeypatch.setattr(xcode, "_run", lambda *a, **k: ran.append(a))

    result = ios_delivery.deliver(
        app, tmp_path / "out", settings=Settings(xcode_team_id="TEAM"), job_id="job-1"
    )

    assert result.status == "ready_for_upload" and result.signed
    assert calls[0]["build_settings"]["DEVELOPMENT_TEAM"] == "TEAM" and calls[0]["auth"] is auth
    assert result.upload_command[:3] == ["xcrun", "altool", "--upload-app"]
    assert ran == []


def test_upload_is_blocked_unless_explicitly_enabled(tmp_path: Path) -> None:
    auth = xcode.AuthKey(tmp_path / "k.p8", "KEYID", "ISSUER")
    with pytest.raises(ios_delivery.UploadBlocked):
        ios_delivery.upload(tmp_path / "a.ipa", auth, settings=Settings())


def test_archive_failure_is_reported(
    app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        xcode, "archive", lambda *a, **k: xcode.BuildOutcome(False, ["x: error: boom"], "log")
    )
    result = ios_delivery.deliver(app, tmp_path / "out", settings=Settings())
    assert result.status == "failed" and result.errors == ["x: error: boom"]


def test_cli_exits_2_without_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    assert ios_delivery.main(["--run-dir", "/tmp/x"]) == 2
