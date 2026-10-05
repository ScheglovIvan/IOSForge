"""Draw App Store listing slides with an image model instead of authoring HTML.

The HTML engine (:mod:`iosforge.mvp.store_assets`) hands an agent the source slide,
our screens and our tokens and has it write a self-contained page. That keeps every
glyph and colour under our control, but costs minutes per slide because the agent
reads full-size screenshots and writes markup step by step.

This engine sends the source slide straight to an image model, which returns a
finished slide in seconds. What it buys in speed it gives up in control: the model
composes pixels, so the text it draws is whatever it decided to draw.

A model that accepts explicit dimensions renders the App Store canvas directly.
Models that only offer stock aspect ratios cannot: 1290x2796 is not among them, and
the closest portrait ratio is 12% too wide. For those, :func:`fit_to_canvas` grows
the background to size afterwards — growing rather than cropping, since the prompt
spends its effort preserving the source composition.
"""

from __future__ import annotations

import base64
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.image_slides")

API = "https://api.replicate.com/v1"
CANVAS = (1290, 2796)
ASPECT = "9:16"

# Models that take explicit width/height, so the canvas needs no post-processing.
SIZED_MODELS = ("bytedance/seedream",)

# The app icon artifact, passed to the model so slides draw OUR mark.
ICON_REF_NAME = "app_icon.png"

_EDGE_STRIP = 6


class ReplicateError(RuntimeError):
    """The image model could not produce a slide."""


def load_token(settings: Settings) -> str:
    """Replicate API token from the gitignored secrets file, or empty when absent."""
    path = Path(settings.replicate_secrets_path)
    if not path.is_file():
        return ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("REPLICATE_API_TOKEN") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def is_configured(settings: Settings) -> bool:
    """Whether a token is on disk, so the caller can fall back to the HTML engine."""
    return bool(load_token(settings))


def upload_image(path: Path, *, token: str, timeout: float = 180.0) -> str:
    """Put a local image on Replicate's file store and return its fetch URL."""
    resp = httpx.post(
        f"{API}/files",
        headers={"Authorization": f"Bearer {token}"},
        files={"content": (path.name, path.read_bytes(), "image/png")},
        timeout=timeout,
    )
    resp.raise_for_status()
    return str(resp.json()["urls"]["get"])


def takes_exact_size(model: str) -> bool:
    """Whether ``model`` renders arbitrary dimensions rather than stock ratios."""
    return model.startswith(SIZED_MODELS)


def _payload(
    prompt: str,
    image_urls: list[str],
    *,
    model: str,
    resolution: str,
    aspect: str,
    size: tuple[int, int],
) -> dict[str, Any]:
    """Model-shaped input: exact pixels when supported, a stock ratio otherwise."""
    common: dict[str, Any] = {
        "prompt": prompt,
        "image_input": image_urls,
        "output_format": "png",
    }
    if takes_exact_size(model):
        width, height = size
        return {
            **common,
            "size": "custom",
            "width": width,
            "height": height,
            # The prompt already carries the palette and the rules; letting the
            # service rewrite it loses those and costs an extra round trip.
            "enhance_prompt": False,
        }
    return {**common, "aspect_ratio": aspect, "resolution": resolution}


