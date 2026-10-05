"""Image-model slide engine: prompt rules, canvas fitting and token loading."""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.mvp import image_slides as nb

_PALETTE = {
    "bg_start": "#B80E74",
    "bg_end": "#E54629",
    "accent": "#DC0C5B",
    "font": "Poppins",
}


def _contract(index: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "index": index,
        "sells": "you can design the dashboard inside your own car",
        "device": {"treatment": "in_context"},
        "key_elements": ["navigation tile", "weather tile", "music tile"],
    }
    base.update(over)
    return base


def _png(path: Path, width: int, height: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"gradients=s={width}x{height}:c0=magenta:c1=orange",
            "-frames:v",
            "1",
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    return path


def test_canvas_is_the_app_store_size() -> None:
    assert nb.CANVAS == (1290, 2796)


def test_aspect_is_the_tallest_the_model_offers() -> None:
    # the model's enum has no 1290x2796 ratio; 9:16 is the closest portrait it emits,
    # which is why fit_to_canvas has to make up the remaining height
    assert nb.ASPECT == "9:16"


def test_prompt_forbids_the_defects_seen_on_the_first_run() -> None:
    p = " ".join(nb.slide_prompt(_contract(2), _PALETTE).split())
    assert "no install counts" in p and "no star ratings" in p
    assert "no laurel wreaths" in p
    assert "no car maker logos" in p and "no real people" in p
    assert "never reproduce a typo it contains" in p


def test_prompt_carries_our_palette_as_names() -> None:
    # as names, not codes — see test_palette_is_described_in_words_never_as_hex
    p = nb.slide_prompt(_contract(2), _PALETTE)
    assert nb.colour_name(_PALETTE["bg_start"]) in p
    assert nb.colour_name(_PALETTE["bg_end"]) in p


def test_prompt_never_names_the_typeface() -> None:
    # given the token's font name the model LETTERED it onto the slide — "Poppins"
    # appeared as the headline. The typeface is described, never named.
    p = nb.slide_prompt(_contract(2), _PALETTE)
    assert _PALETTE.get("font", "Poppins") not in p
    assert "heavy geometric sans" in p


def test_text_free_mode_asks_for_a_bare_image() -> None:
    # so a headline can be composited afterwards instead of being drawn by a model
    # that garbles lettering
    p = " ".join(nb.slide_prompt(_contract(2), _PALETTE, text_free=True).split())
    assert "NO TEXT ANYWHERE IN THE IMAGE" in p
    assert "not a single letter or digit" in p
    assert "keep the upper third free of detail" in p
    assert "NO TEXT ANYWHERE" not in nb.slide_prompt(_contract(2), _PALETTE)


def test_prompt_restyles_inside_the_phone_not_only_the_backdrop() -> None:
    p = " ".join(nb.slide_prompt(_contract(2), _PALETTE).split())
    assert "INSIDE the phone screen too, not only to the backdrop" in p


def test_prompt_reserves_the_edges_for_the_canvas_fit() -> None:
    # fit_to_canvas stretches the top and bottom strips, so content there would smear
    p = " ".join(nb.slide_prompt(_contract(2), _PALETTE).split())
    assert "nothing important within roughly 8% of either edge" in p


def test_prompt_adapts_a_square_plate_instead_of_letterboxing_it() -> None:
    p = " ".join(nb.slide_prompt(_contract(1, device={"treatment": "none"}), _PALETTE).split())
    assert "NO phone in it" in p
    assert "Do not letterbox it" in p


def test_prompt_keeps_the_device_where_the_source_had_it() -> None:
    p = nb.slide_prompt(_contract(4, device={"treatment": "cropped"}), _PALETTE)
    assert "'cropped'" in p


def test_fit_to_canvas_grows_a_short_frame_to_the_exact_size(tmp_path: Path) -> None:
    src = _png(tmp_path / "src.png", 900, 1600)
    out = nb.fit_to_canvas(src, tmp_path / "out.png")
    assert nb.png_size(out.read_bytes()) == nb.CANVAS


def test_fit_to_canvas_crops_a_frame_taller_than_the_canvas(tmp_path: Path) -> None:
    src = _png(tmp_path / "tall.png", 900, 3000)
    out = nb.fit_to_canvas(src, tmp_path / "tall_out.png")
    assert nb.png_size(out.read_bytes()) == nb.CANVAS


def test_png_size_reads_the_header() -> None:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1290, 2796)
    assert nb.png_size(header) == (1290, 2796)


