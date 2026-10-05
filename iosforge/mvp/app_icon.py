"""Generate the app's own icon from the source app's icon.

The clone needs an icon that reads as the same *kind* of app — a listing full of
sound apps looks the way it does for a reason — while being unmistakably ours:
our palette, our mark, none of the source's shapes or lettering.

Apple's rules decide the file, not taste (App Store Connect rejects otherwise):

* exactly 1024x1024 for the App Store icon;
* **no alpha channel** — a transparent icon is rejected outright;
* **square, unrounded** — iOS applies the superellipse mask itself, so baking a
  rounded corner in produces a double-rounded icon on device;
* full bleed: the artwork must reach every edge, no padding, no drop shadow.

The image model is told all of that, and :func:`normalise` enforces the ones a
prompt cannot guarantee — size and the absence of alpha.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger
from iosforge.mvp import image_slides

log = get_logger("mvp.app_icon")

ICON_SIZE = 1024
ICON_NAME = "app_icon.png"

PROMPT = """\
Design an iOS app icon for a DIFFERENT app, using the attached icon only as a
reference for what KIND of app this is.

The app: {name}. What it does: {purpose}

KEEP the reference's genre cues — a listing of similar apps shares a visual
language, and this icon should sit in it naturally: a comparable subject, a
comparable level of detail, the same read at small size.

CHANGE everything that identifies the source:
- Colours come from OUR palette: gradient from {bg_start} to {bg_end}, accent {accent}.
- The mark itself must be a DIFFERENT shape. Do not trace, mirror or recolour the
  reference's symbol; design our own around the same idea.
- No lettering, no words, no numbers, no source app name.
- Nothing recognisable from the real world: no brand logos, no product likenesses.

FORM — these are Apple's rules and they are not negotiable:
- One centred subject, generous scale, instantly readable as a 60px icon.
- Artwork fills the ENTIRE square, edge to edge. No padding, no border, no margin.
- SQUARE corners. Do NOT draw rounded corners — iOS masks the icon itself, and a
  pre-rounded icon renders double-rounded on device.
- Completely opaque. No transparency anywhere, no drop shadow outside the square.
- Flat or softly shaded. No photographic backdrop, no text, no UI screenshot.
"""


def source_icon_url(metadata: dict[str, Any]) -> str:
    """The source app's artwork URL recorded when the listing was ingested."""
    return str(metadata.get("artwork_url") or "")


def fetch_source_icon(url: str, out: Path, *, timeout: float = 60.0) -> Path | None:
    """Download the source icon to seed the model; ``None`` when unavailable."""
    if not url:
        return None
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("app_icon.source_fetch_failed", url=url, error=str(exc))
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    return out


def app_purpose(spec: dict[str, Any]) -> str:
    """One line describing what the app does, from whichever field carries it.

    App Spec v2 names these ``one_liner`` and ``description``; older specs used
    ``purpose``/``summary``. Reading only one shape silently sent the model an
    empty purpose, which it answered with a generic mark.
    """
    for field in ("one_liner", "purpose", "summary", "description"):
        value = str(spec.get(field) or "").strip()
        if value:
            return value[:220]
    return "see the reference icon"


def icon_prompt(spec: dict[str, Any], palette: dict[str, str]) -> str:
    """Compose the icon instruction from the app spec and our design tokens."""
    return PROMPT.format(
        name=str(spec.get("app_name") or spec.get("name") or "this app"),
        purpose=app_purpose(spec),
        bg_start=palette.get("bg_start", "#1B1B2F"),
        bg_end=palette.get("bg_end", "#3A1C4A"),
        accent=palette.get("accent", "#DC0C5B"),
    )


_DART_COLOR = re.compile(r"static\s+const\s+Color\s+(\w+)\s*=\s*Color\(0x[fF]{2}([0-9a-fA-F]{6})\)")

_PALETTE_FIELDS = {
    "primary": "accent",
    "accentTeal": "bg_end",
    "accentOrange": "bg_start",
}


def palette_from_app(source: str) -> dict[str, str]:
    """Read the palette out of the app's own colour definitions.

    The icon is the app's face, so its colours must be the ones the app actually
    ships — which are not always the ones in the spec. Codegen applies its own
    anti-clone divergence, and the two have been observed to disagree completely
    (a spec saying red for an app that ships teal). Whatever the app compiled is
    the truth for the icon.

    Returns an empty dict when the file does not name the expected colours, so
    the caller can fall back to the spec's tokens.
    """
    found = {name: f"#{value.upper()}" for name, value in _DART_COLOR.findall(source)}
    palette = {key: found[name] for name, key in _PALETTE_FIELDS.items() if name in found}
    if "accent" not in palette:
        return {}
    palette.setdefault("bg_start", palette["accent"])
    palette.setdefault("bg_end", palette["accent"])
    return palette


def normalise(src: Path, dst: Path, *, size: int = ICON_SIZE, trim: float = 0.06) -> Path:
    """Force the exact square size, drop any alpha channel and square the corners.

    Three guarantees the prompt cannot make. App Store Connect rejects an icon
    carrying transparency, and the model emits one whatever it was told. It also
    draws rounded corners however firmly it is asked not to — and iOS applies its
    own mask, so a pre-rounded icon renders double-rounded with pale wedges in the
    corners on device.

    Squaring is done by zooming ``trim`` past the frame and cropping back: the
    rounded corner and the sliver of backdrop outside it fall off the edge, and
    what remains is full-bleed art with hard corners. The prompt's "one centred
    subject, generous scale" is what makes that safe to cut.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    zoom = max(1.0, 1.0 + trim * 2)
    inner = round(size * zoom)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vf",
            f"scale={inner}:{inner}:flags=lanczos,"
            f"crop={size}:{size}:(iw-{size})/2:(ih-{size})/2,format=rgb24",
            "-pix_fmt",
            "rgb24",
            str(dst),
        ],
        check=False,
        capture_output=True,
    )
    return dst


def has_alpha(path: Path) -> bool:
    """Whether the PNG declares an alpha channel (colour type 4 or 6)."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    return data[25] in (4, 6)


def generate(
    spec: dict[str, Any],
    metadata: dict[str, Any],
    workspace: Path,
    *,
    settings: Settings,
    palette: dict[str, str],
) -> Path:
    """Generate the app icon and return the normalised 1024x1024 PNG.

    The source icon is passed as a reference when the listing recorded one; without
    it the model works from the prompt alone rather than failing, since the spec
    already describes what the app is.
    """
    token = image_slides.load_token(settings)
    if not token:
        raise image_slides.ReplicateError("no Replicate token configured")

    references: list[str] = []
    source = fetch_source_icon(source_icon_url(metadata), workspace / "source_icon.png")
    if source is not None:
        references.append(image_slides.upload_image(source, token=token))
    else:
        log.warning("app_icon.no_source_reference")

    data = image_slides.generate_image(
        icon_prompt(spec, palette),
        references,
        token=token,
        model=settings.replicate_model,
        resolution=settings.replicate_resolution,
        timeout=settings.replicate_timeout_s,
        aspect="1:1",
    )
    raw = workspace / "icon_raw.png"
    raw.write_bytes(data)
    icon = normalise(raw, workspace / ICON_NAME)
    log.info(
        "app_icon.generated",
        bytes=icon.stat().st_size,
        alpha=has_alpha(icon),
        size=image_slides.png_size(icon.read_bytes()),
    )
    return icon
