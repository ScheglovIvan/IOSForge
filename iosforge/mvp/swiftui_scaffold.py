"""Deterministic SwiftUI scaffold that hard-wires the screen-id contract into every app.

The model never writes navigation, tabs, headless mode, the unknown-id screen,
permission prompters or the XcodeGen project: this module plans them from
``app_spec.json`` (:func:`build_plan`) and renders them through
:mod:`iosforge.mvp.swiftui_templates`, so ``SCREEN_NAV_CONTRACT`` holds for every
generated app regardless of model output. The model only fills fixed extension
points: ``App/Theme/``, ``App/Components/`` (incl. ``AppTabBar``),
``App/Features/<id>/`` and ``App/Fixtures/``. :func:`enforce_contract` restores
the contract byte-for-byte and strips foreign files from contract directories
before every compile-gate pass. Reference: ``docs/swiftui-reference``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iosforge.mvp import swiftui_templates as tpl
from iosforge.mvp.swiftui_navigation import Tab, derive_tabs, has_tab_bar, tab_owners
from iosforge.mvp.swiftui_permissions import PERMISSIONS_DIR, PermissionKind, kinds_for
from iosforge.mvp.swiftui_prompters import PROMPTERS, render_prompter
from iosforge.mvp.swiftui_templates import PENDING_MARKER, ScreenEntry, TabEntry, swift_str

__all__ = [
    "PENDING_MARKER",
    "AppIdentity",
    "ContractReport",
    "NavPlan",
    "ScreenEntry",
    "build_plan",
    "declared_kinds",
    "enforce_contract",
    "pending_screens",
    "screen_entries",
    "swift_str",
    "target_name",
    "write_scaffold",
]

DEPLOYMENT_TARGET = "17.0"
CONTRACT_DIRS = ("App/Navigation", "App/Headless", PERMISSIONS_DIR, "App/Support")
MODEL_DIRS = ("App/Theme", "App/Components", "App/Features", "App/Fixtures")
APP_ROOT_ENTRIES = {"project.yml", "App", "Resources", "Config"}

_FULL_SCREEN = re.compile(r"\b(paywall|subscription|upgrade)\b", re.I)
_MODAL = re.compile(r"\b(sheet|modal|popup|pop-up|dialog|alert|picker)\b", re.I)
_ONBOARDING = re.compile(r"\b(splash|launch|onboarding|welcome|intro|loading)\b", re.I)


@dataclass(frozen=True)
class AppIdentity:
    """Name and bundle id the scaffold renders the contract files for."""

    app_name: str
    bundle_id: str


@dataclass(frozen=True)
class NavPlan:
    """Screens with final presentation/owner, root tabs and tab-bar visibility."""

    entries: list[ScreenEntry]
    tabs: list[TabEntry]
    shows_tab_bar: bool


@dataclass
class ContractReport:
    """What :func:`enforce_contract` fixed or found: restored, removed, unexpected paths."""

    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    @property
    def tampered(self) -> list[str]:
        return [*self.restored, *self.removed]


def _swift_suffix(screen_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", screen_id).strip("_") or "screen"
    return cleaned[0].upper() + cleaned[1:]


def _base_entries(spec: dict[str, Any]) -> list[ScreenEntry]:
    entries: list[ScreenEntry] = []
    seen_ids: set[str] = set()
    used: set[str] = set()
    for screen in spec.get("screens", []):
        if not isinstance(screen, dict) or not screen.get("id"):
            continue
        screen_id = str(screen["id"])
        if screen_id in seen_ids:
            raise ValueError(f"duplicate screen id {screen_id!r} in app_spec")
        seen_ids.add(screen_id)
        name = tpl.clean_text(str(screen.get("name") or screen_id))
        onboarding = bool(_ONBOARDING.search(name))
        suffix = base = _swift_suffix(screen_id)
        counter = 2
        while suffix in used:
            suffix, counter = f"{base}_{counter}", counter + 1
        used.add(suffix)
        declared = screen.get("presentation")
        if onboarding:
            presentation: tpl.Presentation = "onboarding"
        elif declared in ("sheet", "fullScreenCover"):
            presentation = declared
        elif _FULL_SCREEN.search(name):
            presentation = "fullScreenCover"
        elif _MODAL.search(name):
            presentation = "sheet"
        else:
            presentation = "push"
        entries.append(
            ScreenEntry(
                screen_id=screen_id,
                name=name,
                case_name=f"s{suffix}",
                type_name=f"Screen{suffix}View",
                presentation=presentation,
                onboarding=onboarding,
            )
        )
    return entries


def _home_id(spec: dict[str, Any], entries: list[ScreenEntry]) -> str:
    routes = {
        str(s.get("id")): s.get("route") for s in spec.get("screens", []) if isinstance(s, dict)
    }
    candidates = [e for e in entries if e.presentation == "push"]
    for entry in candidates:
        if routes.get(entry.screen_id) == "/":
            return entry.screen_id
    if candidates:
        return candidates[0].screen_id
    raise ValueError("app_spec has no non-onboarding, non-modal screen to use as home")


def build_plan(spec: dict[str, Any]) -> NavPlan:
    """Plan screens, tabs and tab ownership from ``spec`` (raises on ambiguity).

    Tab apps get their tabs from :func:`derive_tabs`; other apps get one implicit
    tab rooted at the home screen with the tab bar hidden. Every pushed screen must
    be reachable from a tab root (:func:`tab_owners`).
    """
    entries = _base_entries(spec)
    if not entries:
        raise ValueError("app_spec has no screens to scaffold")
    tabs = derive_tabs(spec)
    shows_bar = bool(tabs)
    if not tabs:
        tabs = [Tab(_home_id(spec, entries), "Home")]
    roots = {t.screen_id for t in tabs}
    stack = [e.screen_id for e in entries if e.presentation == "push" and e.screen_id not in roots]
    owners = tab_owners(spec, tabs, stack_screens=stack)
    with_bar = {
        str(s.get("id")) for s in spec.get("screens", []) if isinstance(s, dict) and has_tab_bar(s)
    }
    final: list[ScreenEntry] = []
    for entry in entries:
        if entry.screen_id in roots:
            entry = dataclasses.replace(
                entry, presentation="tabRoot", tab_root=entry.screen_id, shows_tab_bar=shows_bar
            )
        elif entry.screen_id in owners:
            entry = dataclasses.replace(
                entry,
                tab_root=owners[entry.screen_id],
                shows_tab_bar=shows_bar and entry.screen_id in with_bar,
            )
        final.append(entry)
    by_id = {e.screen_id: e for e in final}
    tab_entries = [
        TabEntry(case_name=f"t{_swift_suffix(t.screen_id)}", title=t.title, root=by_id[t.screen_id])
        for t in tabs
    ]
    return NavPlan(final, tab_entries, shows_bar)


def screen_entries(spec: dict[str, Any]) -> list[ScreenEntry]:
    """Planned screens of ``spec`` (see :func:`build_plan`)."""
    return build_plan(spec).entries


def target_name(app_name: str) -> str:
    """Xcode target / scheme name derived from the app name (ASCII identifier)."""
    words = re.findall(r"[A-Za-z0-9]+", app_name)
    name = "".join(w[0].upper() + w[1:] for w in words) or "GeneratedApp"
    return f"App{name}" if name[0].isdigit() else name


def declared_kinds(spec: dict[str, Any]) -> list[PermissionKind]:
    """Permission kinds ``app_spec.permissions`` declares (unique, registry order)."""
    found: dict[str, PermissionKind] = {}
    for item in spec.get("permissions", []) or []:
        if isinstance(item, dict):
            for kind in kinds_for(str(item.get("permission", ""))):
                found.setdefault(kind.case, kind)
    return list(found.values())


def _purpose_strings(spec: dict[str, Any], app_name: str) -> dict[str, str]:
    strings: dict[str, str] = {}
    for item in spec.get("permissions", []) or []:
        if not isinstance(item, dict):
            continue
        reason = re.sub(r"\s*\([^)]*\)", "", str(item.get("reason") or "")).strip()
        text = tpl.clean_text(reason.split(". ")[0].rstrip(".")) if reason else ""
        for kind in kinds_for(str(item.get("permission", ""))):
            for key in kind.plist_keys:
                strings.setdefault(
                    key, f"{text}." if text else f"{app_name} uses this to provide its features."
                )
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
    """XcodeGen spec: one iOS app target with a scheme, signing off, Info.plist in Config/."""
    lines = [
        f"name: {target}",
        "options:",
        "  deploymentTarget:",
        f'    iOS: "{DEPLOYMENT_TARGET}"',
        "targets:",
        f"  {target}:",
        "    type: application",
        "    platform: iOS",
        "    scheme: {}",
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
        "      path: Config/Info.plist",
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


def _bundled_fonts(app_dir: Path) -> list[str]:
    fonts = app_dir / "Resources" / "Fonts"
    if not fonts.is_dir():
        return []
    return sorted(f.name for f in fonts.iterdir() if f.suffix.lower() in (".ttf", ".otf"))


def render_contract(app_dir: Path, spec: dict[str, Any], identity: AppIdentity) -> dict[str, str]:
    """Contract files (relative path → text) for ``spec``, given resources in ``app_dir``."""
    plan = build_plan(spec)
    target = target_name(identity.app_name)
    media = app_dir / "Resources" / "Media"
    files = {
        "project.yml": render_project_yml(
            target=target,
            app_name=identity.app_name,
            bundle_id=identity.bundle_id,
            fonts=_bundled_fonts(app_dir),
            purpose_strings=_purpose_strings(spec, identity.app_name),
            has_media=media.is_dir() and any(media.iterdir()),
        ),
        "App/App.swift": tpl.render_app(target),
        "App/Navigation/ScreenID.swift": tpl.render_screen_id(plan.entries, plan.tabs),
        "App/Navigation/AppTab.swift": tpl.render_app_tab(plan.tabs, shows_bar=plan.shows_tab_bar),
        "App/Navigation/Router.swift": tpl.ROUTER,
        "App/Navigation/RootView.swift": tpl.ROOT_VIEW,
        "App/Headless/Headless.swift": tpl.HEADLESS,
        f"{PERMISSIONS_DIR}/Permissions.swift": tpl.render_permissions(),
        "App/Support/MediaAsset.swift": tpl.MEDIA,
    }
    for kind in declared_kinds(spec):
        if kind.case in PROMPTERS:
            name, text = render_prompter(kind.case)
            files[f"{PERMISSIONS_DIR}/{name}"] = text
    return files


def _rel(path: Path, app_dir: Path) -> str:
    return path.relative_to(app_dir).as_posix()


def enforce_contract(app_dir: Path, spec: dict[str, Any], identity: AppIdentity) -> ContractReport:
    """Make the contract layer byte-identical to the template; report every deviation.

    Contract files are rewritten when changed or missing; any other file inside a
    contract directory (or loose under ``App/``) is deleted. Unknown top-level
    entries of the app or of ``App/`` and feature folders of unknown screens are
    reported as ``unexpected`` (gate errors) but left in place.
    """
    report = ContractReport()
    contract = render_contract(app_dir, spec, identity)
    for rel, text in contract.items():
        path = app_dir / rel
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            report.restored.append(rel)
    for folder in CONTRACT_DIRS:
        root = app_dir / folder
        for path in sorted(root.rglob("*")) if root.is_dir() else []:
            if path.is_file() and _rel(path, app_dir) not in contract:
                path.unlink()
                report.removed.append(_rel(path, app_dir))
    app_root = app_dir / "App"
    allowed_dirs = {Path(d).name for d in (*CONTRACT_DIRS, *MODEL_DIRS)}
    for path in sorted(app_root.iterdir()) if app_root.is_dir() else []:
        if path.is_file() and _rel(path, app_dir) not in contract:
            path.unlink()
            report.removed.append(_rel(path, app_dir))
        elif path.is_dir() and path.name not in allowed_dirs:
            report.unexpected.append(_rel(path, app_dir))
    for path in sorted(app_dir.iterdir()):
        if path.name not in APP_ROOT_ENTRIES and path.suffix != ".xcodeproj":
            report.unexpected.append(_rel(path, app_dir))
    screen_ids = {e.screen_id for e in build_plan(spec).entries}
    features = app_dir / "App" / "Features"
    for path in sorted(features.iterdir()) if features.is_dir() else []:
        if path.name not in screen_ids:
            report.unexpected.append(_rel(path, app_dir))
    return report


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

    Contract files (:func:`render_contract`) are always rewritten; model-owned
    extension points (theme, tab bar, fixtures, screen views) are only created when
    missing so re-running the scaffold never discards generated code.
    """
    plan = build_plan(spec)
    if fonts_dir is not None and fonts_dir.is_dir():
        dst = app_dir / "Resources" / "Fonts"
        dst.mkdir(parents=True, exist_ok=True)
        for font in sorted(fonts_dir.iterdir()):
            if font.suffix.lower() in (".ttf", ".otf"):
                shutil.copy2(font, dst / font.name)
    if media_dir is not None and media_dir.is_dir() and any(media_dir.iterdir()):
        shutil.copytree(media_dir, app_dir / "Resources" / "Media", dirs_exist_ok=True)

    enforce_contract(app_dir, spec, AppIdentity(app_name, bundle_id))
    extension_points = {
        "App/Theme/Theme.swift": tpl.THEME_PLACEHOLDER,
        "App/Components/AppTabBar.swift": tpl.TAB_BAR_PLACEHOLDER,
        "App/Fixtures/Fixtures.swift": tpl.FIXTURES_PLACEHOLDER,
        **{entry.view_path: tpl.render_pending(entry) for entry in plan.entries},
    }
    for rel, text in extension_points.items():
        path = app_dir / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return plan.entries


def pending_screens(app_dir: Path, entries: list[ScreenEntry]) -> list[str]:
    """Screen ids whose view is still the scaffold placeholder."""
    return [
        e.screen_id
        for e in entries
        if PENDING_MARKER in (app_dir / e.view_path).read_text(encoding="utf-8")
    ]
