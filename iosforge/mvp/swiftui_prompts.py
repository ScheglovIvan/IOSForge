"""Inline prompts for the SwiftUI codegen (theme, component library, screen, compile fix).

Prompts stay inline (accepted SPEC §6 tech-debt, DECISIONS 2026-10-09 Phase 3).
They only ever ask the model to fill the scaffold's extension points; navigation,
tabs, headless mode and permission prompters are rendered by
:mod:`iosforge.mvp.swiftui_scaffold` and must not be touched. The workspace
carries ``reference/`` (``docs/swiftui-reference``) as the worked example.
"""

from __future__ import annotations

from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp.swiftui_integrations import Integrations
from iosforge.mvp.swiftui_permissions import PERMISSIONS_DIR, SERVICE_SUFFIX
from iosforge.mvp.swiftui_scaffold import CONTRACT_DIRS, NavPlan
from iosforge.mvp.swiftui_templates import ScreenEntry, TabEntry

APP_DIR = "xcode_app"
COMPONENTS_MD = "COMPONENTS.md"


def base_rules(prompters: list[str], modules: list[str] | None = None) -> str:
    """Rules shared by every SwiftUI task (contract, navigation, headless, permissions).

    ``modules`` are the screen API rules of the app's capability modules
    (:func:`iosforge.mvp.swiftui_capabilities.screen_rules`); the core modules by default.
    """
    contract = ", ".join(f"`{APP_DIR}/{d}/`" for d in CONTRACT_DIRS)
    rules = modules if modules is not None else caps.screen_rules(caps.select({}, Integrations()))
    module_rules = "".join(f"- {rule}\n" for rule in rules)
    available = ", ".join(f"`{p}`" for p in prompters) or "none (the app declares no permissions)"
    return f"""You are generating part of a native iOS app in SwiftUI. The Xcode project
lives in `{APP_DIR}/` and is generated with XcodeGen by IOSForge.

HARD RULES
- iOS 17+, SwiftUI, Swift 5 language mode, Observation allowed. Apple frameworks only:
  no Swift packages, no CocoaPods, no UIKit view controllers (UIKit types like UIImage are fine).
- NEVER create, edit or delete anything in the scaffold: `{APP_DIR}/project.yml`,
  `{APP_DIR}/App/App.swift`, {contract}. IOSForge restores those byte-for-byte and deletes any
  foreign file there. Never create or edit an `.xcodeproj`, never run xcodegen/xcodebuild.
- Navigation only through the scaffold Router: `@Environment(Router.self) private var router`,
  then `router.show(.<case>)` for a `ScreenID` case that EXISTS in
  `{APP_DIR}/App/Navigation/ScreenID.swift`, `router.dismiss()` to go back and
  `router.finishOnboarding()` to leave onboarding. No NavigationStack, NavigationLink
  (destination:), TabView or .sheet of your own (paged carousels: ScrollView +
  `.scrollTargetBehavior(.paging)`). The tab bar is drawn by the app shell
  (`AppTabBar`), NEVER by a screen.
- Headless screen-id mode (`Headless.isActive`): the app is launched straight onto one screen
  for a screenshot. Every screen MUST render completely on first frame from `Fixtures` data,
  with no user interaction, no network, no auto-navigation, no timers firing navigation and no
  capture service started.
- System prompts: the scaffold already generated these prompters: {available}. Ask only from a
  user action with `await Permissions.request(<Name>.self)`; never call a system permission API
  yourself and never call `.prompt()`. Capture APIs that prompt implicitly (AVAudioEngine
  inputNode, AVAudioRecorder, AVCaptureSession, CMPedometer, CBCentralManager, ...) are only
  allowed in a file named `*{SERVICE_SUFFIX}` that returns early when `Headless.isActive` and
  starts capturing only after `Permissions.request(...)` returned true. Never use
  `@Environment(\\.requestReview)`.
- Real behaviour (tone playback, level metering, ...) uses Apple frameworks and starts from user
  actions; in headless mode screens show their fixture state instead.
{module_rules}\
- Bundled fonts: `Font.custom("<postscript_name>", size:)` with names from `fonts.json`
  (files with `is_system: false`); system fonts otherwise. Bundled media:
  `MediaAsset.image("<file>")` / `MediaAsset.url("<file>")` where `<file>` is the file name of
  a `media.json` entry's `path` (without the `media/` prefix).
- NO ADS, everywhere and always (the clone earns through subscriptions only): the screenshots
  and view trees still show the original's ads — never reproduce any of it. No ad SDKs, no
  banner / native / interstitial / rewarded slots, no "Loading ads…" overlays or spinners, no
  "may contain ads" / "Sponsored" / "Ad" labels, no "watch an ad" or "remove ads" offers; delete
  the ad and let the remaining content reflow (no empty strip, no reserved gap). Subscription
  paywalls and "Get Pro" upsells are not ads and stay. IOSForge rejects ad code and ad text.
- `reference/` holds a minimal working example of this architecture (read-only).
- Code without inline `//` comments; short `///` doc comments on types are fine.
"""


