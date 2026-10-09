"""Render App Store marketing screenshots for the generated app.

These are designed selling shots — the format every app on the store actually uses:
a headline, a styled background and a device mockup, not a bare screen grab. The
palette, gradient and typeface come from the clone's own divergent
``design_tokens`` (see :mod:`iosforge.mvp.design`), so the store page matches the
app and looks nothing like the source listing.

What sits *inside* the device frame is a real screen of the generated app. Apple
rejects listings whose screenshots advertise things the app does not do
(guideline 2.3.3), and since we own the app there is no reason to fabricate UI —
everything around the frame is free design space.

Rendering is deterministic: an HTML page per slide, screenshotted headless by
Chromium at the exact canvas size. No image model, no per-run drift.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

from iosforge.common.logging import get_logger

log = get_logger("mvp.store_assets")

_PEXELS_SEARCH = "https://api.pexels.com/v1/search"

# 6.7"/6.9" iPhone canvas. App Store Connect accepts 1320x2868, 1290x2796 and
# 1260x2736 for this display class and downsamples for the smaller ones; 1290x2796
# is the size proven to pass review here, so it is the default.
CANVAS = (1290, 2796)

_FALLBACK_BG = ("#1B1B2F", "#3A1C4A")
_FALLBACK_TEXT = "#FFFFFF"

# Reusable decorative assets staged for every build. A laurel branch and a five-star
# row are the badge furniture listings lean on; hand-drawing them in CSS looks crude and
# wastes authoring time, so they ship as clean vectors the agent references by path.
# The laurel is ONE left branch — the slide mirrors it (`transform:scaleX(-1)`) for the
# right side. Colour is baked white (what most listings use), because `currentColor`
# does not inherit into an SVG loaded through <img>. Leaves are elongated, spaced and
# paired along a clean arc so it reads as a branch, not a blob.
_LAUREL_STEM = "M62 136 C40 122 27 98 26 70 C25 46 33 24 52 8"
_LAUREL_LEAVES = (
    (48, 122, 60, 22, 6),
    (34, 122, 20, 22, 6),
    (38, 100, 74, 22, 6.5),
    (24, 102, 34, 22, 6.5),
    (30, 76, 86, 23, 7),
    (18, 80, 46, 23, 7),
    (28, 52, 100, 22, 6.5),
    (18, 56, 58, 22, 6.5),
    (34, 30, 112, 20, 6),
    (26, 34, 72, 20, 6),
)
_LAUREL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 144">'
    f'<path d="{_LAUREL_STEM}" fill="none" stroke="#fff" stroke-width="3.5" '
    'stroke-linecap="round"/>'
    '<g fill="#fff">'
    + "".join(
        f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" transform="rotate({rot} {cx} {cy})"/>'
        for cx, cy, rot, rx, ry in _LAUREL_LEAVES
    )
    + "</g></svg>"
)
_STARS_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 260 48" fill="#FFC531">'
    + "".join(
        f'<path transform="translate({i * 52})" d="M24 4l5.9 12 13.1 1.9-9.5 9.2 2.2 13'
        'L24 27.9 12.3 34l2.2-13L5 11.9 18.1 10z"/>'
        for i in range(5)
    )
    + "</svg>"
)


def load_pexels_key(secrets_path: str = "secrets/pexels.env") -> str:
    """Pexels API key from the gitignored secrets file or the environment."""
    path = Path(secrets_path)
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("PEXELS_API_KEY") and "=" in line:
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    return os.environ.get("PEXELS_API_KEY", "")


def fetch_background(query: str, out: Path, *, api_key: str, timeout: float = 25.0) -> Path | None:
    """Download a portrait stock photo for ``query``; ``None`` when unavailable.

    Pexels' licence allows commercial use without attribution, so the photo can ship
    on a store listing as-is.
    """
    if not (query and api_key):
        return None
    try:
        resp = httpx.get(
            _PEXELS_SEARCH,
            params={"query": query, "orientation": "portrait", "per_page": 1},
            headers={"Authorization": api_key},
            timeout=timeout,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos") or []
        if not photos:
            log.warning("store_assets.no_photo", query=query)
            return None
        url = photos[0]["src"]["portrait"]
        data = httpx.get(url, timeout=timeout).content
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("store_assets.photo_failed", query=query, error=str(exc))
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


_STORE_PAGE = "https://apps.apple.com/{country}/app/id{app_id}"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def download_original_slides(
    app_id: str,
    out_dir: Path,
    *,
    country: str = "us",
    limit: int = 8,
    timeout: float = 30.0,
) -> list[Path]:
    """Download the source app's store screenshots, best-effort.

    Apple's iTunes Lookup API returns an empty ``screenshotUrls`` for many listings,
    so the marketing shots are read off the product page instead. They are reference
    material for composition only — nothing from them ships in our listing.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        page = httpx.get(
            _STORE_PAGE.format(country=country, app_id=app_id),
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA},
        )
        page.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("store_assets.page_failed", app_id=app_id, error=str(exc))
        return []

    bases = re.findall(
        r"(https://is\d-ssl\.mzstatic\.com/image/thumb/PurpleSource[^\"\\ ]+?)/\d+x\d+",
        page.text,
    )
    written: list[Path] = []
    for idx, base in enumerate(dict.fromkeys(bases), start=1):
        if len(written) >= limit:
            break
        try:
            img = httpx.get(
                f"{base}/{CANVAS[0]}x{CANVAS[1]}bb.png",
                timeout=timeout,
                headers={"User-Agent": _BROWSER_UA},
            )
            if img.status_code != 200 or len(img.content) < 10_000:
                continue
        except httpx.HTTPError:
            continue
        target = out_dir / f"{idx:02d}.png"
        target.write_bytes(img.content)
        written.append(target)

    log.info("store_assets.original_slides", app_id=app_id, count=len(written))
    return written


