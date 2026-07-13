"""Tests for design divergence (anti-clone design language rewrite)."""

from __future__ import annotations

from iosforge.common.config import Settings
from iosforge.mvp import design, spec_contract


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {"design_divergence": True, "design_font_substitution": True}
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_hex_roundtrip_and_rotation_preserves_lightness() -> None:
    assert design._hex_to_rgb("#3366FF") == (0x33, 0x66, 0xFF)
    assert design._hex_to_rgb("#fff") == (255, 255, 255)
    assert design._hex_to_rgb("#3366FFAA") == (0x33, 0x66, 0xFF)
    assert design._hex_to_rgb("nope") is None

    rotated = design._rotate_hex("#3366FF", design._HUE_SHIFT)
    assert rotated is not None
    assert rotated != "#3366FF"
    assert rotated.startswith("#") and len(rotated) == 7


def test_classify_and_substitute_font_is_different_same_class_deterministic() -> None:
    assert design.classify_font("Roboto Mono") == "mono"
    assert design.classify_font("Playfair Display") == "serif"
    assert design.classify_font("Nunito") == "rounded"
    assert design.classify_font("Poppins") == "geometric_sans"
    assert design.classify_font("Inter") == "humanist_sans"

    sub = design.substitute_font("Inter")
    assert sub != "Inter"
    assert sub in design._FONT_CLASSES["humanist_sans"]
    assert design.substitute_font("Inter") == sub  # deterministic


def test_apply_divergence_rewrites_tokens_and_stashes_source() -> None:
    spec = {
        "app_name": "Todo",
        "design_tokens": {
            "color": {"primary": {"$value": "#3366FF", "$type": "color"}},
            "font": {"body": {"$value": "Inter", "$type": "fontFamily"}},
            "dimension": {"radius_md": {"$value": "8px", "$type": "dimension"}},
            "dark_mode": True,
            "ios_adaptation": ["cupertino"],
        },
    }
    out = design.apply_divergence(spec, settings=_settings())
    dt = out["design_tokens"]

    assert dt["color"]["primary"]["$value"] != "#3366FF"
    assert dt["font"]["body"]["$value"] != "Inter"
    assert dt["dimension"]["radius_md"]["$value"] == "12px"
    assert dt["gradient"]["primary"]["$type"] == "gradient"
    assert len(dt["gradient"]["primary"]["$value"]["stops"]) == 2
    assert dt["button_style"] in design._BUTTON_STYLES
    assert dt["dark_mode"] is True
    source = dt["$extensions"]["com.iosforge.source_tokens"]
    assert source["color"]["primary"]["$value"] == "#3366FF"
    assert source["font"]["body"]["$value"] == "Inter"
    # original spec is untouched (apply returns a new dict)
    assert spec["design_tokens"]["color"]["primary"]["$value"] == "#3366FF"


def test_apply_divergence_noop_when_disabled() -> None:
    spec = {"app_name": "X", "design_tokens": {"color": {"a": {"$value": "#111111"}}}}
    out = design.apply_divergence(spec, settings=_settings(design_divergence=False))
    assert out is spec


def test_font_substitution_can_be_disabled() -> None:
    spec = {
        "app_name": "X",
        "design_tokens": {"font": {"body": {"$value": "Inter", "$type": "fontFamily"}}},
    }
    out = design.apply_divergence(spec, settings=_settings(design_font_substitution=False))
    assert out["design_tokens"]["font"]["body"]["$value"] == "Inter"


def test_divergent_spec_still_passes_contract() -> None:
    from tests.test_mvp_analyze import _VALID_APP_SPEC

    spec = dict(_VALID_APP_SPEC)
    spec["spec_version"] = spec_contract.SPEC_VERSION
    spec["provenance"] = spec_contract.build_provenance(
        generator="t", generated_at="2026-01-01T00:00:00Z", source_crawl_sha256="x", screen_count=2
    )
    validated = spec_contract.validate_spec(spec)
    diverged = design.apply_divergence(validated, settings=_settings())
    revalidated = spec_contract.validate_spec(diverged)
    assert "gradient" in revalidated["design_tokens"]