DIVERGENCE_NOTE = """CONTENT DIVERGENCE: keep the structure and layout of the original, but
paraphrase every user-visible string, use SF Symbols instead of copying the original's
icons, and never use the original app's name, logo or branding.
"""


def _divergence(enabled: bool) -> str:
    return DIVERGENCE_NOTE if enabled else ""


def theme_prompt(
    *,
    app_name: str,
    prompters: list[str],
    modules: list[str] | None = None,
    diverge_content: bool = True,
) -> str:
    """Theme task: design tokens → ``App/Theme/`` Swift (colours, fonts, styles)."""
    return f"""{base_rules(prompters, modules)}
{_divergence(diverge_content)}
TASK: theme for "{app_name}".
Read `app_spec.json` → `design_tokens` (W3C tokens: color, font, dimension, gradient,
button_style, elevation_style, dark_mode, ios_adaptation) and `fonts.json`. Replace
`{APP_DIR}/App/Theme/Theme.swift` (you may add more files under `{APP_DIR}/App/Theme/`) with:
- `extension Color` palette from the colour tokens (hex initialiser included in your files);
- `enum AppFont` helpers mapping heading/body/etc. tokens to bundled fonts by postscript name;
- spacing / corner-radius constants from dimension tokens;
- `LinearGradient` values from gradient tokens;
- `ButtonStyle`s from `button_style` and shadow modifiers from `elevation_style`.
Keep a type named `Theme` (it may become a namespace enum). Write ONLY under
`{APP_DIR}/App/Theme/`. The project must compile.
"""


def components_prompt(
    plan: NavPlan,
    *,
    prompters: list[str],
    modules: list[str] | None = None,
    diverge_content: bool = True,
) -> str:
    """Component-library task: shared views + the single ``AppTabBar`` + ``COMPONENTS.md``."""
    if plan.shows_tab_bar:
        tabs = "\n".join(
            f"  - `.{t.case_name}` — {t.title!r} (root screen `{t.root.screen_id}`, "
            f"see `screens/{t.root.screen_id}.png`)"
            for t in plan.tabs
        )
        tab_bar = f"""- Replace `{APP_DIR}/App/Components/AppTabBar.swift`: draw the app's tab
  bar exactly as the tab-root screenshots show it (shape, selected-state styling, icon + label,
  safe area). Keep the signature
  `struct AppTabBar: View {{ let tabs: [AppTab]; @Binding var selection: AppTab }}`;
  it only renders `tabs` and assigns `selection` — no Router, no navigation, no screen content.
  Pick an SF Symbol per tab with a `switch` over the `AppTab` cases. `tab.title` is the
  original's label and only identifies the tab: show your own paraphrased label per case
  (content divergence), never `tab.title` verbatim. Tabs:
{tabs}"""
    else:
        tab_bar = (
            f"- The app has no tab bar: leave `{APP_DIR}/App/Components/AppTabBar.swift` unchanged."
        )
    return f"""{base_rules(prompters, modules)}
{_divergence(diverge_content)}
TASK: component library (runs after the theme, before any screen).
Read `app_spec.json` → `screens[].components` across ALL screens, the screenshots in `screens/`,
`source/*.json` and the theme in `{APP_DIR}/App/Theme/`. Then:
- Write reusable SwiftUI views under `{APP_DIR}/App/Components/` for every visual element that
  repeats across screens (cards, buttons, list rows, headers, toggles, chips, gauges, ...).
  They are purely visual: data comes in through init parameters, actions through closures. No
  Router, no Fixtures, no navigation, no system prompts.
{tab_bar}
- Write `{COMPONENTS_MD}` in the workspace root (NOT inside `{APP_DIR}/`): one entry per
  component with its name, init signature, what it renders and which screens use it. Screen
  tasks build from this list.
Write ONLY under `{APP_DIR}/App/Components/` plus `{COMPONENTS_MD}`. The project must compile.
"""