def _token(tokens: dict[str, Any], group: str, name: str) -> str | None:
    entry = (tokens.get(group) or {}).get(name)
    if isinstance(entry, dict):
        value = entry.get("$value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def palette(tokens: dict[str, Any]) -> dict[str, str]:
    """Background pair, accent and typeface pulled from the clone's design tokens."""
    start = _token(tokens, "color", "primary_gradient_start") or _token(tokens, "color", "primary")
    end = _token(tokens, "color", "primary_gradient_end") or start
    accent = _token(tokens, "color", "primary") or _FALLBACK_BG[1]
    return {
        "bg_start": start or _FALLBACK_BG[0],
        "bg_end": end or _FALLBACK_BG[1],
        "accent": accent,
        "text": _token(tokens, "color", "on_primary") or _FALLBACK_TEXT,
        "font": _token(tokens, "font", "display") or "Helvetica Neue",
    }


BUILD_PROMPT = """\
You are building the App Store listing for a NEW app as real web pages. One HTML file
per contract; a headless browser screenshots each at exactly {w}x{h}.

Read in this directory:
- `contracts/NN.json` — the analysis of source slide NN: what it sells, its `hero`, the
  `key_elements` inside the phone, headline treatment, backdrop, device handling,
  `extras` and `depth_devices`.
- `original/NN.png` — the source store slide itself. Use it as the visual reference for
  the COMPOSITION and the in-phone CONTENT you are reproducing (layout, which UI blocks,
  what they contain). Never take colour, type or branding from it.
- `screens.json` + `screens/<id>.png` — OUR app's real screens. These are your STYLE
  reference: match their component look (card shape, buttons, list rows, icons, spacing).
  Do NOT embed them as images — they are there to be imitated, not pasted.
- `backgrounds/NN.jpg` — a stock photo pre-fetched for slides whose contract asks for a
  photographic backdrop. Use it when present; otherwise build the backdrop in CSS.
- `props/NN.jpg` — a stock photo of the slide's HEAVY prop (a car, a splash, a 3D object)
  when the contract has a `prop_query`. Composite it; do NOT hand-draw the object.
- `assets/laurel.svg`, `assets/stars.svg` — reusable badge furniture. Use these for any
  laurel wreath or star rating (tint the laurel with `color`); never CSS-draw a wreath.
- `tokens.json` — OUR palette, gradients and typefaces.

Write `slides/NN.html`, one per contract, same numbering.

WHAT TO BUILD vs WHAT TO PLACE AS AN IMAGE
- BUILD in HTML/CSS: the in-phone app UI (below), the headline, the backdrop when it is a
  gradient, simple shapes, and text. These carry our style, so they must be authored.
- PLACE AS AN IMAGE, never hand-drawn: any HEAVY or complex object — a car, a water
  splash, a 3D render (`props/NN.jpg`), a laurel wreath or star row (`assets/laurel.svg`,
  `assets/stars.svg`), a photographic backdrop (`backgrounds/NN.jpg`). Hand-drawing a car
  or a wreath in CSS looks crude and is a defect; reach for the asset instead.

THE IN-PHONE UI IS HTML/CSS — NOT A SCREENSHOT
Do not embed any screenshot. The phone's screen is built as real HTML/CSS elements, just
like the rest of the slide. Store listings are polished mock-ups, not raw captures, so
this is what real listings do.
- CONTENT and LAYOUT of the in-phone UI mirror the source slide's phone (from `original/`
  and the contract's `key_elements`): the same kind of blocks, in the same arrangement —
  if the source shows three sound-slot cards, build three; if it shows a scrolling list
  of tracks with play buttons, build that list; if a dashboard of clock/map/weather
  tiles, build those tiles.
- STYLE comes entirely from OUR design system — `tokens.json` plus the look of
  `screens/<id>.png` (our card radius, our buttons, our accent, our fonts). It must read
  as OUR app, never the source's colours or branding.
- TEXT is OURS. Where our real screens have a matching label or item name, use it; other-
  wise write natural copy in the same spirit. Never reproduce the source's exact strings
  or brand names, and never render the source's logo.
- FILL THE WHOLE SCREEN, top to bottom. The phone screen is a COMPLETE app screen: the
  header/title area, the main content the source shows there (cards, list rows, tiles, a
  map, a player — whatever it is, populated with real items), and the tab bar when the
  source has one. There must be NO bands of bare background inside the frame. Two or three
  thin strips floating over an empty dark screen is the single most common failure here and
  counts as a hard defect — the frame must read as a real, fully populated screen.
- Build it cleanly: no doubled elements, nothing overlapping, no placeholder lorem. It
  should look like a real, populated screen of our app.

DEVICE OR NO DEVICE — read the contract's `device.treatment`
- `treatment` is `none` (icon plates, full-bleed graphics, hero shots): do NOT draw a
  phone at all. Such a source is often SQUARE while our canvas is portrait — adapt it, do
  not letterbox it: put the mark large in the upper half, our headline/tagline beneath it,
  and a backdrop (gradient or the provided photo) covering the ENTIRE canvas edge to edge.
  A small tile floating on empty black is a hard defect; no third of the canvas may sit
  empty. Forcing a phone onto an icon slide is also a hard structural miss.
- Otherwise render the phone using the CANONICAL FRAME below, VERBATIM. Every slide that
  shows a phone must use this exact frame so the device looks identical across the whole
  listing — only its overall size may differ (scale the `.device` width). The `.screen`
  is a container you fill with the built in-phone UI, never an `<img>`.

  <div class="device"><div class="notch"></div>
    <div class="screen"><!-- built UI here, styled from our tokens --></div></div>

  .device{{position:relative;width:62%;aspect-ratio:1179/2556;
    background:#0a0a0a;border-radius:12%/5.8%;padding:1.4%;
    box-shadow:0 4% 9% rgba(0,0,0,.6),inset 0 0 0 1px rgba(255,255,255,.06);}}
  .device .screen{{width:100%;height:100%;overflow:hidden;
    border-radius:10.5%/5%;display:block;}}
  .device .notch{{position:absolute;top:2.2%;left:50%;transform:translateX(-50%);
    width:30%;height:1.6%;background:#0a0a0a;border-radius:999px;z-index:2;}}

GEOMETRY
- `body` is exactly {w}px by {h}px, `overflow:hidden`, no margins, no scrolling.
- The in-phone UI is clipped by `.screen` (`overflow:hidden`), so build it to fill the
  screen naturally; content may run under the bottom edge like a real scrolling screen.
- Everything stays INSIDE the canvas. A prop, wreath or phone may sit partly off one edge
  only if the contract says so; nothing drifts off by accident (no `right:-90px` that
  pushes content out of frame).
- Lay the canvas out as a vertical flow (flex column) so blocks reserve their own space.
  Use absolute positioning only for things that are meant to overlap the backdrop, never
  for headlines, badges or captions — that is how decoration ends up across text.
- Leave breathing room: no element may touch or cross another's text.

COMPOSITION
- Honour the contract's `hero`, its `key_elements` and its `depth_devices` — express them
  in HTML/CSS, in our design system, per the rules above.
- CSS carries most "3D": `perspective`, `rotate3d`, layered shadows, and a vanishing-
  point grid via `repeating-linear-gradient` under `transform: perspective()`.
- Self-contained: inline `<style>` only, and any backdrop photo by relative path
  (`../backgrounds/03.jpg`). NO external fonts, CDNs or network URLs — they will not load
  and the slide renders broken.
- Colours, gradients and type come from `tokens.json` — NOT from the source's palette.

COPY
- Write OUR headline: same benefit, clearly different wording, short and punchy. Never
  reuse the source's phrasing, never copy their typos.
- Badges must be true. This app is new: no install counts, no ratings, no awards, even
  if the contract records them on the source slide. Use a qualitative line or none.

Write ONLY the files under `slides/`. Produce one for EVERY contract — a listing that
drops a slide is a failure.
"""


BUILD_ONE_PROMPT = (
    "Author EXACTLY ONE slide: write `slides/{index:02d}.html` for contract "
    "`contracts/{index:02d}.json`. Do not touch any other slide. Follow every rule "
    "below.\n\n" + BUILD_PROMPT
)


def stage_assets(workspace: Path) -> None:
    """Write the reusable decorative vectors into the workspace for the agent to use."""
    assets = workspace / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "laurel.svg").write_text(_LAUREL_SVG, encoding="utf-8")
    (assets / "stars.svg").write_text(_STARS_SVG, encoding="utf-8")