def test_load_token_reads_the_secrets_file(tmp_path: Path) -> None:
    secret = tmp_path / "replicate.env"
    secret.write_text('REPLICATE_API_TOKEN="r8_example"\n')
    settings = Settings(replicate_secrets_path=str(secret))
    assert nb.load_token(settings) == "r8_example"
    assert nb.is_configured(settings) is True


def test_missing_token_is_reported_not_raised(tmp_path: Path) -> None:
    settings = Settings(replicate_secrets_path=str(tmp_path / "absent.env"))
    assert nb.load_token(settings) == ""
    assert nb.is_configured(settings) is False


def test_seedream_renders_the_canvas_directly() -> None:
    # the whole reason for switching engines: no aspect approximation, no ffmpeg
    assert nb.takes_exact_size("bytedance/seedream-4") is True
    assert nb.takes_exact_size("google/nano-banana-pro") is False


def test_payload_asks_a_sized_model_for_exact_pixels() -> None:
    payload = nb._payload(
        "p",
        ["u"],
        model="bytedance/seedream-4",
        resolution="4K",
        aspect="9:16",
        size=nb.CANVAS,
    )
    assert payload["size"] == "custom"
    assert (payload["width"], payload["height"]) == nb.CANVAS
    assert "aspect_ratio" not in payload
    # the prompt already carries the palette and the rules; rewriting it loses them
    assert payload["enhance_prompt"] is False


def test_payload_falls_back_to_a_stock_ratio_otherwise() -> None:
    payload = nb._payload(
        "p",
        ["u"],
        model="google/nano-banana-pro",
        resolution="4K",
        aspect="9:16",
        size=nb.CANVAS,
    )
    assert payload["aspect_ratio"] == "9:16"
    assert payload["resolution"] == "4K"
    assert "width" not in payload and "size" not in payload


def test_operator_notes_go_last_so_they_win() -> None:
    # a re-run must answer the feedback, not re-roll the same dice
    base = nb.slide_prompt(_contract(3), _PALETTE)
    steered = nb.slide_prompt(_contract(3), _PALETTE, notes="make the phone larger")
    assert "make the phone larger" in steered
    assert steered.index("OPERATOR CORRECTIONS") > steered.index("CONTENT RULES")
    assert "OPERATOR CORRECTIONS" not in base


def test_palette_is_described_in_words_never_as_hex() -> None:
    # observed live: given "#B80E74" the model PRINTED "#B80E7" onto the slide as a
    # caption. A hex code is text to render; a colour name is an instruction.
    p = " ".join(nb.slide_prompt(_contract(2), _PALETTE).split())
    assert "#" not in p
    assert "vivid magenta" in p and "vivid orange-red" in p
    assert "no hex codes, no swatches" in p


def test_colour_name_describes_the_hue_not_the_code() -> None:
    assert nb.colour_name("#B80E74") == "vivid magenta"
    assert nb.colour_name("#E54629") == "vivid orange-red"
    assert nb.colour_name("#0F766E") == "deep vivid teal"


def test_colour_name_handles_neutrals_and_junk() -> None:
    assert nb.colour_name("#000000") == "near-black"
    assert nb.colour_name("#FFFFFF") == "white"
    assert nb.colour_name("#808080") == "neutral grey"
    # an unparsable value must pass through rather than crash a generation
    assert nb.colour_name("not-a-colour") == "not-a-colour"


def test_prompt_points_the_model_at_our_icon() -> None:
    # the model copies the icon it can see in the source screenshot, colours and
    # all — it has to be told which image carries OUR mark
    p = " ".join(nb.slide_prompt(_contract(1), _PALETTE, has_icon=True).split())
    assert "SECOND IMAGE IS OUR APP ICON" in p
    assert "Never the icon from the first image" in p
    assert "never a recoloured version of it" in p


def test_prompt_says_nothing_about_an_icon_when_none_is_supplied() -> None:
    p = nb.slide_prompt(_contract(1), _PALETTE)
    assert "SECOND IMAGE" not in p


def test_prompt_numbers_the_source_so_references_are_unambiguous() -> None:
    p = " ".join(nb.slide_prompt(_contract(1), _PALETTE, has_icon=True).split())
    assert "Redraw the FIRST image" in p


