"""Deterministic SwiftUI scaffold that hard-wires the screen-id contract into every app.

The model never writes navigation, headless mode, the unknown-id screen or the
XcodeGen project: this module renders them from ``app_spec.json`` so
``SCREEN_NAV_CONTRACT`` holds for every generated app regardless of model output.
The model only fills the fixed extension points it is given: ``App/Theme/``,
``App/Features/<id>/Screen<id>View.swift``, ``App/Fixtures/`` and
``App/Permissions/`` prompters. Reference implementation:
``docs/swiftui-reference`` (Phase 0 spike).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from iosforge.mvp.swiftui_permissions import PERMISSION_KINDS, PERMISSIONS_DIR, kind_for

Presentation = Literal["root", "push", "sheet", "fullScreenCover"]

DEPLOYMENT_TARGET = "17.0"
PENDING_MARKER = "PendingScreenView(id:"

_MODAL = re.compile(r"\b(paywall|sheet|modal|popup|pop-up|dialog|alert|picker)\b", re.I)
_ONBOARDING = re.compile(r"\b(splash|launch|onboarding|welcome|intro|loading)\b", re.I)


@dataclass(frozen=True)
class ScreenEntry:
    """One ``app_spec`` screen as the scaffold sees it (Swift names + presentation)."""

    screen_id: str
    name: str
    case_name: str
    type_name: str
    presentation: Presentation
    onboarding: bool

    @property
    def view_path(self) -> str:
        return f"App/Features/{self.screen_id}/{self.type_name}.swift"


def _swift_suffix(screen_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", screen_id).strip("_") or "screen"
    return cleaned[0].upper() + cleaned[1:]


def _presentation(screen: dict[str, Any], onboarding: bool) -> Presentation:
    declared = screen.get("presentation")
    if declared in ("root", "push", "sheet", "fullScreenCover"):
        return cast(Presentation, declared)
    if onboarding or screen.get("route") == "/":
        return "root"
    return "sheet" if _MODAL.search(str(screen.get("name", ""))) else "push"


def screen_entries(spec: dict[str, Any]) -> list[ScreenEntry]:
    """Scaffold view of every screen in ``spec`` (order preserved)."""
    entries: list[ScreenEntry] = []
    for screen in spec.get("screens", []):
        if not isinstance(screen, dict) or not screen.get("id"):
            continue
        screen_id = str(screen["id"])
        name = str(screen.get("name") or screen_id)
        onboarding = bool(_ONBOARDING.search(name))
        suffix = _swift_suffix(screen_id)
        entries.append(
            ScreenEntry(
                screen_id=screen_id,
                name=name,
                case_name=f"s{suffix}" if suffix[0].isdigit() else suffix[0].lower() + suffix[1:],
                type_name=f"Screen{suffix}View",
                presentation=_presentation(screen, onboarding),
                onboarding=onboarding,
            )
        )
    return entries


def home_entry(entries: list[ScreenEntry]) -> ScreenEntry:
    """Base screen under pushed/modal targets: first non-onboarding root, else first."""
    for entry in entries:
        if entry.presentation == "root" and not entry.onboarding:
            return entry
    for entry in entries:
        if not entry.onboarding:
            return entry
    return entries[0]


def target_name(app_name: str) -> str:
    """Xcode target / scheme name derived from the app name (ASCII identifier)."""
    words = re.findall(r"[A-Za-z0-9]+", app_name)
    name = "".join(w[0].upper() + w[1:] for w in words) or "GeneratedApp"
    return f"App{name}" if name[0].isdigit() else name


def _purpose_strings(spec: dict[str, Any], app_name: str) -> dict[str, str]:
    strings: dict[str, str] = {}
    for item in spec.get("permissions", []) or []:
        if not isinstance(item, dict):
            continue
        kind = kind_for(str(item.get("permission", "")))
        if kind is None:
            continue
        reason = re.sub(r"\s*\([^)]*\)", "", str(item.get("reason") or "")).strip()
        text = reason.split(". ")[0].rstrip(".") if reason else ""
        for key in kind.plist_keys:
            strings[key] = f"{text}." if text else f"{app_name} uses this to provide its features."
    return strings


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_project_yml(
    *,
    target: str,
    app_name: str,
    bundle_id: str,
    fonts: list[str],
    purpose_strings: dict[str, str],
    has_media: bool,
) -> str:
    """XcodeGen spec: one iOS app target, signing off, Info.plist from properties."""
    lines = [
        f"name: {target}",
        "options:",
        "  deploymentTarget:",
        f'    iOS: "{DEPLOYMENT_TARGET}"',
        "targets:",
        f"  {target}:",
        "    type: application",
        "    platform: iOS",
        "    sources:",
        "      - path: App",
    ]
    if fonts:
        lines.append("      - path: Resources/Fonts")
    if has_media:
        lines += ["      - path: Resources/Media", "        type: folder"]
    lines += [
        "    settings:",
        "      base:",
        f"        PRODUCT_BUNDLE_IDENTIFIER: {bundle_id}",
        "        GENERATE_INFOPLIST_FILE: YES",
        '        MARKETING_VERSION: "1.0"',
        '        CURRENT_PROJECT_VERSION: "1"',
        '        SWIFT_VERSION: "5.0"',
        "        CODE_SIGNING_ALLOWED: NO",
        '        TARGETED_DEVICE_FAMILY: "1"',
        "    info:",
        "      path: App/Info.plist",
        "      properties:",
        f"        CFBundleDisplayName: {_yaml_str(app_name)}",
        "        UILaunchScreen: {}",
        "        UISupportedInterfaceOrientations: [UIInterfaceOrientationPortrait]",
        "        CFBundleURLTypes:",
        f"          - CFBundleURLName: {bundle_id}",
        "            CFBundleURLSchemes: [iosforge]",
    ]
    if fonts:
        lines.append("        UIAppFonts:")
        lines += [f"          - {_yaml_str(font)}" for font in fonts]
    for key, text in sorted(purpose_strings.items()):
        lines.append(f"        {key}: {_yaml_str(text)}")
    return "\n".join(lines) + "\n"


def _swift_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_screen_id(entries: list[ScreenEntry], home: ScreenEntry) -> str:
    cases = "\n".join(
        f"    case {e.case_name} = {_swift_str(e.screen_id)}  // {e.name}" for e in entries
    )
    presentations = "\n".join(f"        case .{e.case_name}: .{e.presentation}" for e in entries)
    onboarding = [e for e in entries if e.onboarding]
    onboarding_check = (
        "[" + ", ".join(f".{e.case_name}" for e in onboarding) + "].contains(self)"
        if onboarding
        else "false"
    )
    views = "\n".join(f"        case .{e.case_name}: {e.type_name}()" for e in entries)
    first_onboarding = f".{onboarding[0].case_name}" if onboarding else "nil"
    return f"""import SwiftUI