def prefetch_props(
    workspace: Path, contracts: list[dict[str, Any]], *, api_key: str | None = None
) -> int:
    """Fetch a stock photo for each contract's heavy photographic prop (car, splash…).

    A vehicle or 3D object hand-drawn in CSS looks crude and is slow to author, so the
    analysis surfaces a `prop_query` and the object ships as a real photo the agent
    composites, never as CSS shapes.
    """
    key = load_pexels_key() if api_key is None else api_key
    out = workspace / "props"
    fetched = 0
    for contract in contracts:
        query = str(contract.get("prop_query") or "").strip()
        if not query:
            continue
        index = int(contract.get("index") or 0)
        if fetch_background(query, out / f"{index:02d}.jpg", api_key=key):
            fetched += 1
    log.info("store_assets.props", fetched=fetched)
    return fetched


def prefetch_backgrounds(
    workspace: Path, contracts: list[dict[str, Any]], *, api_key: str | None = None
) -> int:
    """Fetch a stock photo for each contract that calls for a photographic backdrop."""
    key = load_pexels_key() if api_key is None else api_key
    out = workspace / "backgrounds"
    fetched = 0
    for contract in contracts:
        background = contract.get("background") or {}
        if str(background.get("kind")) not in {"photo", "texture"}:
            continue
        query = str(background.get("description") or "").strip()
        if not query:
            continue
        index = int(contract.get("index") or 0)
        if fetch_background(query, out / f"{index:02d}.jpg", api_key=key):
            fetched += 1
    log.info("store_assets.backgrounds", fetched=fetched)
    return fetched


