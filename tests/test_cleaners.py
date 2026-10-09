"""Level 2 Phase D: cleaner capability modules (rendering, seeds, Mac e2e on seeded data)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from iosforge.mvp import functional, swiftui_cleaners, swiftui_gen
from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_permissions import permission_violations
from tests.test_compliance_ios import _first_iphone
from tests.test_feasibility import _spec_three_screens

PHOTOS_SCREEN = """import SwiftUI

struct Screen0001View: View {
    @State private var groups: [PhotoDuplicateGroup] = []
    @State private var status = "Not scanned"

    var body: some View {
        VStack(spacing: 16) {
            Text(status)
            Button("Scan") {
                Task {
                    groups = (try? await PhotosCleaner.scan()) ?? []
                    status = "\\(groups.reduce(0) { $0 + $1.duplicates }) duplicates"
                }
            }
            Button("Delete duplicates") {
                Task {
                    let removed = (try? await PhotosCleaner.deleteDuplicates(in: groups)) ?? 0
                    status = "Removed \\(removed)"
                }
            }
        }
    }
}
"""

CONTACTS_SCREEN = """import SwiftUI

struct Screen0001View: View {
    @State private var groups: [ContactDuplicateGroup] = []
    @State private var status = "Not scanned"

    var body: some View {
        VStack(spacing: 16) {
            Text(status)
            Button("Scan") {
                Task {
                    groups = (try? await ContactsCleaner.scan()) ?? []
                    status = "\\(groups.count) groups"
                }
            }
            Button("Merge") {
                let merged = (try? ContactsCleaner.merge(groups)) ?? 0
                status = "Merged \\(merged)"
            }
        }
    }
}
"""

STORAGE_SCREEN = """import SwiftUI

struct Screen0001View: View {
    @State private var text = "Tap to check"

    var body: some View {
        VStack(spacing: 16) {
            Text(text)
            Button("Check storage") {
                if let snapshot = try? StorageScan.scan() {
                    text = StorageScan.format(snapshot.available) + " free"
                }
            }
        }
    }
}
"""


def _spec(module: str, kind: str) -> dict[str, Any]:
    spec = _spec_three_screens()
    spec["capabilities"] = [
        {"name": module, "kind": kind, "expected_behavior": "cleans", "screens": ["0001"],
         "tier": 2, "module": module}
    ]  # fmt: skip
    return spec


@pytest.mark.parametrize(
    ("module", "kind", "file", "prompter", "usage"),
    [
        ("photos_cleaner", "photos_cleaner", "PhotosCleaner.swift", "PhotosPermission",
         "NSPhotoLibraryUsageDescription"),
        ("contacts_cleaner", "contacts_cleaner", "ContactsCleaner.swift", "ContactsPermission",
         "NSContactsUsageDescription"),
        ("storage_scan", "storage_scan", "StorageScan.swift", None, None),
    ],
)  # fmt: skip
def test_cleaner_module_renders_into_the_scaffold(
    tmp_path: Path, module: str, kind: str, file: str, prompter: str | None, usage: str | None
) -> None:
    spec = _spec(module, kind)
    app = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(app, spec, app_name="Clean", bundle_id="dev.iosforge.clean")
    assert (app / caps.CAPABILITIES_DIR / file).is_file()
    project = (app / "project.yml").read_text()
    if usage:
        assert f"        {usage}: " in project
    if prompter:
        assert prompter in swiftui_gen.prompter_names(spec, app)
    assert permission_violations(app) == []
    check = caps.functional_checks(caps.select(spec, caps.integ.Integrations()))[0]
    assert check.screen_id == "0001"


def test_seed_photos_contain_exactly_one_duplicate(tmp_path: Path) -> None:
    files = swiftui_cleaners.seed_photos(tmp_path)
    blobs = [f.read_bytes() for f in files]
    assert len(files) == 3 and blobs[0] == blobs[2] and blobs[0] != blobs[1]
    assert Image.open(files[1]).size == (480, 360)


def test_seed_vcard_has_a_duplicate_person() -> None:
    assert swiftui_cleaners.SEED_VCARD.count("FN:Iosforge Duplicate") == 2


def _run(tmp_path: Path, module: str, kind: str, screen: str) -> dict[str, Any]:
    udid = _first_iphone()
    assert udid is not None
    spec = _spec(module, kind)
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(spec))
    swiftui_gen.write_scaffold(
        paths.xcode_app, spec, app_name="Clean Demo", bundle_id=f"dev.iosforge.{module}"
    )
    (paths.xcode_app / "App/Features/0001/Screen0001View.swift").write_text(screen)
    return functional.run(paths, udid=udid, spec=spec, timeout=1500)


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_photos_cleaner_deletes_seeded_duplicates(tmp_path: Path) -> None:
    report = _run(tmp_path, "photos_cleaner", "photos_cleaner", PHOTOS_SCREEN)
    assert report["ok"], report
    assert {"photos.duplicates_found", "photos.deleted"} <= set(report["checks"][0]["events"])


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_contacts_cleaner_merges_seeded_duplicates(tmp_path: Path) -> None:
    report = _run(tmp_path, "contacts_cleaner", "contacts_cleaner", CONTACTS_SCREEN)
    assert report["ok"], report


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_storage_scan_reads_the_device(tmp_path: Path) -> None:
    report = _run(tmp_path, "storage_scan", "storage_scan", STORAGE_SCREEN)
    assert report["ok"], report
