"""Divergent replacement images for decorative photos the capture could not provide.

Decorative photos and illustrations of the original often live in the compiled asset
catalog, which the Frida capture does not export, so the generated screens would show
placeholders. Copying the original pixels (e.g. cropping the screenshot) is ruled out
by the anti-clone policy; instead each decorative image slot of the app_spec gets a NEW
image generated from its text description and the clone's own palette
(:func:`image_slides.generate_image`, Replicate). Real generation spends money and is
gated by ``Settings.codegen_generate_images``; without it (or when a prediction fails) a
local divergent placeholder (gradient + caption) keeps the pipeline shape identical.
Slots come from the ad-stripped spec the model sees (DECISIONS 2026-10-09 «Phase 4»).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger
from iosforge.mvp import image_slides

log = get_logger("mvp.swiftui_media")

GENERATED_DIR = "generated"
GENERATED_MEDIA_JSON = "generated_media.json"
PLACEHOLDER_SIZE = (1200, 900)

_IMAGE_KIND = re.compile(
    r"\b(image\w*|media|illustrations?|photos?|pictures?|artworks?|graphics?)\b", re.I
)
_DECORATIVE_ROLE = re.compile(r"\b(hero|header|photo|illustration|banner|cover|background)\b", re.I)
_EXCLUDED_ROLE = re.compile(r"\b(icons?|logos?|avatars?|flags?|profile|brand\w*)\b", re.I)


@dataclass(frozen=True)
class ImageSlot:
    """One decorative image of a screen that needs a divergent replacement."""

    screen_id: str
    slot: str
    description: str

    @property
    def file_name(self) -> str:
        return f"{self.screen_id}_{self.slot}.png"


@dataclass(frozen=True)
class GeneratedImage:
    """A replacement image placed into the app bundle (``Media/generated/<file>``)."""

    screen_id: str
    slot: str
    description: str
    file: str
    source: str


def _words(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9]+", " ", str(value or "")).strip()


def image_slots(spec: dict[str, Any]) -> list[ImageSlot]:
    """Decorative image components (photos, illustrations, heroes); icons and logos excluded."""
    slots: list[ImageSlot] = []
    for screen in spec.get("screens", []):
        if not isinstance(screen, dict):
            continue
        seen: set[str] = set()
        for component in screen.get("components", []) or []:
            if not isinstance(component, dict):
                continue
            kind, role = _words(component.get("type")), _words(component.get("role"))
            if not _IMAGE_KIND.search(kind) or _EXCLUDED_ROLE.search(f"{kind} {role}"):
                continue
            if not (_DECORATIVE_ROLE.search(role) or _DECORATIVE_ROLE.search(kind)):
                continue
            slot = re.sub(r"\s+", "_", role.lower()) or "image"
            if slot in seen:
                slot = f"{slot}_{len(seen) + 1}"
            seen.add(slot)
            description = str(component.get("data") or role or kind).strip()
            slots.append(ImageSlot(str(screen.get("id")), slot, description))
    return slots


def _palette(spec: dict[str, Any]) -> list[str]:
    colors = (spec.get("design_tokens") or {}).get("color") or {}
    values = [v.get("$value") for v in colors.values() if isinstance(v, dict)]
    return [str(v) for v in values if isinstance(v, str) and v.startswith("#")][:4]


def generation_prompt(slot: ImageSlot, palette: list[str]) -> str:
    """Text-only prompt: an ORIGINAL image for the slot, in the clone's palette."""
    colors = ", ".join(palette) or "the app's brand colours"
    return (
        f"Original, clean editorial image for a mobile app screen: {slot.description}. "
        f"Use a fresh composition and styling (not a stock look-alike of any existing app), "
        f"soft lighting, colour accents in {colors}, no text, no logos, no UI chrome."
    )


def _hex(value: str) -> tuple[int, int, int]:
    raw = value.lstrip("#")[:6].ljust(6, "0")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def placeholder(slot: ImageSlot, palette: list[str], out: Path) -> Path:
    """Local divergent stand-in: diagonal gradient in the clone's palette plus a caption."""
    first, second = (palette + ["#6C7BFF", "#9AE6D3"])[:2]
    a, b = _hex(first), _hex(second)
    width, height = PLACEHOLDER_SIZE
    img = Image.new("RGB", PLACEHOLDER_SIZE)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(height - 1, 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)),
        )
    draw.text((40, height - 80), slot.description[:70], fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def generate_images(
    app_dir: Path,
    workspace: Path,
    spec: dict[str, Any],
    *,
    settings: Settings,
) -> list[GeneratedImage]:
    """Create one replacement image per decorative slot into ``Resources/Media/generated``.

    Uses Replicate only when ``settings.codegen_generate_images`` is on and a token is
    configured; otherwise writes local placeholders. Records the mapping in
    ``<workspace>/generated_media.json`` for the screen prompts.
    """
    palette = _palette(spec)
    target = app_dir / "Resources" / "Media" / GENERATED_DIR
    spend = settings.codegen_generate_images and image_slides.is_configured(settings)
    token = image_slides.load_token(settings) if spend else ""
    images: list[GeneratedImage] = []
    for slot in image_slots(spec):
        out = target / slot.file_name
        source = "placeholder"
        marker = out.with_suffix(".source")
        if spend and out.exists() and marker.is_file() and marker.read_text() == "replicate":
            source = "cached"
        elif spend:
            try:
                data = image_slides.generate_image(
                    generation_prompt(slot, palette),
                    [],
                    token=token,
                    model=settings.replicate_model,
                    resolution=settings.replicate_resolution,
                    timeout=settings.replicate_timeout_s,
                    aspect="4:3",
                    size=PLACEHOLDER_SIZE,
                )
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(image_slides.to_png(data))
                marker.write_text("replicate")
                source = "replicate"
            except Exception as exc:
                log.warning("swiftui_media.generate_failed", slot=slot.file_name, error=str(exc))
                placeholder(slot, palette, out)
        else:
            placeholder(slot, palette, out)
        images.append(
            GeneratedImage(
                slot.screen_id, slot.slot, slot.description, f"{GENERATED_DIR}/{out.name}", source
            )
        )
    (workspace / GENERATED_MEDIA_JSON).write_text(
        json.dumps([asdict(i) for i in images], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("swiftui_media.done", images=len(images), spend=spend)
    return images


ICON_SIZE = 1024


def placeholder_icon(spec: dict[str, Any], out: Path) -> Path:
    """Opaque 1024 px stand-in app icon (palette gradient + initial) when none was generated."""
    first, second = (_palette(spec) + ["#6C7BFF", "#9AE6D3"])[:2]
    a, b = _hex(first), _hex(second)
    img = Image.new("RGB", (ICON_SIZE, ICON_SIZE))
    draw = ImageDraw.Draw(img)
    for y in range(ICON_SIZE):
        t = y / (ICON_SIZE - 1)
        draw.line(
            [(0, y), (ICON_SIZE, y)], fill=tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
        )
    initial = (str(spec.get("app_name") or "A").strip()[:1] or "A").upper()
    draw.ellipse((312, 312, 712, 712), fill=(255, 255, 255))
    draw.text((480, 470), initial, fill=a)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
