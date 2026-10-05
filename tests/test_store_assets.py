"""App Store listing rendering: canvas, palette, backdrops and the authoring contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iosforge.mvp import store_assets as sa

_TOKENS: dict[str, Any] = {
    "color": {
        "primary": {"$value": "#DC0C5B"},
        "primary_gradient_start": {"$value": "#B80E74"},
        "primary_gradient_end": {"$value": "#E54629"},
    },
    "font": {"display": {"$value": "Poppins"}},
}


def test_canvas_is_an_accepted_app_store_size() -> None:
    # App Store Connect rejects anything outside the 6.7"/6.9" set
    assert sa.CANVAS in {(1290, 2796), (1320, 2868), (1260, 2736)}


def test_palette_reads_the_apps_own_tokens() -> None:
    pal = sa.palette(_TOKENS)
    assert pal["bg_start"] == "#B80E74"
    assert pal["bg_end"] == "#E54629"
    assert pal["font"] == "Poppins"


def test_palette_falls_back_when_tokens_missing() -> None:
    pal = sa.palette({})
    assert pal["bg_start"] and pal["bg_end"] and pal["font"]


def test_build_prompt_pins_the_exact_canvas() -> None:
    prompt = sa.BUILD_PROMPT.format(w=sa.CANVAS[0], h=sa.CANVAS[1])
    assert f"{sa.CANVAS[0]}x{sa.CANVAS[1]}" in prompt
    assert f"exactly {sa.CANVAS[0]}px by {sa.CANVAS[1]}px" in prompt


def test_build_prompt_builds_the_in_phone_ui_not_a_screenshot() -> None:
    # the model changed: the phone's screen is rendered UI in our style, not an embedded
    # screenshot — that is what removes the doubling, banners and PRO badges
    p = sa.BUILD_PROMPT
    assert "THE IN-PHONE UI IS HTML/CSS" in p
    assert "Do not embed any screenshot" in p
    assert "built as real HTML/CSS elements" in p


def test_build_prompt_requires_self_contained_pages() -> None:
    # a CDN font or remote image silently renders broken in headless Chromium
    p = sa.BUILD_PROMPT
    assert "NO external fonts, CDNs or network" in p
    assert "inline `<style>` only" in p


def test_build_prompt_keeps_true_claims() -> None:
    assert "no install counts, no ratings, no awards" in sa.BUILD_PROMPT


def test_build_prompt_reproduces_content_but_skins_it_ours() -> None:
    # content/layout of the in-phone UI mirrors the source; the skin is our design system
    p = sa.BUILD_PROMPT
    assert "CONTENT and LAYOUT of the in-phone UI mirror" in p
    assert "STYLE comes entirely from OUR design system" in p
    assert "TEXT is OURS" in p
    assert "key_elements" in p and "depth_devices" in p
    assert "perspective" in p  # the "3D" the source leans on is CSS-expressible


def test_build_prompt_requires_one_page_per_contract() -> None:
    assert "one per contract" in sa.BUILD_PROMPT
    assert "EVERY contract" in sa.BUILD_PROMPT


def test_prefetch_backgrounds_only_for_photographic_slides(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_fetch(query: str, out: Path, *, api_key: str, timeout: float = 25.0) -> Path | None:
        calls.append(query)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"jpg")
        return out

    original = sa.fetch_background
    sa.fetch_background = fake_fetch  # type: ignore[assignment]
    try:
        fetched = sa.prefetch_backgrounds(
            tmp_path,
            [
                {"index": 1, "background": {"kind": "photo", "description": "car interior"}},
                {"index": 2, "background": {"kind": "gradient", "description": "dark teal"}},
                {"index": 3, "background": {"kind": "photo", "description": ""}},
            ],
            api_key="k",
        )
    finally:
        sa.fetch_background = original  # type: ignore[assignment]

    assert fetched == 1
    assert calls == ["car interior"]  # gradients are CSS; empty queries are skipped
    assert (tmp_path / "backgrounds" / "01.jpg").is_file()


def test_render_pages_is_a_noop_without_pages(tmp_path: Path) -> None:
    assert sa.render_pages(tmp_path, [], tmp_path / "out") == []


def test_build_prompt_uses_our_screens_as_style_not_content() -> None:
    # our real screens guide the LOOK; they must not be pasted in
    p = sa.BUILD_PROMPT
    assert "match their component look" in p
    assert "imitated, not pasted" in p


def test_build_prompt_keeps_branding_ours() -> None:
    # the in-phone UI reproduces the source's structure but never its brand
    p = sa.BUILD_PROMPT
    assert "Never take colour, type or branding from it" in p
    assert "never render the source's logo" in p


def test_build_prompt_constrains_geometry() -> None:
    p = sa.BUILD_PROMPT
    assert "clipped by `.screen`" in p  # in-phone UI is masked by the screen container
    assert "vertical flow" in p
    # decoration positioned absolutely is how wreaths ended up across captions
    assert "never" in p and "headlines, badges or captions" in p


def test_review_prompt_scores_structure_not_skin() -> None:
    p = sa.REVIEW_PROMPT
    assert "Do NOT penalise different colours, fonts or copy" in p
    # the defects that must dominate the score under the HTML/CSS model
    assert "the IN-PHONE UI" in p
    # a blank OR half-filled phone screen is a hard fail
    assert "ONLY PARTLY FILLED" in p and "flat placeholder" in p
    assert "SOURCE's colours/branding instead of ours" in p
    assert "crossing text" in p
    assert "review.json" in p


def test_review_prompt_never_penalises_the_mandated_canvas() -> None:
    # the source's first slides are square 1290x1290 icon plates; scoring ours down for
    # being a portrait App Store canvas produced 36-42 scores and "fixes" that would have
    # made the screenshot invalid.
    p = " ".join(sa.REVIEW_PROMPT.split())  # prompt text wraps; compare on one line
    assert "CANVAS IS FIXED — NEVER PENALISE IT" in p
    assert "never ask us to resize the canvas" in p
    assert "ADAPTED to our portrait canvas" in p


def test_review_scores_parses_and_skips_junk() -> None:
    review = {
        "slides": [
            {"index": 1, "score": 92},
            {"index": 2, "score": 55},
            "not-a-dict",
            {"index": "x", "score": 10},
        ]
    }
    assert sa.review_scores(review) == {1: 92, 2: 55}


def test_failing_slides_returns_worst_first() -> None:
    review = {
        "slides": [{"index": 1, "score": 92}, {"index": 2, "score": 55}, {"index": 3, "score": 70}]
    }
    assert sa.failing_slides(review, 80) == [2, 3]
    assert sa.failing_slides(review, 50) == []


def test_failing_slides_handles_an_empty_review() -> None:
    # an unparsable review must not look like "everything passed"
    assert sa.review_scores({"slides": []}) == {}
    assert sa.failing_slides({"slides": []}, 80) == []


def test_refine_prompt_repeats_the_rules_that_were_violated() -> None:
    p = sa.REFINE_PROMPT.format(threshold=80, w=1290, h=2796)
    assert "below 80" in p
    assert "everything is HTML/CSS, including the in-phone UI" in p
    assert "SAME canonical device" in p
    assert "1290px by 2796px" in p


def test_build_prompt_allows_slides_without_a_device() -> None:
    # icon plates and full-bleed graphics were forced into phones and scored ~40
    p = sa.BUILD_PROMPT
    assert "DEVICE OR NO DEVICE" in p
    assert "do NOT draw a" in p and "phone at all" in p
    flat = " ".join(p.split())  # prompt text wraps; compare on one line
    assert "icon slide is also a hard structural miss" in flat
    # a square source must be ADAPTED to portrait, not letterboxed on empty black
    assert "adapt it, do not letterbox it" in flat
    assert "no third of the canvas may sit empty" in flat


def test_build_prompt_requires_a_fully_populated_phone_screen() -> None:
    # the dominant real defect was "hollow device": two or three thin strips of UI with
    # bare black between them inside the frame (slides 3-6 scored 40-73 on this).
    p = " ".join(sa.BUILD_PROMPT.split())  # prompt text wraps; compare on one line
    assert "FILL THE WHOLE SCREEN" in p
    assert "NO bands of bare background inside the frame" in p
    assert "fully populated screen" in p


def test_build_prompt_requires_a_realistic_device() -> None:
    # hairline outlines over black scored 68-73; the device must read as physical
    p = sa.BUILD_PROMPT
    assert "CANONICAL FRAME" in p
    assert "box-shadow" in p and ".notch{" in p
    # the screen is a container the agent fills, not an <img>
    assert 'class="screen"><!-- built UI here' in p


def test_build_prompt_keeps_everything_inside_the_canvas() -> None:
    # a prop with right:-90px ran off the edge and capped the slide at 40
    assert "Everything stays INSIDE the canvas" in sa.BUILD_PROMPT
    assert "right:-90px" in sa.BUILD_PROMPT


def test_edit_prompt_edits_in_place_not_from_scratch() -> None:
    p = sa.EDIT_SLIDE_PROMPT
    assert "Do not rewrite it from scratch" in p
    assert "Edit the one file in place" in p
    # the invariants that must survive an edit
    assert "EXACTLY ONCE" in p
    assert "SAME device as the other slides" in p


def test_edit_slide_returns_none_when_page_is_absent(tmp_path: Path) -> None:
    (tmp_path / "slides").mkdir()
    assert sa.edit_slide(tmp_path, 3, "fix it") is None


def test_build_prompt_pins_one_canonical_device_frame() -> None:
    # phones differed slide to slide; a verbatim frame makes them identical
    p = sa.BUILD_PROMPT
    assert "CANONICAL FRAME" in p
    assert "VERBATIM" in p
    assert "identical across" in p
    assert ".device{" in p and ".notch{" in p  # the reusable markup is spelled out


def test_build_one_prompt_targets_a_single_slide() -> None:
    p = sa.BUILD_ONE_PROMPT.format(index=3, w=1290, h=2796)
    assert "Author EXACTLY ONE slide" in p
    assert "slides/03.html" in p
    assert "contracts/03.json" in p
    # it still carries the full ruleset so a parallel author obeys every constraint
    assert "THE IN-PHONE UI IS HTML/CSS" in p


def test_build_slide_pages_is_a_noop_without_contracts(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir()
    assert sa.build_slide_pages(tmp_path, {}) == []


def test_author_one_slide_isolates_and_collects(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # shared inputs are symlinked into a per-slide dir; the authored page is collected back
    for name in ("contracts", "screens", "original", "backgrounds"):
        (tmp_path / name).mkdir()
    (tmp_path / "contracts" / "03.json").write_text("{}")
    (tmp_path / "tokens.json").write_text("{}")
    (tmp_path / "slides").mkdir()

    def fake_run(sub, prompt, *, timeout, tlog):  # type: ignore[no-untyped-def]
        # the agent writes into its OWN sub-workspace, never the shared one
        assert (sub / "contracts" / "03.json").read_text() == "{}"  # symlink resolves
        (sub / "slides" / "03.html").write_text("<html>3</html>")
        return 0

    monkeypatch.setattr("iosforge.mvp.claude_gen.run_task", fake_run)
    page = sa._author_one_slide(tmp_path, 3, sa.CANVAS, 60)
    assert page is not None
    assert (tmp_path / "slides" / "03.html").read_text() == "<html>3</html>"


def test_review_prompt_targets_depth_overlaps() -> None:
    # the user's ask: polish where 3D effects and overlapping elements collide
    p = sa.REVIEW_PROMPT
    assert "DEPTH compositions" in p
    assert "That is where elements collide" in p
    assert "restack" in p  # the fix vocabulary the reviewer should use


def test_build_prompt_places_heavy_props_as_images() -> None:
    # cars, splashes and wreaths ship as assets/props, never CSS-drawn
    p = sa.BUILD_PROMPT
    assert "WHAT TO BUILD vs WHAT TO PLACE AS AN IMAGE" in p
    assert "props/NN.jpg" in p
    assert "assets/laurel.svg" in p
    assert "never CSS-draw a wreath" in p


def test_stage_assets_writes_reusable_vectors(tmp_path: Path) -> None:
    sa.stage_assets(tmp_path)
    laurel = (tmp_path / "assets" / "laurel.svg").read_text()
    stars = (tmp_path / "assets" / "stars.svg").read_text()
    # white-baked: currentColor does not inherit into an <img>-loaded SVG
    assert laurel.startswith("<svg") and 'fill="#fff"' in laurel
    assert stars.count("<path") == 5  # five stars


def test_prefetch_props_only_when_contract_names_a_prop(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_fetch(query: str, out: Path, *, api_key: str, timeout: float = 25.0) -> Path | None:
        calls.append(query)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"jpg")
        return out

    original = sa.fetch_background
    sa.fetch_background = fake_fetch  # type: ignore[assignment]
    try:
        fetched = sa.prefetch_props(
            tmp_path,
            [
                {"index": 2, "prop_query": "yellow sports car"},
                {"index": 3, "prop_query": ""},
                {"index": 4},
            ],
            api_key="k",
        )
    finally:
        sa.fetch_background = original  # type: ignore[assignment]

    assert fetched == 1
    assert calls == ["yellow sports car"]
    assert (tmp_path / "props" / "02.jpg").is_file()


def test_restore_pages_repopulates_the_workspace_from_a_previous_run(tmp_path) -> None:
    # authoring 8 slides takes hours; a restart must reuse the pages already stored
    stored = {"01.html": b"<i>one</i>", "03.html": b"<i>three</i>"}
    calls: list[str] = []

    def fetch(name: str) -> bytes | None:
        calls.append(name)
        return stored.get(name)

    assert sa.restore_pages(tmp_path, [1, 2, 3], fetch) == [1, 3]
    assert calls == ["01.html", "02.html", "03.html"]
    assert (tmp_path / "slides" / "01.html").read_bytes() == b"<i>one</i>"
    assert not (tmp_path / "slides" / "02.html").exists()


def test_restore_pages_keeps_a_page_the_workspace_already_has(tmp_path) -> None:
    slides = tmp_path / "slides"
    slides.mkdir(parents=True)
    (slides / "01.html").write_bytes(b"<i>local</i>")
    assert sa.restore_pages(tmp_path, [1], lambda name: b"<i>remote</i>") == []
    assert (slides / "01.html").read_bytes() == b"<i>local</i>"


def test_restore_pages_survives_a_storage_miss(tmp_path) -> None:
    def fetch(name: str) -> bytes | None:
        raise RuntimeError("no such key")

    assert sa.restore_pages(tmp_path, [1, 2], fetch) == []


def test_resumable_slides_only_carries_over_what_already_passed() -> None:
    # resuming a rejected slide would preserve exactly the output the new prompt fixes
    review = {
        "slides": [
            {"index": 1, "score": 92},
            {"index": 2, "score": 79},
            {"index": 3, "score": 80},
        ]
    }
    assert sa.resumable_slides(review, [1, 2, 3, 4], 80) == [1, 3]
    assert sa.resumable_slides({}, [1, 2], 80) == []