def generate_image(
    prompt: str,
    image_urls: list[str],
    *,
    token: str,
    model: str,
    resolution: str,
    timeout: int,
    aspect: str = ASPECT,
    size: tuple[int, int] = CANVAS,
    poll_s: float = 5.0,
) -> bytes:
    """Run one prediction to completion and return the rendered image bytes."""
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "input": _payload(
            prompt, image_urls, model=model, resolution=resolution, aspect=aspect, size=size
        )
    }
    started = httpx.post(
        f"{API}/models/{model}/predictions",
        headers={**headers, "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    ).json()
    prediction_id = started.get("id")
    if not prediction_id:
        raise ReplicateError(f"no prediction started: {str(started)[:200]}")

    deadline = time.monotonic() + timeout
    data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        data = httpx.get(f"{API}/predictions/{prediction_id}", headers=headers, timeout=60).json()
        if data.get("status") in ("succeeded", "failed", "canceled"):
            break
        time.sleep(poll_s)
    if data.get("status") != "succeeded":
        raise ReplicateError(f"{data.get('status')}: {str(data.get('error'))[:200]}")

    output = data.get("output")
    urls = [output] if isinstance(output, str) else list(output or [])
    if not urls:
        raise ReplicateError("prediction succeeded with no image")
    return httpx.get(urls[0], timeout=300).content


def is_png(data: bytes) -> bool:
    """Whether these bytes really are a PNG, whatever the file is called."""
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def to_png(data: bytes) -> bytes:
    """Return the image as a real PNG, transcoding when it is not one already.

    The model returns JPEG even when asked for PNG, and storing those bytes under a
    ``.png`` name produced files App Store Connect rejected: it checks the contents
    against the extension. Transcoding here means every artifact the pipeline
    stores is what its name says it is.
    """
    if is_png(data):
        return data
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "image2", "-c:v", "png", "pipe:1"],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        return result.stdout
    log.warning("image_slides.png_transcode_failed", bytes=len(data))
    return data


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height read from a PNG header."""
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def fit_to_canvas(src: Path, dst: Path, *, size: tuple[int, int] = CANVAS) -> Path:
    """Bring a generated frame to the exact canvas without discarding composition.

    The frame is fitted by width; the height that is still missing is taken from
    stretched top and bottom strips of the frame itself. On the gradient backdrops
    these slides use that continuation is invisible, and nothing the model composed
    is lost. A frame that is already taller than the canvas is cropped instead,
    since there is nothing to grow.
    """
    width, height = size
    source_w, source_h = png_size(src.read_bytes())
    scaled_h = round(source_h * width / source_w)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if scaled_h >= height:
        _ffmpeg(
            [
                "-i",
                str(src),
                "-vf",
                f"scale={width}:-1:flags=lanczos,crop={width}:{height}",
                str(dst),
            ]
        )
        return dst

    pad = height - scaled_h
    top, bottom = pad // 2, pad - pad // 2
    _ffmpeg(
        [
            "-i",
            str(src),
            "-filter_complex",
            f"[0:v]scale={width}:{scaled_h}:flags=lanczos[b];[b]split=3[b1][b2][b3];"
            f"[b2]crop={width}:{_EDGE_STRIP}:0:0,scale={width}:{top}:flags=bilinear[t0];"
            f"[b3]crop={width}:{_EDGE_STRIP}:0:{scaled_h - _EDGE_STRIP},"
            f"scale={width}:{bottom}:flags=bilinear[t1];"
            f"[t0][b1][t1]vstack=inputs=3[out]",
            "-map",
            "[out]",
            str(dst),
        ]
    )
    return dst


# App Store Connect's APP_IPAD_PRO_3GEN_129 slot. A tablet listing needs its own
# artwork: the iPhone canvas is 0.46 wide-to-tall, the iPad one 0.75, so a phone
# slide letterboxed onto it reads as a mistake.
IPAD_CANVAS = (2048, 2732)

IPAD_PROMPT = """\
Re-frame this finished App Store slide for an iPad listing. It is the SAME slide,
laid out for a wider, squarer canvas — not a new design.

KEEP, exactly as they are:
 - the headline, word for word, in the same typeface and colour;
 - every other caption, label and badge that is present;
 - the colour palette and the background treatment;
 - the subject and what the slide is saying.

CHANGE only what the shape demands:
 - Re-balance the composition for the wider canvas. Use the extra width instead of
   padding it: let the background fill it, and give the elements room to breathe
   rather than stretching them.
 - Nothing may be squashed or stretched. Every object keeps its proportions.
 - Fill the canvas edge to edge. No letterboxing, no bars, no empty margins.

{device}

Do not add any new text. Do not re-letter existing text — reproduce every word
exactly as it appears. Do not add ratings, install counts or awards."""

DEVICE_SWAP = """\
DEVICE: the slide shows a phone. Replace it with an iPad — a tablet in the same
position and at the same angle, showing the same interface re-laid-out for a tablet
screen (wider, more room, same content and same wording). It must read as the same
app running on an iPad, not as an enlarged phone."""

DEVICE_KEEP = """\
DEVICE: there is no phone mockup to change here. Keep the subject as it is."""


def ipad_prompt(*, swap_device: bool) -> str:
    """Instruction to re-frame a finished phone slide for the iPad canvas."""
    return IPAD_PROMPT.format(device=DEVICE_SWAP if swap_device else DEVICE_KEEP)


MASK_MODEL = "black-forest-labs/flux-fill-pro"


def build_mask(
    regions: list[tuple[float, float, float, float]], dst: Path, *, size: tuple[int, int] = CANVAS
) -> Path:
    """Draw the black-and-white mask an inpainting model needs.

    White marks what may be repainted, black what must survive untouched — which
    is the guarantee a prompt cannot give. Regions are fractions of the canvas
    (x, y, width, height), so a caller says "the bottom eighth" rather than
    counting pixels.
    """
    width, height = size
    boxes = "".join(
        f",drawbox=x={round(rx * width)}:y={round(ry * height)}"
        f":w={round(rw * width)}:h={round(rh * height)}:color=white:t=fill"
        for rx, ry, rw, rh in regions
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}",
            "-frames:v",
            "1",
            "-vf",
            f"format=rgb24{boxes}",
            str(dst),
        ]
    )
    return dst


def retouch(
    image: Path,
    mask: Path,
    prompt: str,
    *,
    token: str,
    timeout: int,
    poll_s: float = 4.0,
) -> bytes:
    """Repaint only the masked area of ``image``; everything else is preserved.

    A generation model rewrites the whole frame every time and keeps the rest only
    by luck — asked to clear the sides of a slide it also deleted the headline and
    the badge. Masked inpainting makes "change just this" structural.
    """
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "input": {
            "image": _data_uri(image),
            "mask": _data_uri(mask),
            "prompt": prompt,
            "output_format": "png",
        }
    }
    started = httpx.post(
        f"{API}/models/{MASK_MODEL}/predictions",
        headers={**headers, "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    ).json()
    prediction_id = started.get("id")
    if not prediction_id:
        raise ReplicateError(f"no prediction started: {str(started)[:200]}")

    deadline = time.monotonic() + timeout
    data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        data = httpx.get(f"{API}/predictions/{prediction_id}", headers=headers, timeout=60).json()
        if data.get("status") in ("succeeded", "failed", "canceled"):
            break
        time.sleep(poll_s)
    if data.get("status") != "succeeded":
        raise ReplicateError(f"{data.get('status')}: {str(data.get('error'))[:200]}")
    output = data.get("output")
    urls = [output] if isinstance(output, str) else list(output or [])
    if not urls:
        raise ReplicateError("prediction succeeded with no image")
    return httpx.get(urls[0], timeout=300).content


def _data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/png;base64,{encoded}"


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=False, capture_output=True)


_HUE_NAMES = (
    (8, "red"),
    (20, "orange-red"),
    (40, "orange"),
    (52, "amber"),
    (66, "yellow"),
    (95, "lime"),
    (150, "green"),
    (172, "emerald"),
    (190, "teal"),
    (205, "cyan"),
    (240, "blue"),
    (265, "indigo"),
    (290, "violet"),
    (318, "purple"),
    (338, "magenta"),
    (352, "pink"),
)


def colour_name(hex_value: str) -> str:
    """Describe a colour in words, e.g. ``#DC0C5B`` -> "deep vivid pink".

    The model treats a hex code as text to render, not as an instruction: asked
    for `#B80E74` it printed "#B80E7" onto the slide as a caption. Words are
    understood as colour, so the palette is described rather than quoted.
    """
    raw = hex_value.strip().lstrip("#")
    if len(raw) != 6:
        return hex_value
    try:
        red, green, blue = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return hex_value

    high, low = max(red, green, blue), min(red, green, blue)
    span = high - low
    lightness = (high + low) / 2
    if span < 0.08:
        if lightness < 0.2:
            return "near-black"
        if lightness > 0.85:
            return "white"
        return "neutral grey"

    if high == red:
        hue = 60 * (((green - blue) / span) % 6)
    elif high == green:
        hue = 60 * (((blue - red) / span) + 2)
    else:
        hue = 60 * (((red - green) / span) + 4)

    name = next((label for edge, label in _HUE_NAMES if hue < edge), "red")
    saturation = span / (1 - abs(2 * lightness - 1)) if lightness not in (0, 1) else 0
    tone = "deep " if lightness < 0.35 else ("pale " if lightness > 0.72 else "")
    vivid = "vivid " if saturation > 0.65 else ""
    return f"{tone}{vivid}{name}".strip()


STYLE_RULES = """\
STYLE — replace the source's skin entirely with ours:
- Background: a smooth diagonal gradient running from {bg_start_name} into
  {bg_end_name}. Stay in those two hues — do not drift into neighbouring colours.
- Accent: {accent_name}. Surfaces: near-black. Plus white for type. Nothing else.
- Colour is something you PAINT WITH, never something you write: no colour names, no
  hex codes, no swatches, no palette strip anywhere in the image.
- Headline type: a heavy geometric sans, pure white.
- Cards and tiles: generously rounded corners, soft shadows.
- The styling applies INSIDE the phone screen too, not only to the backdrop. The UI
  on the device must read as OUR app — our accent, our card shape, our type. Do not
  keep the source app's colour scheme anywhere.

CONTENT RULES — these override anything visible in the source:
- Keep the COMPOSITION and LAYOUT of the source: same block positions, same device
  placement and angle, same visual hierarchy.
- Write OUR OWN short headline carrying the same benefit in clearly different words.
  Never reuse the source's phrasing and never reproduce a typo it contains.
- Every word must be correctly spelled and a real word, including small UI labels
  inside the phone.
- NO fabricated social proof. This app is brand new: no install counts, no user
  totals, no star ratings, no review counts, no awards, no laurel wreaths.
- NO recognisable real-world brands: no car maker logos or badges, no platform
  wordmarks, no source app logo or name, no real people. Generic objects only.
- Leave a clear band of plain background at the very top and the very bottom —
  nothing important within roughly 8% of either edge.
"""


TEXT_FREE_RULE = """\
NO TEXT ANYWHERE IN THE IMAGE. Not a headline, not a caption, not a label, not a
watermark, not a single letter or digit — not even inside a device screen or on a
badge. The wording is added afterwards by us, so leave the space it will occupy as
clean background: keep the upper third free of detail, with nothing there but the
backdrop.

This is not a licence to leave the canvas empty elsewhere: the subject still fills
the frame the way the source composition does."""


def slide_prompt(
    contract: dict[str, Any],
    palette: dict[str, str],
    *,
    notes: str = "",
    has_icon: bool = False,
    editing: bool = False,
    text_free: bool = False,
) -> str:
    """Compose the instruction for one slide from its contract and our tokens.

    ``notes`` are the operator's corrections for a re-run and go LAST, so they
    override the generic rules above them when the two disagree.
    """
    treatment = str((contract.get("device") or {}).get("treatment") or "none")
    if editing:
        # Editing our own slide: it is already right in most respects, so the brief
        # is a correction, not a redraw. Restating the full house style here would
        # invite the model to reinterpret parts nobody complained about.
        edit_parts = [
            "This image is a FINISHED App Store slide of ours. Your job is a targeted "
            "retouch of the areas named below. Everything else must come back "
            "unchanged.",
            "Before you change anything, account for what is already in the frame: "
            "the headline, any badge or award graphic, the device or dashboard, the "
            "backdrop, and every caption. All of them must still be present "
            "afterwards, in the same position, at the same size, in the same "
            "colours, unless a correction below says otherwise. Removing an element "
            "nobody asked about is a failure, even if the result looks cleaner.",
            "Do not re-compose. Do not re-crop. Do not change the canvas. Do not "
            "shrink the artwork or add empty margins — the composition fills the "
            "frame now and must still fill it.",
            "TEXT IS OFF LIMITS. Do not add, remove, move, resize, restyle or "
            "re-letter a single word. Reproduce all existing lettering exactly as it "
            "is, including any spelling it already has.",
        ]
        if notes.strip():
            edit_parts.append(f"CORRECTIONS:\n{notes.strip()}")
        return "\n\n".join(edit_parts)

    parts = [
        "Redraw the FIRST image — an App Store marketing screenshot — as a slide for a "
        "DIFFERENT app.",
        f"What this slide must sell: {contract.get('sells')}",
    ]
    if contract.get("headline"):
        parts.append(f"Source headline, to be rewritten in our own words: {contract['headline']}")
    elements = contract.get("key_elements") or []
    if elements:
        parts.append("Elements that must survive: " + "; ".join(str(e)[:90] for e in elements[:8]))
    if treatment == "none":
        parts.append(
            "This slide has NO phone in it. The source is a square plate, so adapt it to "
            "a tall portrait canvas: mark large in the upper half, our headline beneath, "
            "backdrop covering the whole canvas edge to edge. Do not letterbox it and do "
            "not leave a third of the canvas empty."
        )
    else:
        parts.append(
            f"Device treatment is '{treatment}' — keep the device where the source puts it."
        )
    parts.append(
        STYLE_RULES.format(
            bg_start_name=colour_name(palette.get("bg_start", "#1B1B2F")),
            bg_end_name=colour_name(palette.get("bg_end", "#3A1C4A")),
            accent_name=colour_name(palette.get("accent", "#DC0C5B")),
        )
    )
    if has_icon:
        parts.append(
            "THE SECOND IMAGE IS OUR APP ICON. Wherever the slide shows the app's own "
            "mark — the icon plate, a badge, a logo lock-up — draw OUR icon from that "
            "second image: its exact artwork, its exact colours. Never the icon from "
            "the first image, and never a recoloured version of it. Keep it square "
            "with softly rounded corners, as an app icon is shown on a listing."
        )
    if text_free:
        parts.append(TEXT_FREE_RULE)
    if notes.strip():
        parts.append(
            "OPERATOR CORRECTIONS — these override anything above that contradicts "
            f"them:\n{notes.strip()}"
        )
    return "\n\n".join(parts)


def render_slide(
    contract: dict[str, Any],
    source: Path,
    out: Path,
    *,
    settings: Settings,
    token: str,
    palette: dict[str, str],
    notes: str = "",
    icon: Path | None = None,
    editing: bool = False,
    text_free: bool = False,
) -> Path:
    """Generate one slide from its source image and store it at the exact canvas.

    ``notes`` lets an operator steer a re-run ("make the phone larger", "warmer
    background") without touching the contract, which stays the record of what the
    source slide contained.
    """
    index = int(contract.get("index") or 0)
    model = settings.replicate_model
    bound = log.bind(stage="image_slides", index=index, model=model)
    references = [upload_image(source, token=token)]
    if icon is not None and icon.is_file():
        references.append(upload_image(icon, token=token))
    bound = bound.bind(refs=len(references))
    data = generate_image(
        slide_prompt(
            contract,
            palette,
            notes=notes,
            has_icon=len(references) > 1,
            editing=editing,
            text_free=text_free,
        ),
        references,
        token=token,
        model=model,
        resolution=settings.replicate_resolution,
        timeout=settings.replicate_timeout_s,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    if takes_exact_size(model):
        # Rendered at the canvas already; only the container may need fixing.
        out.write_bytes(to_png(data))
    else:
        raw = out.parent / f".raw_{index:02d}.png"
        raw.write_bytes(data)
        fit_to_canvas(raw, out)
        raw.unlink(missing_ok=True)
    bound.info("image_slides.slide_done", bytes=out.stat().st_size)
    return out