def _placement(entry: ScreenEntry, tabs: dict[str, TabEntry], shows_bar: bool) -> str:
    tab = tabs.get(entry.tab_root or "")
    if entry.presentation == "tabRoot":
        if shows_bar and tab:
            return (
                f"This screen is the root of the {tab.title!r} tab. The app shell renders the tab "
                "bar below it — do NOT draw a tab bar or reserve space for one."
            )
        return "This screen is the app's home (root of its only navigation stack)."
    if entry.presentation == "push" and tab:
        return (
            f"This screen is pushed onto the {tab.title!r} tab's navigation stack. Use the system "
            "back button, or hide it (`.navigationBarBackButtonHidden()`) and call "
            "`router.dismiss()` from a custom one if the original has one."
        )
    if entry.presentation in ("sheet", "fullScreenCover"):
        return (
            f"This screen is presented modally ({entry.presentation}) over the current tab. "
            "Close it with `router.dismiss()`."
        )
    return (
        "This is an onboarding screen shown full-screen outside the tabs. Move on with "
        "`router.show(.<next onboarding case>)` or `router.finishOnboarding()` from a user action "
        "or a timer that never runs when `Headless.isActive`."
    )


def screen_prompt(
    entry: ScreenEntry,
    plan: NavPlan,
    *,
    targets: list[ScreenEntry],
    prompters: list[str],
    modules: list[str] | None = None,
    observed: bool = True,
    diverge_content: bool = True,
    images: list[tuple[str, str]] | None = None,
) -> str:
    """Screen task: one ``app_spec`` screen → its view, fixtures and sub-views.

    ``observed`` is False for screens the analysis inferred (no screenshot / view tree).
    """
    if targets:
        nav = "\n".join(f"  - `.{t.case_name}` — {t.name} ({t.presentation})" for t in targets)
        nav_block = f"Navigation targets available from this screen (use router.show):\n{nav}"
    else:
        nav_block = (
            "No navigation target from this screen exists in this build: controls that would "
            "navigate elsewhere render normally but their action is a no-op."
        )
    tabs = {t.root.screen_id: t for t in plan.tabs}
    image_block = ""
    if images:
        listed = "\n".join(f'  - `MediaAsset.image("{file}")` — {desc}' for file, desc in images)
        image_block = (
            "- Replacement images made for this clone (use them where the original shows these "
            f"photos/illustrations; never recreate the original's pictures):\n{listed}\n"
        )
    if observed:
        reference = f"""- `screens/{entry.screen_id}.png` — screenshot of the original screen.
  Match its layout, hierarchy, proportions, colours and typography closely (ignore its tab bar:
  the shell draws it).
- `source/{entry.screen_id}.json` — the original native view tree (frames in points,
  `screen_size`, fonts, colours, `asset_ref.media_id`). Use it for exact geometry, spacing and
  font sizes; skip ad containers and the tab bar."""
    else:
        reference = """- There is NO screenshot and NO native view tree for this screen: the
  analysis inferred it from the rest of the app. Design it from its app_spec entry so it looks
  like a sibling of the observed screens (same theme, components, spacing, header style)."""
    return f"""{base_rules(prompters, modules)}
{_divergence(diverge_content)}
TASK: implement screen `{entry.screen_id}` — "{entry.name}".
{_placement(entry, tabs, plan.shows_tab_bar)}

INPUTS (ground truth, read them all):
- `app_spec.json` → the `screens[]` entry with id `{entry.screen_id}`: components, layout_notes,
  states (render the "default" state), dynamic_content.
{reference}
- `{COMPONENTS_MD}` and `{APP_DIR}/App/Components/` — build the screen from these components;
  add only screen-specific sub-views.
- The theme in `{APP_DIR}/App/Theme/` — use its colours, fonts and styles.
{image_block}
OUTPUT (write ONLY these paths; anything else is discarded):
- Replace `{APP_DIR}/{entry.view_path}` with the real screen. Keep `struct {entry.type_name}: View`
  with no initialiser parameters. Sub-views, view models and `*{SERVICE_SUFFIX}` files go under
  `{APP_DIR}/App/Features/{entry.screen_id}/`.
- Put the data the screen shows in `{APP_DIR}/{entry.fixtures_path}` as
  `extension Fixtures {{ ... }}` with names prefixed by the screen (realistic values, no lorem
  ipsum) and read it from the view.
- {nav_block}
"""