/// Every screen of the app, keyed by its app_spec id (the `-screen-id` value).
/// Generated by the IOSForge scaffold — do not edit.
enum ScreenID: String, CaseIterable, Identifiable, Hashable {{
{cases}

    var id: String {{ rawValue }}

    /// Base screen that pushed and modal targets sit on.
    static let home: ScreenID = .{home.case_name}

    /// First onboarding screen for a normal (non-headless) first launch.
    static let onboardingStart: ScreenID? = {first_onboarding}

    var presentation: ScreenPresentation {{
        switch self {{
{presentations}
        }}
    }}

    var isOnboarding: Bool {{
        {onboarding_check}
    }}

    @MainActor @ViewBuilder
    func makeView() -> some View {{
        switch self {{
{views}
        }}
    }}
}}

enum ScreenPresentation {{
    case root, push, sheet, fullScreenCover
}}
"""


_ROUTER = """import Observation
import SwiftUI

/// Navigation state. `open(_:)` implements the headless screen-id contract:
/// it rebuilds the stack so the requested screen is on top, with modal targets
/// presented over `ScreenID.home`. Generated by the IOSForge scaffold — do not edit.
@Observable
final class Router {
    var base: ScreenID = .home
    var path: [ScreenID] = []
    var sheet: ScreenID?
    var cover: ScreenID?
    var unknownID: String?

    static let onboardingDoneKey = "iosforge.onboarding_done"

