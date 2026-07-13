"""Design divergence (Stage B post-processing).

The clone must PRESERVE structure (screen map, block placement, navigation) but
DIVERGE visually so it does not read as a copy of the source app. This module
rewrites ``app_spec.json``'s ``design_tokens`` into a new design language:

- a hue-rotated palette that keeps role names and per-role lightness (so
  contrast/legibility survive) while shifting the overall colour identity;
- first-class ``gradient`` tokens (the source spec has none);
- similar-but-different fonts, substituted within the same typographic class;
- shifted corner radii plus ``button_style`` / ``elevation_style`` knobs.

The transform is fully deterministic (no LLM, no subprocess) so it is cheap,
reproducible and stable across codegen checkpoint resume. The original tokens
are stashed under ``design_tokens['$extensions']['com.iosforge.source_tokens']``
so the compliance divergence metric can measure the distance from the source.
"""

from __future__ import annotations

import colorsys
import hashlib
from copy import deepcopy
from typing import Any

from iosforge.common.config import Settings

_HUE_SHIFT = 0.42
_SATURATION_GAIN = 1.06
_RADIUS_SCALE = 1.5
_BUTTON_STYLES = ("filled", "tonal", "outlined")

_FONT_CLASSES: dict[str, tuple[str, ...]] = {
    "geometric_sans": ("Poppins", "Montserrat", "Urbanist", "Sora", "Manrope"),
    "humanist_sans": ("Inter", "Work Sans", "Mulish", "Nunito Sans", "Source Sans 3"),
    "rounded": ("Nunito", "Quicksand", "Comfortaa", "Varela Round", "Baloo 2"),
    "serif": ("Merriweather", "Lora", "Playfair Display", "PT Serif", "Bitter"),
    "slab": ("Roboto Slab", "Zilla Slab", "Arvo", "Josefin Slab", "Alfa Slab One"),
    "mono": ("JetBrains Mono", "IBM Plex Mono", "Roboto Mono", "Space Mono", "Fira Code"),
    "display": ("Bebas Neue", "Righteous", "Fredoka", "Lexend", "Rubik"),
}

_ROUNDED_MARKERS = ("round", "nunito", "quicksand", "comfortaa", "varela", "baloo", "fredoka")
_SERIF_MARKERS = ("serif", "times", "georgia", "playfair", "merriweather", "lora", "garamond")
_HUMANIST_MARKERS = ("inter", "roboto", "open sans", "segoe", "system", "helvetica", "arial")


def _hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) not in (6, 8):
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, c)):02X}" for c in rgb)


def _rotate_hex(value: str, shift: float) -> str | None:
    """Rotate a colour's hue, keeping lightness (preserves contrast/legibility)."""
    rgb = _hex_to_rgb(value)
    if rgb is None:
        return None
    r, g, b = (c / 255 for c in rgb)
    h, lightness, s = colorsys.rgb_to_hls(r, g, b)
    h = (h + shift) % 1.0
    s = min(1.0, s * _SATURATION_GAIN)
    nr, ng, nb = colorsys.hls_to_rgb(h, lightness, s)
    return _rgb_to_hex((round(nr * 255), round(ng * 255), round(nb * 255)))


def classify_font(family: str) -> str:
    """Bucket a font family into a typographic class for substitution."""
    lower = family.lower()
    if "mono" in lower:
        return "mono"
    if "slab" in lower:
        return "slab"
    if any(m in lower for m in _ROUNDED_MARKERS):
        return "rounded"
    if any(m in lower for m in _SERIF_MARKERS) and "sans" not in lower:
        return "serif"
    if any(m in lower for m in _HUMANIST_MARKERS):
        return "humanist_sans"
    return "geometric_sans"


def substitute_font(family: str, cls: str | None = None) -> str:
    """Return a DIFFERENT family in the same class; deterministic per family."""
    bucket = _FONT_CLASSES.get(cls or classify_font(family), _FONT_CLASSES["geometric_sans"])
    options = [f for f in bucket if f.lower() != family.strip().lower()]
    if not options:
        return bucket[0]
    idx = int(hashlib.sha1(family.strip().lower().encode()).hexdigest(), 16) % len(options)
    return options[idx]


def _color_value(colors: Any, names: tuple[str, ...]) -> str | None:
    if not isinstance(colors, dict):
        return None
    for name in names:
        tok = colors.get(name)
        if isinstance(tok, dict) and isinstance(tok.get("$value"), str):
            return str(tok["$value"])
    for tok in colors.values():
        if isinstance(tok, dict) and isinstance(tok.get("$value"), str):
            return str(tok["$value"])
    return None


def _build_gradients(colors: Any) -> dict[str, Any]:
    primary = _color_value(colors, ("primary", "brand", "accent")) or "#5B5BD6"
    secondary = (
        _color_value(colors, ("secondary", "accent", "tertiary"))
        or _rotate_hex(primary, 0.08)
        or primary
    )
    return {
        "primary": {
            "$type": "gradient",
            "$value": {
                "angle": 135,
                "stops": [
                    {"color": primary, "pos": 0.0},
                    {"color": secondary, "pos": 1.0},
                ],
            },
        }
    }


_RADIUS_MARKERS = ("radius", "corner", "round")


def _diverge_dimensions(dims: dict[str, Any]) -> None:
    """Scale ONLY radius-like dimension tokens (leaves spacing/sizes untouched)."""
    for name, tok in dims.items():
        if not any(m in name.lower() for m in _RADIUS_MARKERS):
            continue
        if not isinstance(tok, dict):
            continue
        raw = tok.get("$value")
        if not isinstance(raw, str) or not raw.endswith("px"):
            continue
        try:
            px = float(raw[:-2])
        except ValueError:
            continue
        tok["$value"] = f"{round(px * _RADIUS_SCALE)}px"


def _button_style(spec: dict[str, Any]) -> str:
    seed = str(spec.get("app_name") or spec.get("package") or "app").strip().lower()
    idx = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(_BUTTON_STYLES)
    return _BUTTON_STYLES[idx]


def apply_divergence(spec: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    """Return ``spec`` with ``design_tokens`` rewritten into a divergent language.

    No-op when ``settings.design_divergence`` is False (reproduces the faithful
    clone). Original tokens are preserved under
    ``design_tokens['$extensions']['com.iosforge.source_tokens']``.
    """
    if not settings.design_divergence:
        return spec
    tokens = spec.get("design_tokens")
    if not isinstance(tokens, dict):
        return spec

    source = deepcopy(tokens)
    new = deepcopy(tokens)

    colors = new.get("color")
    if isinstance(colors, dict):
        for tok in colors.values():
            if isinstance(tok, dict) and isinstance(tok.get("$value"), str):
                rotated = _rotate_hex(tok["$value"], _HUE_SHIFT)
                if rotated is not None:
                    tok["$value"] = rotated

    if settings.design_font_substitution:
        fonts = new.get("font")
        if isinstance(fonts, dict):
            for tok in fonts.values():
                if isinstance(tok, dict) and isinstance(tok.get("$value"), str):
                    tok["$value"] = substitute_font(tok["$value"])

    dims = new.get("dimension")
    if isinstance(dims, dict):
        _diverge_dimensions(dims)

    new["gradient"] = _build_gradients(new.get("color"))
    new["button_style"] = _button_style(spec)
    new["elevation_style"] = "soft"

    extensions = new.get("$extensions")
    if not isinstance(extensions, dict):
        extensions = {}
    extensions["com.iosforge.source_tokens"] = source
    new["$extensions"] = extensions

    result = dict(spec)
    result["design_tokens"] = new
    return result