def restore_pages(
    workspace: Path, indices: Iterable[int], fetch: Callable[[str], bytes | None]
) -> list[int]:
    """Seed ``workspace/slides`` with pages authored by an earlier run.

    Authoring eight slides takes hours, and the task is ``acks_late``: a worker restart
    re-delivers it and used to re-author everything from zero, so the refine loop never
    got its turn. Pages already on record are replayed and :func:`build_slide_pages`
    skips them; a page the workspace already holds is never replaced.
    """
    slides = workspace / "slides"
    slides.mkdir(parents=True, exist_ok=True)
    restored: list[int] = []
    for index in indices:
        name = f"{index:02d}.html"
        page = slides / name
        if page.is_file():
            continue
        try:
            data = fetch(name)
        except Exception as exc:
            log.warning("store_assets.restore_failed", index=index, error=str(exc))
            continue
        if not data:
            continue
        page.write_bytes(data)
        restored.append(index)
    if restored:
        log.info("store_assets.restored", slides=restored)
    return restored


def build_slide_pages(
    workspace: Path,
    tokens: dict[str, Any],
    *,
    size: tuple[int, int] = CANVAS,
    timeout: int = 1800,
) -> list[Path]:
    """Author one HTML page per contract; return the pages written.

    One agent session authors all pages, then any slide it skipped is authored on its
    own, serially. Authoring is done serially on purpose: concurrent claude CLI sessions
    on a small host starve each other's CPU and most crash, producing far fewer pages
    than they should — slow-but-complete beats fast-but-missing.
    """

    bound = log.bind(stage="slide_build", workdir=str(workspace))
    (workspace / "slides").mkdir(parents=True, exist_ok=True)
    (workspace / "tokens.json").write_text(
        json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    indices = sorted(
        int(p.stem) for p in (workspace / "contracts").glob("*.json") if p.stem.isdigit()
    )
    if not indices:
        return []

    bound.info("store_assets.building", slides=len(indices))
    (workspace / "BUILD_PROMPT.md").write_text(
        BUILD_PROMPT.format(w=size[0], h=size[1]), encoding="utf-8"
    )
    # One session per slide, each bounded by its own timeout (a single session for all
    # pages blew past the timeout and retried the whole task). The CLI sometimes returns
    # having written nothing, so retry a slide a few times until its page exists — a
    # no-op return is cheap to repeat, and this is what makes the run reach 8/8.
    for idx in indices:
        page = workspace / "slides" / f"{idx:02d}.html"
        for attempt in range(1, _BUILD_ATTEMPTS + 1):
            if page.is_file():
                break
            try:
                _author_one_slide(workspace, idx, size, timeout)
            except Exception as exc:
                bound.warning(
                    "store_assets.slide_build_failed", index=idx, attempt=attempt, error=str(exc)
                )
        if not page.is_file():
            bound.warning("store_assets.slide_unrecoverable", index=idx)

    pages = sorted((workspace / "slides").glob("*.html"))
    bound.info("store_assets.built", pages=len(pages))
    return pages


# The CLI occasionally returns without writing the page; a couple of retries per slide
# turns those flaky no-ops into a complete listing.
_BUILD_ATTEMPTS = 3


_SHARED_INPUTS = ("contracts", "screens", "original", "backgrounds", "props", "assets")
_SHARED_FILES = ("tokens.json", "screens.json")


def link_shared_inputs(workspace: Path, sub: Path) -> None:
    """Give ``sub`` its own entries for the shared inputs, without copying bytes.

    These used to be symlinks, and the agent could not read a single one: the CLI
    sandbox allows reads inside the working directory, a symlink resolves outside it,
    and the read is refused. The agent then rightly declined to author a slide blind
    and exited 0 having written nothing — the "flaky no-op" that lost slides every run.
    Hard links are real directory entries, so they resolve inside and cost no space.
    """
    for name in _SHARED_INPUTS:
        src = workspace / name
        if not src.is_dir():
            continue
        shutil.copytree(src, sub / name, copy_function=_link_or_copy, dirs_exist_ok=True)
    for name in _SHARED_FILES:
        src = workspace / name
        if src.is_file():
            _link_or_copy(str(src), str(sub / name))


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _author_one_slide(
    workspace: Path, index: int, size: tuple[int, int], timeout: int
) -> Path | None:
    """Author a single slide in an isolated sub-workspace, then collect its page.

    Concurrent agents must not share a cwd (each writes its own TASK.md), so every
    slide gets its own directory with the shared inputs linked in.
    """
    from iosforge.mvp.claude_gen import run_task, task_tail

    sub = workspace / "build" / f"{index:02d}"
    if sub.exists():
        shutil.rmtree(sub, ignore_errors=True)
    (sub / "slides").mkdir(parents=True, exist_ok=True)
    link_shared_inputs(workspace, sub)

    prompt = BUILD_ONE_PROMPT.format(index=index, w=size[0], h=size[1])
    run_task(sub, prompt, timeout=timeout, tlog=log.bind(stage="slide_build", index=index))

    authored = sub / "slides" / f"{index:02d}.html"
    if not authored.is_file():
        log.warning("store_assets.slide_not_authored", index=index, tail=task_tail(sub))
        return None
    target = workspace / "slides" / f"{index:02d}.html"
    shutil.copy2(authored, target)
    return target


REVIEW_PROMPT = """\
Score how faithfully each of our slides reproduces the COMPOSITION of the source slide
it answers, then say exactly what to change.

Pairs to compare (same number = same slide):
- `original/NN.png` — the source slide.
- `render/NN.png` — ours.

We deliberately differ in palette, wording and branding — ours is skinned in our own
design system. Do NOT penalise different colours, fonts or copy.

CANVAS IS FIXED — NEVER PENALISE IT. Ours is a portrait App Store screenshot canvas whose
size is mandated by Apple; the source slide may be a completely different shape (a square
icon plate, a wide banner). Never score down for the aspect/size difference and never ask
us to resize the canvas — that "fix" would produce an invalid screenshot. Judge instead how
well the source's composition is ADAPTED to our portrait canvas. When the source is only an
app icon or a full-bleed graphic, the correct answer is a real portrait slide (the mark
large, our headline, a backdrop filling the frame); score that adaptation, and penalise
dead empty space rather than the shape.

Score how well ours reproduces the source's STRUCTURE:
- headline placement, how many lines, whether the emphasis treatment matches;
- where the device sits, its size relative to the canvas, how it is cropped or angled;
- the IN-PHONE UI: does it show the same kind of blocks in the same arrangement as the
  source's phone (same number of cards, a matching list, the same tile grid)?
- presence and position of badges, feature lists, callouts, props;
- the depth device (cards lifted past the frame, in-context composite, etc.);
- overall balance and use of space.

Defects that cap a slide at 40 regardless of layout:
- the in-phone screen is empty, ONLY PARTLY FILLED (thin strips of UI with bands of bare
  background between them inside the frame), a flat placeholder, or clearly not the UI the
  source slide shows there;
- more than a third of the canvas is dead empty space carrying nothing;
- the in-phone UI is skinned in the SOURCE's colours/branding instead of ours;
- any element crossing text (a badge, wreath or prop over a headline or caption);
- the device cut off when the source keeps it whole;
- text clipped, overflowing the canvas, or running off an edge;
- any duplicated / doubled-up element;
- a heavy object (car, splash, wreath, 3D shape) crudely hand-drawn in CSS instead of
  placed as the provided image.

Look hardest at the DEPTH compositions — lifted cards, angled devices, in-context
composites, perspective grids. That is where elements collide. Flag precisely: a lifted
card overlapping another card or its own text; a tilted phone whose corner covers a
caption; a prop or grid line crossing a headline; two floating layers overlapping so
their content is unreadable. Give the exact pair and the fix (shift, resize, restack).

Write `review.json`:

{"slides": [
  {"index": 1,
   "score": 0-100,
   "defects": ["specific, actionable: 'laurel wreath overlaps the badge caption'"],
   "fixes": ["what to change in the HTML, concretely"]}
]}

Be strict and concrete. A defect with no actionable fix is useless. Output ONLY
`review.json`.
"""

REFINE_PROMPT = """\
Fix the slides that failed review.

`review.json` scores each slide against the source composition and lists its defects and
fixes. For EVERY slide scoring below {threshold}, edit its `slides/NN.html` so the listed
defects are gone. Leave passing slides untouched.

All the original build rules still hold — in particular:
- everything is HTML/CSS, including the in-phone UI: no screenshot is embedded, the phone
  screen is built elements styled from our design system, its content mirroring the
  source slide's phone;
- if a phone is shown it is the SAME canonical device as the other slides;
- the canvas is a vertical flow; decoration must not cross text; nothing doubled;
- `body` stays exactly {w}px by {h}px with nothing unintentionally cut off.

Edit the existing files in place. Write nothing else.
"""


EDIT_SLIDE_PROMPT = """\
Improve ONE existing slide in place. Do not rewrite it from scratch.

`slides/{name}` is the current HTML for slide {index}; `render/{index:02d}.png` is how it
looks now; `original/{index:02d}.png` is the source slide it reproduces. Our app's real
screens are in `screens/` (STYLE reference — imitate their look, do not embed them), our
palette in `tokens.json`, a stock backdrop (if any) in `backgrounds/`.

Operator request:
{instructions}

Edit `slides/{name}` to satisfy that request while keeping everything else about the
slide. All the original build rules still hold:
- the app UI appears EXACTLY ONCE, always as an `<img>` of a screen or a crop of it —
  never hand-author cards, chips, rows or nav bars;
- never layer anything over a screenshot that already shows the same content;
- if a phone is shown it must be the SAME device as the other slides — a `.device` with a
  dark bezel, ~12% body radius, a notch and a drop shadow (only the size may differ),
  never a hairline outline; if the contract's treatment is `none`, do not draw a phone;
- the canvas is a vertical flow; decoration must never cross text; nothing drifts off the
  edge by accident;
- `body` stays exactly {w}px by {h}px, self-contained inline CSS, local paths only.

Edit the one file in place. Write nothing else.
"""


def edit_slide(
    workspace: Path,
    index: int,
    instructions: str,
    *,
    size: tuple[int, int] = CANVAS,
    timeout: int = 1200,
) -> Path | None:
    """Apply an operator instruction to a single existing slide page, in place."""
    from iosforge.mvp.claude_gen import run_task

    page = workspace / "slides" / f"{index:02d}.html"
    if not page.is_file():
        log.warning("store_assets.edit_missing_page", index=index)
        return None
    bound = log.bind(stage="slide_edit", index=index)
    prompt = EDIT_SLIDE_PROMPT.format(
        name=page.name,
        index=index,
        instructions=instructions.strip() or "Improve the composition and fix any defects.",
        w=size[0],
        h=size[1],
    )
    bound.info("store_assets.editing")
    run_task(workspace, prompt, timeout=timeout, tlog=bound)
    return page


def review_slides(
    workspace: Path, originals_dir: Path, renders: list[Path], *, timeout: int = 900
) -> dict[str, Any]:
    """Score each render against its source slide; returns the parsed review."""
    from iosforge.mvp.claude_gen import run_task

    bound = log.bind(stage="slide_review", workdir=str(workspace))
    render_dir = workspace / "render"
    if render_dir.exists():
        shutil.rmtree(render_dir, ignore_errors=True)
    render_dir.mkdir(parents=True, exist_ok=True)
    for png in renders:
        shutil.copy2(png, render_dir / png.name)

    (workspace / "review.json").unlink(missing_ok=True)
    bound.info("store_assets.reviewing", pairs=len(renders))
    run_task(workspace, REVIEW_PROMPT, timeout=timeout, tlog=bound)

    produced = workspace / "review.json"
    if not produced.is_file():
        bound.warning("store_assets.review_missing")
        return {"slides": []}
    try:
        payload = json.loads(produced.read_text())
    except ValueError:
        bound.warning("store_assets.review_unparsable")
        return {"slides": []}
    return payload if isinstance(payload, dict) else {"slides": []}


def review_scores(review: dict[str, Any]) -> dict[int, int]:
    """Slide index -> score, from a review payload."""
    scores: dict[int, int] = {}
    for entry in review.get("slides", []) or []:
        if not isinstance(entry, dict):
            continue
        index, score = entry.get("index"), entry.get("score", 0)
        try:
            scores[int(index)] = int(score)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return scores


def failing_slides(review: dict[str, Any], threshold: int) -> list[int]:
    """Indices scoring below ``threshold``, worst first."""
    scores = review_scores(review)
    return sorted((i for i, s in scores.items() if s < threshold), key=lambda i: scores[i])


def resumable_slides(review: dict[str, Any], indices: Iterable[int], threshold: int) -> list[int]:
    """Indices worth carrying over from a previous run — those that already passed.

    Resuming everything would preserve the very slides the last review rejected, and
    the authoring prompt is what usually changed between runs. A slide that scored
    below the threshold (or was never scored) is re-authored from its contract.
    """
    scores = review_scores(review)
    return [i for i in indices if scores.get(i, 0) >= threshold]


def refine_slides(
    workspace: Path,
    *,
    threshold: int,
    size: tuple[int, int] = CANVAS,
    timeout: int = 1800,
) -> None:
    """Have the agent repair the slides the review failed, in place."""
    from iosforge.mvp.claude_gen import run_task

    bound = log.bind(stage="slide_refine", workdir=str(workspace))
    prompt = REFINE_PROMPT.format(threshold=threshold, w=size[0], h=size[1])
    bound.info("store_assets.refining")
    run_task(workspace, prompt, timeout=timeout, tlog=bound)


def _snap_stage_dir(chromium_bin: str) -> Path | None:
    """A screenshot staging dir a *snap-confined* Chromium can actually write to.

    Snap Chromium runs under confinement and cannot write ``--screenshot`` to
    ``/tmp`` (it silently writes into its private namespace, so the file never
    appears at the real path). It CAN write under its own snap home. Return that
    dir for a snap binary, or ``None`` for a normal (unconfined) Chromium.
    """
    if "/snap/" not in chromium_bin:
        return None
    stage = Path.home() / "snap" / "chromium" / "common" / "iosforge_render"
    stage.mkdir(parents=True, exist_ok=True)
    return stage


_CHROMIUM_CANDIDATES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")


def _chromium() -> str:
    """Locate a headless-capable Chromium/Chrome binary (slide rendering), or raise."""
    from iosforge.common.config import get_settings

    preferred = get_settings().chromium_bin
    if preferred:
        return preferred
    for name in _CHROMIUM_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for path in ("/snap/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
        if Path(path).is_file():
            return path
    raise RuntimeError("no chromium/chrome binary found for slide rendering")


def render_pages(
    workspace: Path,
    pages: list[Path],
    out_dir: Path,
    *,
    size: tuple[int, int] = CANVAS,
    timeout: float = 90.0,
) -> list[Path]:
    """Screenshot each authored page at ``size``; returns the PNGs written, in order.

    Pages load their screens and backdrops by relative path, so the whole workspace is
    staged where the browser can reach it — snap-confined Chromium cannot read /tmp.
    """
    if not pages:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    binary = _chromium()

    stage = _snap_stage_dir(binary)
    render_pages_list = pages
    if stage:
        staged_root = Path(stage) / "pages"
        if staged_root.exists():
            shutil.rmtree(staged_root, ignore_errors=True)
        shutil.copytree(workspace, staged_root)
        render_pages_list = [staged_root / p.relative_to(workspace) for p in pages]

    written: list[Path] = []
    for page in render_pages_list:
        shot = page.with_suffix(".png")
        subprocess.run(
            [
                binary,
                "--headless=old",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                "--allow-file-access-from-files",
                f"--window-size={size[0]},{size[1]}",
                f"--screenshot={shot}",
                page.as_uri(),
            ],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
        if not shot.is_file():
            log.warning("store_assets.page_render_failed", page=page.name)
            continue
        target = out_dir / f"{page.stem}.png"
        shutil.move(str(shot), target)
        written.append(target)
    log.info("store_assets.rendered", count=len(written), out_dir=str(out_dir))
    return written