    static func launch() -> Router {
        let router = Router()
        if let raw = Headless.screenID {
            router.open(raw)
        } else if !UserDefaults.standard.bool(forKey: onboardingDoneKey),
                  let start = ScreenID.onboardingStart {
            router.base = start
        }
        return router
    }

    func open(_ raw: String) {
        path = []
        sheet = nil
        cover = nil
        unknownID = nil
        base = .home
        guard let screen = ScreenID(rawValue: raw) else {
            unknownID = raw
            return
        }
        show(screen)
    }

    func show(_ screen: ScreenID) {
        switch screen.presentation {
        case .root:
            path = []
            base = screen
        case .push:
            path.append(screen)
        case .sheet:
            sheet = screen
        case .fullScreenCover:
            cover = screen
        }
    }

    func dismiss() {
        if cover != nil {
            cover = nil
        } else if sheet != nil {
            sheet = nil
        } else if !path.isEmpty {
            path.removeLast()
        }
    }

    func finishOnboarding() {
        if !Headless.isActive {
            UserDefaults.standard.set(true, forKey: Router.onboardingDoneKey)
        }
        path = []
        base = .home
    }

    func handle(_ url: URL) {
        guard url.scheme == "iosforge", url.host == "screen",
              let raw = url.pathComponents.dropFirst().first else { return }
        open(raw)
    }
}
"""

_ROOT_VIEW = """import SwiftUI

/// Hosts the navigation stack and modal presentations driven by `Router`.
/// Generated by the IOSForge scaffold — do not edit.
struct RootView: View {
    @Environment(Router.self) private var router

    var body: some View {
        @Bindable var router = router
        if let unknown = router.unknownID {
            UnknownScreenView(id: unknown)
        } else {
            NavigationStack(path: $router.path) {
                router.base.makeView()
                    .navigationDestination(for: ScreenID.self) { $0.makeView() }
            }
            .sheet(item: $router.sheet) { $0.makeView() }
            .fullScreenCover(item: $router.cover) { $0.makeView() }
        }
    }
}

/// Explicit, detectable screen for an unknown `-screen-id` (never a silent fallback).
struct UnknownScreenView: View {
    let id: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 48))
            Text("UNKNOWN SCREEN-ID")
                .font(.title2.bold())
            Text(id)
                .font(.body.monospaced())
        }
        .foregroundStyle(.red)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.white)
        .accessibilityIdentifier("iosforge.unknown-screen")
    }
}

/// Placeholder for a screen the generator has not produced yet.
struct PendingScreenView: View {
    let id: String

    var body: some View {
        Text("Screen \\(id) not generated")
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .accessibilityIdentifier("iosforge.pending-screen")
    }
}
"""

_HEADLESS = """import Foundation

/// Headless screen-id mode (`-screen-id <id>` launch argument, UserDefaults
/// argument domain). While active: no onboarding, no permission prompts,
/// fixture data only. Generated by the IOSForge scaffold — do not edit.
enum Headless {
    static let screenID: String? = UserDefaults.standard.string(forKey: "screen-id")

    static var isActive: Bool { screenID != nil }
}
"""


def _render_permissions() -> str:
    cases = "\n".join(f"    case {kind.case}" for kind in PERMISSION_KINDS)
    return f"""import Foundation

/// Every system permission prompt iOS can show. Generated by the IOSForge
/// scaffold — do not edit.
enum PermissionKind: String, CaseIterable {{
{cases}
}}

/// One concrete system prompt. Implementations live in `{PERMISSIONS_DIR}/` and are
/// only ever invoked through `Permissions.request(_:)`.
protocol PermissionPrompter {{
    static var kind: PermissionKind {{ get }}
    static func prompt() async -> Bool
}}

/// The single gate for permission prompts: a no-op returning `false` in headless
/// screen-id mode so no system alert can cover the captured screen.
enum Permissions {{
    @discardableResult
    static func request<P: PermissionPrompter>(_ prompter: P.Type) async -> Bool {{
        guard !Headless.isActive else {{ return false }}
        return await prompter.prompt()
    }}
}}
"""


_MEDIA = """import UIKit