def compile_fix_prompt(
    errors: list[str], *, prompters: list[str], modules: list[str] | None = None
) -> str:
    """Compile-gate fix task for the given xcodebuild / contract / permission-lint errors."""
    listed = "\n".join(errors)
    return f"""{base_rules(prompters, modules)}
TASK: the project in `{APP_DIR}/` does not build or breaks the scaffold contract. Below are the
errors from xcodebuild, the IOSForge contract check and the permission lint. Fix ONLY these
errors with the smallest edits: imports, types, missing members, typos, misplaced files.
Permission-lint errors: route the prompt through one of the generated prompters with
`Permissions.request(...)`, or move capture code into a `*{SERVICE_SUFFIX}` that checks
`Headless.isActive`. Unexpected-path errors: move the code into an allowed folder
(`App/Theme`, `App/Components`, `App/Features/<id>`, `App/Fixtures`). Do NOT change layout,
colours, fonts or navigation, and never edit scaffold files ({PERMISSIONS_DIR}/ included).

errors:
{listed}
"""


def _corrective_task_text(task: dict[str, object], entry: ScreenEntry) -> str:
    kind = str(task.get("type", "fix"))
    if kind == "diverge":
        return (
            "The screen still LOOKS TOO MUCH like the original. Restyle only its appearance "
            "with the app theme and components (palette, gradients, fonts, card/button shapes, "
            "icons, paraphrased copy); keep the same blocks in the same positions."
        )
    if kind in ("add_screen", "fix_blank"):
        return (
            "The screen renders blank or failed to render. Implement it fully so it draws its "
            f"content on the first frame in headless mode (`-screen-id {entry.screen_id}`)."
        )
    if kind == "add_edge":
        via = task.get("via_element") or "the matching control"
        target = f"`router.show(.{task.get('to_case')})` (screen {task.get('to')})"
        return f"Wire navigation: {via} on this screen must call {target}. Keep the layout."
    raw = task.get("diffs")
    diffs = [str(d) for d in raw] if isinstance(raw, list) else []
    lines = "\n".join(f"- {d}" for d in diffs) or "- close the layout gap"
    return (
        "Correct the LAYOUT (block placement / order / hierarchy) so it matches the original; "
        f"keep the divergent design. Differences to fix:\n{lines}"
    )


def corrective_prompt(
    tasks: list[dict[str, object]],
    entry: ScreenEntry,
    *,
    prompters: list[str],
    modules: list[str] | None = None,
) -> str:
    """Vision-Judge corrective task for one screen (all of its fix tasks combined)."""
    body = "\n\n".join(_corrective_task_text(task, entry) for task in tasks)
    return f"""{base_rules(prompters, modules)}
TASK: correct screen `{entry.screen_id}` — "{entry.name}" after the Vision Judge review.
Compare (LOOK at both): `screens/{entry.screen_id}.png` — the ORIGINAL target, and
`generated_screens/{entry.screen_id}.png` — the CURRENT render of this app.

{body}

Write ONLY `{APP_DIR}/{entry.view_path}`, other files under
`{APP_DIR}/App/Features/{entry.screen_id}/` and `{APP_DIR}/{entry.fixtures_path}`; anything else
is discarded. Keep the screen compiling and rendering fully on the first frame.
"""


def rework_prompt(
    instructions: str, *, prompters: list[str], modules: list[str] | None = None
) -> str:
    """Operator rework round over the generated app (post-MVP feature/fix iteration)."""
    return f"""{base_rules(prompters, modules)}
TASK: operator rework round on the existing app in `{APP_DIR}/`. Apply these instructions:

{instructions.strip()}

Edit ONLY model-owned code: `{APP_DIR}/App/Theme/`, `{APP_DIR}/App/Components/`
(+ `{COMPONENTS_MD}`), `{APP_DIR}/App/Features/<screen id>/` and `{APP_DIR}/App/Fixtures/`;
anything else is discarded.
Keep every screen rendering on its first frame in headless mode and keep the app compiling. To
add a NEW screen the operator must extend the scope instead — do not invent screens here.
"""
