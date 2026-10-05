"""App icon generation: Apple's file rules and the anti-clone prompt."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from iosforge.mvp import app_icon

_PALETTE = {"bg_start": "#B80E74", "bg_end": "#E54629", "accent": "#DC0C5B"}
_SPEC: dict[str, Any] = {
    "app_name": "Sounds for CarPlay",
    "purpose": "assign custom sounds to CarPlay connect and disconnect events",
}


def _image(path: Path, width: int, height: int, *, alpha: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "rgba" if alpha else "rgb24"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=magenta@0.5:s={width}x{height}",
            "-frames:v",
            "1",
            "-pix_fmt",
            fmt,
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    return path


def test_icon_is_the_size_app_store_connect_requires() -> None:
    assert app_icon.ICON_SIZE == 1024


def test_prompt_bans_the_shapes_that_would_make_it_a_clone() -> None:
    p = " ".join(app_icon.icon_prompt(_SPEC, _PALETTE).split())
    assert "Do not trace, mirror or recolour the reference's symbol" in p
    assert "No lettering, no words, no numbers" in p
    assert "no brand logos" in p


def test_prompt_keeps_the_genre_so_it_fits_the_category() -> None:
    p = " ".join(app_icon.icon_prompt(_SPEC, _PALETTE).split())
    assert "KEEP the reference's genre cues" in p


def test_prompt_states_apples_form_rules() -> None:
    # each of these is a rejection or a visible defect on device, not a preference
    p = " ".join(app_icon.icon_prompt(_SPEC, _PALETTE).split())
    assert "SQUARE corners" in p and "double-rounded" in p
    assert "Completely opaque" in p
    assert "fills the ENTIRE square, edge to edge" in p


def test_prompt_carries_the_app_identity_and_our_palette() -> None:
    p = app_icon.icon_prompt(_SPEC, _PALETTE)
    assert "Sounds for CarPlay" in p
    assert "CarPlay connect and disconnect" in p
    assert "#B80E74" in p and "#E54629" in p and "#DC0C5B" in p


def test_prompt_survives_a_spec_without_a_purpose() -> None:
    p = app_icon.icon_prompt({"app_name": "Thing"}, _PALETTE)
    assert "see the reference icon" in p


def test_normalise_forces_the_square_size(tmp_path: Path) -> None:
    src = _image(tmp_path / "wide.png", 1600, 900)
    out = app_icon.normalise(src, tmp_path / "icon.png")
    from iosforge.mvp.image_slides import png_size

    assert png_size(out.read_bytes()) == (1024, 1024)


def test_normalise_strips_alpha_because_transparency_is_rejected(tmp_path: Path) -> None:
    src = _image(tmp_path / "alpha.png", 512, 512, alpha=True)
    assert app_icon.has_alpha(src) is True
    out = app_icon.normalise(src, tmp_path / "opaque.png")
    assert app_icon.has_alpha(out) is False


def test_source_icon_url_reads_the_ingested_listing() -> None:
    assert app_icon.source_icon_url({"artwork_url": "https://x/i.png"}) == "https://x/i.png"
    assert app_icon.source_icon_url({}) == ""


def test_missing_source_icon_is_reported_not_raised(tmp_path: Path) -> None:
    # no artwork recorded must not fail the job: the spec still describes the app
    assert app_icon.fetch_source_icon("", tmp_path / "none.png") is None


def test_normalise_squares_off_rounded_corners(tmp_path: Path) -> None:
    # the model draws rounded corners however firmly the prompt forbids it, and iOS
    # masks the icon itself — a pre-rounded icon renders double-rounded on device
    src = tmp_path / "rounded.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=600x600",
            "-vf",
            "format=rgb24,geq=r='if(lt(hypot(max(0,60-X),max(0,60-Y)),60),220,255)'"
            ":g='if(lt(hypot(max(0,60-X),max(0,60-Y)),60),20,255)'"
            ":b='if(lt(hypot(max(0,60-X),max(0,60-Y)),60),120,255)'",
            "-frames:v",
            "1",
            str(src),
        ],
        check=False,
        capture_output=True,
    )
    out = app_icon.normalise(src, tmp_path / "squared.png")
    corner = _corner_pixel(out)
    # the pale corner wedge must be gone: what survives is the artwork colour
    assert corner != (255, 255, 255)


def _corner_pixel(path: Path) -> tuple[int, int, int]:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            "crop=1:1:0:0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=False,
        capture_output=True,
    ).stdout
    return (raw[0], raw[1], raw[2]) if len(raw) >= 3 else (0, 0, 0)


_APP_COLORS = """
class AppColors {
  static const Color primary = Color(0xFF0F766E);
  static const Color accentTeal = Color(0xFF0E7490);
  static const Color accentOrange = Color(0xFF0D9488);
  static const Color background = Color(0xFFFFFFFF);
}
"""


def test_palette_comes_from_the_app_not_the_spec() -> None:
    # observed live: a spec saying #FF0B00 red for an app that ships teal. The icon
    # is the app's face, so the compiled theme wins.
    p = app_icon.palette_from_app(_APP_COLORS)
    assert p["accent"] == "#0F766E"
    assert p["bg_end"] == "#0E7490"
    assert p["bg_start"] == "#0D9488"


def test_palette_from_app_is_empty_when_colours_are_absent() -> None:
    # an unreadable theme must fall back to the spec's tokens, not to a wrong colour
    assert app_icon.palette_from_app("class Foo {}") == {}


def test_purpose_reads_spec_v2_field_names() -> None:
    # spec v2 calls it one_liner; reading only "purpose" sent the model nothing
    assert app_icon.app_purpose({"one_liner": "clean your speaker"}) == "clean your speaker"
    assert app_icon.app_purpose({"description": "fallback text"}) == "fallback text"
    assert app_icon.app_purpose({}) == "see the reference icon"