/// Bundled media from the source app (`Resources/Media`, folder reference).
/// Generated by the IOSForge scaffold — do not edit.
enum MediaAsset {
    static func url(_ file: String) -> URL? {
        Bundle.main.url(forResource: file, withExtension: nil, subdirectory: "Media")
    }

    static func image(_ file: String) -> UIImage? {
        url(file).flatMap { UIImage(contentsOfFile: $0.path) }
    }
}
"""

_THEME_PLACEHOLDER = """import SwiftUI

/// Design tokens. Replaced by the theme task.
enum Theme {}
"""

_FIXTURES_PLACEHOLDER = """import Foundation

/// Fixture data for headless rendering; screens add `extension Fixtures` files.
enum Fixtures {}
"""


def _render_app(target: str) -> str:
    return f"""import SwiftUI

/// App entry point. Generated by the IOSForge scaffold — do not edit.
@main
struct {target}App: App {{
    @State private var router = Router.launch()

    var body: some Scene {{
        WindowGroup {{
            RootView()
                .environment(router)
                .onOpenURL {{ router.handle($0) }}
        }}
    }}
}}
"""


def _render_pending(entry: ScreenEntry) -> str:
    return f"""import SwiftUI

/// {entry.name} — placeholder until the screen task generates it.
struct {entry.type_name}: View {{
    var body: some View {{
        {PENDING_MARKER} {_swift_str(entry.screen_id)})
    }}
}}
"""


def write_scaffold(
    app_dir: Path,
    spec: dict[str, Any],
    *,
    app_name: str,
    bundle_id: str,
    fonts_dir: Path | None = None,
    media_dir: Path | None = None,
) -> list[ScreenEntry]:
    """Render the full Xcode project skeleton into ``app_dir``; return its screens.

    Contract files (``App/App.swift``, ``App/Navigation/``, ``App/Headless/``,
    ``App/Permissions/Permissions.swift``, ``project.yml``) are always rewritten;
    model-owned extension points (theme, fixtures, screen views) are only created
    when missing so re-running the scaffold never discards generated code.
    """
    entries = screen_entries(spec)
    if not entries:
        raise ValueError("app_spec has no screens to scaffold")
    home = home_entry(entries)
    target = target_name(app_name)

    fonts: list[str] = []
    if fonts_dir is not None and fonts_dir.is_dir():
        dst = app_dir / "Resources" / "Fonts"
        dst.mkdir(parents=True, exist_ok=True)
        for font in sorted(fonts_dir.iterdir()):
            if font.suffix.lower() in (".ttf", ".otf"):
                shutil.copy2(font, dst / font.name)
                fonts.append(font.name)
    has_media = media_dir is not None and media_dir.is_dir() and any(media_dir.iterdir())
    if has_media and media_dir is not None:
        shutil.copytree(media_dir, app_dir / "Resources" / "Media", dirs_exist_ok=True)

    contract_files = {
        "project.yml": render_project_yml(
            target=target,
            app_name=app_name,
            bundle_id=bundle_id,
            fonts=fonts,
            purpose_strings=_purpose_strings(spec, app_name),
            has_media=has_media,
        ),
        "App/App.swift": _render_app(target),
        "App/Navigation/ScreenID.swift": _render_screen_id(entries, home),
        "App/Navigation/Router.swift": _ROUTER,
        "App/Navigation/RootView.swift": _ROOT_VIEW,
        "App/Headless/Headless.swift": _HEADLESS,
        f"{PERMISSIONS_DIR}/Permissions.swift": _render_permissions(),
        "App/Support/MediaAsset.swift": _MEDIA,
    }
    extension_points = {
        "App/Theme/Theme.swift": _THEME_PLACEHOLDER,
        "App/Fixtures/Fixtures.swift": _FIXTURES_PLACEHOLDER,
        **{entry.view_path: _render_pending(entry) for entry in entries},
    }
    for rel, text in contract_files.items():
        path = app_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel, text in extension_points.items():
        path = app_dir / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return entries


def pending_screens(app_dir: Path, entries: list[ScreenEntry]) -> list[str]:
    """Screen ids whose view is still the scaffold placeholder."""
    return [
        e.screen_id
        for e in entries
        if PENDING_MARKER in (app_dir / e.view_path).read_text(encoding="utf-8")
    ]
