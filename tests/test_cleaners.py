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
    @State private var confirming = false

    var body: some View {
        VStack(spacing: 16) {
            Text(status)
            Button("Scan") {
                Task {
                    groups = (try? await ContactsCleaner.scan()) ?? []
                    status = "\\(groups.count) groups"
                }
            }
            Button("Merge duplicates") { confirming = true }
                .alert("Merge duplicates?", isPresented: $confirming) {
                    Button("Merge", role: .destructive) {
                        Task {
                            let merged = (try? await ContactsCleaner.merge(groups)) ?? 0
                            status = "Merged \\(merged)"
                        }
                    }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("Extra cards are deleted after their numbers and emails are copied.")
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


def test_seed_vcard_has_one_duplicate_and_a_namesake() -> None:
    cards = swiftui_cleaners.SEED_VCARD.split("END:VCARD")
    same_name = [c for c in cards if "FN:Iosforge Duplicate" in c]
    digits = ["".join(ch for ch in c.split("TEL;TYPE=CELL:")[1].split("\n")[0] if ch.isdigit())
              for c in same_name]  # fmt: skip
    assert len(same_name) == 3 and digits[0] == digits[1] != digits[2]
    assert "EMAIL:" in same_name[1] and "EMAIL:" not in same_name[0]


def test_cleaner_modules_never_merge_by_name_or_hash_alone() -> None:
    contacts = swiftui_cleaners.CONTACTS_SWIFT
    assert "isDisjoint(with: own)" in contacts and "CNContactEmailAddressesKey" in contacts
    assert "request.update(keeper)" in contacts and "contacts.error" in contacts
    photos = swiftui_cleaners.PHOTOS_SWIFT
    assert "SHA256.hash" in photos and "pixelWidth" in photos and "value != 0" in photos
    assert "photos.error" in photos
    assert swiftui_cleaners.CONFIRM_ID in swiftui_cleaners.CONTACTS_RULE
    assert ".alert" in swiftui_cleaners.CONTACTS_RULE


def test_contacts_check_confirms_before_merging() -> None:
    spec = _spec("contacts_cleaner", "contacts_cleaner")
    check = caps.functional_checks(caps.select(spec, caps.integ.Integrations()))[0]
    kinds = [(step.kind, step.identifier) for step in check.steps]
    clean = kinds.index(("tap", swiftui_cleaners.CLEAN_ID))
    assert kinds[clean + 1] == ("tap", swiftui_cleaners.CONFIRM_ID)


def test_patterns_are_word_bounded() -> None:
    import re

    assert not re.search(swiftui_cleaners.CLEAN_PATTERN, "Start Free Trial", re.I)
    assert re.search(swiftui_cleaners.CLEAN_PATTERN, "Delete duplicates", re.I)
    assert not re.search(swiftui_cleaners.SCAN_PATTERN, "Unlock Premium", re.I)


def test_system_and_allow_steps_render_into_the_driver() -> None:
    from iosforge.mvp import swiftui_functional as swf

    spec = _spec("photos_cleaner", "photos_cleaner")
    checks = caps.functional_checks(caps.select(spec, caps.integ.Integrations()))
    source = swf.render_ui_tests(checks, [])
    assert 'tapSystemAlert("Delete"' in source and "allowIfAsked(" in source


def test_privacy_policy_covers_the_cleaner_permissions() -> None:
    from iosforge.mvp import legal_pages

    practices = legal_pages._PERMISSION_PRACTICES
    assert "on your device" in practices["NSContactsUsageDescription"].detail
    assert "NSPhotoLibraryUsageDescription" in practices


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