def test_edit_mode_is_a_retouch_not_a_redraw() -> None:
    # once a slide is close, redrawing throws away what already works and re-rolls
    # the parts nobody complained about
    p = " ".join(
        nb.slide_prompt(
            _contract(2), _PALETTE, notes="remove the side panels", editing=True
        ).split()
    )
    assert "FINISHED App Store slide of ours" in p
    assert "targeted retouch" in p
    assert "Everything else must come back unchanged" in p
    assert "remove the side panels" in p


def test_edit_mode_inventories_before_touching_anything() -> None:
    # asked to remove "the panels beside the car", the model deleted the headline and
    # the badge too — naming what must survive is what stops that
    p = " ".join(nb.slide_prompt(_contract(2), _PALETTE, notes="x", editing=True).split())
    assert "account for what is already in the frame" in p
    assert "Removing an element nobody asked about is a failure" in p
    # it also shrank the artwork and left empty margins
    assert "Do not re-crop" in p and "add empty margins" in p


def test_edit_mode_protects_the_text_it_cannot_re_render() -> None:
    # the model garbles lettering on every full redraw ("Ponnics", "Heavmervy"), so
    # an edit must be told not to touch type at all
    p = " ".join(
        nb.slide_prompt(_contract(2), _PALETTE, notes="fix the bottom", editing=True).split()
    )
    assert "TEXT IS OFF LIMITS" in p
    assert "re-letter a single word" in p
    assert "including any spelling it already has" in p


def test_edit_mode_drops_the_house_style_brief() -> None:
    # restating the full style rules would invite reinterpretation of untouched parts
    p = nb.slide_prompt(_contract(2), _PALETTE, notes="x", editing=True)
    assert "CONTENT RULES" not in p
    assert "no install counts" not in p


def test_ipad_canvas_is_the_slot_app_store_connect_names() -> None:
    # APP_IPAD_PRO_3GEN_129, confirmed against the live API's list of valid types
    assert nb.IPAD_CANVAS == (2048, 2732)


def test_ipad_prompt_preserves_the_decisions_already_made() -> None:
    # the wording and palette were settled on the phone slide; re-deriving them
    # would reopen choices that were made deliberately
    p = " ".join(nb.ipad_prompt(swap_device=True).split())
    assert "the headline, word for word" in p
    assert "It is the SAME slide" in p
    assert "not a new design" in p
    assert "Do not re-letter existing text" in p


def test_ipad_prompt_swaps_the_device_when_one_is_pictured() -> None:
    swapped = " ".join(nb.ipad_prompt(swap_device=True).split())
    assert "Replace it with an iPad" in swapped
    assert "not as an enlarged phone" in swapped
    kept = " ".join(nb.ipad_prompt(swap_device=False).split())
    assert "no phone mockup to change" in kept
    assert "Replace it with an iPad" not in kept


def test_ipad_prompt_forbids_stretching_to_fill() -> None:
    # the canvas goes from 0.46 to 0.75 wide-to-tall; scaling would distort everything
    p = " ".join(nb.ipad_prompt(swap_device=True).split())
    assert "Nothing may be squashed or stretched" in p
    assert "No letterboxing" in p


def _ffmpeg_image(codec: str, colour: str) -> bytes:
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={colour}:s=32x32",
        "-frames:v",
        "1",
    ]
    args += ["-f", "mjpeg", "-"] if codec == "jpeg" else ["-f", "image2", "-c:v", "png", "-"]
    return subprocess.run(args, capture_output=True, check=False).stdout


def test_jpeg_output_is_transcoded_to_a_real_png() -> None:
    # the model returns JPEG even when the payload asks for PNG; storing those bytes
    # under a .png name produced slides App Store Connect refused to display
    jpeg = _ffmpeg_image("jpeg", "red")
    assert not nb.is_png(jpeg)
    assert nb.is_png(nb.to_png(jpeg))


def test_png_output_is_left_alone() -> None:
    png = _ffmpeg_image("png", "blue")
    assert nb.to_png(png) is png


def test_transcoded_slide_keeps_its_pixel_size() -> None:
    # a slot rejects the wrong dimensions just as firmly as the wrong format
    jpeg = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=1290x2796",
            "-frames:v",
            "1",
            "-f",
            "mjpeg",
            "-",
        ],
        capture_output=True,
        check=False,
    ).stdout
    assert nb.png_size(nb.to_png(jpeg)) == (1290, 2796)


def test_unreadable_bytes_are_returned_rather_than_raising() -> None:
    assert nb.to_png(b"not an image") == b"not an image"
