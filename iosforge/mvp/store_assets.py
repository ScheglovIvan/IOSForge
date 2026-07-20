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
  `key_elements` that must survive, headline treatment, backdrop, how the device sits,
  `extras` and `depth_devices`. Do NOT open `original/` — contracts are the input.
- `screens.json` + `screens/<id>.png` — OUR app's real screens (`id`, `name`, `purpose`).
- `backgrounds/NN.jpg` — a stock photo already fetched for slides whose contract asks
  for a photographic backdrop. Use it when present; otherwise build the backdrop in CSS.
- `tokens.json` — OUR palette, gradients and typefaces. The listing must look like OUR
  app, never like the source.

Write `slides/NN.html`, one per contract, same numbering.

HARD REQUIREMENTS
- `body` is exactly {w}px by {h}px, `overflow:hidden`, no margins. Nothing may be clipped
  unintentionally, and the page must not scroll.
- Self-contained: inline `<style>` only. Reference local files by relative path
  (`../screens/0006.png`, `../backgrounds/03.jpg`). NO external fonts, CDNs or network
  URLs — they will not load and the slide will render broken.
- Inside any device frame put OUR real screen image. Never draw a fake app UI, and never
  claim a feature our screens do not show.

REPRODUCE THE COMPOSITION, NOT THE SKIN
- Honour the contract's `hero`, every entry in `key_elements`, and its `depth_devices`.
  If it says the UI cards overhang the phone edges, make them overhang. If the phone is
  angled or bleeds off an edge, do that with `transform` / positioning. If there is a
  feature list, a callout ring or a badge, build it.
- CSS can carry most "3D": `perspective`, `rotate3d`, layered shadows, and a gold
  vanishing-point grid via `repeating-linear-gradient` under `transform: perspective()`.
  Use them instead of flat boxes when the contract calls for depth.
- Colours, gradients and type come from `tokens.json` — NOT from the source's palette.

COPY
- Write OUR headline: same benefit, clearly different wording, short and punchy. Never
  reuse the source's phrasing, never copy their typos.
- Badges must be true. This app is new: no install counts, no ratings, no awards, even
  if the contract records them on the source slide. Use a qualitative line or none.

Write ONLY the files under `slides/`. Produce one for EVERY contract — a listing that
drops a slide is a failure.
"""


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


def build_slide_pages(
    workspace: Path,
    tokens: dict[str, Any],
    *,
    size: tuple[int, int] = CANVAS,
    timeout: int = 1800,
) -> list[Path]:
    """Have the agent author one HTML page per contract; return the pages written.

    Authoring beats fixed templates here: the contracts describe angled devices,
    in-context composites, callouts and perspective grids, and enumerating a template
    per treatment would always trail what the analysis can describe.
    """
    from iosforge.mvp.claude_gen import run_task

    bound = log.bind(stage="slide_build", workdir=str(workspace))
    (workspace / "slides").mkdir(parents=True, exist_ok=True)
    (workspace / "tokens.json").write_text(
        json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    prompt = BUILD_PROMPT.format(w=size[0], h=size[1])
    (workspace / "BUILD_PROMPT.md").write_text(prompt, encoding="utf-8")
    bound.info("store_assets.building")
    run_task(workspace, prompt, timeout=timeout, tlog=bound)

    pages = sorted((workspace / "slides").glob("*.html"))
    bound.info("store_assets.built", pages=len(pages))
    return pages


def _chromium() -> str:
    from iosforge.mvp.compliance import _discover_chromium

    return _discover_chromium()


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

    from iosforge.mvp.compliance import _snap_stage_dir

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
