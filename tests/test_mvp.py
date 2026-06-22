"""Smoke tests for the MVP vertical — pure logic on the critical path.

No emulator / no Claude CLI here (those are live integration, exercised by a real run).
Covers: uiautomator XML -> screen elements, bounds math, run layout, claude workspace prep.
"""

from __future__ import annotations

from pathlib import Path

from iosforge.mvp import claude_gen, crawl
from iosforge.mvp.paths import RunPaths

_SAMPLE_UI_XML = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node text="Play" resource-id="com.x:id/play" clickable="true" bounds="[0,0][100,50]"/>
  <node text="" resource-id="com.x:id/menu" clickable="true" bounds="[200,0][300,50]"/>
  <node text="label" resource-id="com.x:id/lbl" clickable="false" bounds="[0,60][100,90]"/>
</hierarchy>
"""


def test_clickables_extracts_only_clickable_nodes_with_bounds() -> None:
    els = crawl._clickables(_SAMPLE_UI_XML)
    assert len(els) == 2  # the non-clickable label is excluded
    assert {e["resource_id"] for e in els} == {"com.x:id/play", "com.x:id/menu"}
    assert els[0]["text"] == "Play"


def test_center_parses_bounds() -> None:
    assert crawl._center("[0,0][100,50]") == (50, 25)
    assert crawl._center("not-bounds") is None


def test_signature_is_stable_and_order_independent() -> None:
    a = crawl._clickables(_SAMPLE_UI_XML)
    b = list(reversed(a))
    assert crawl._signature(a) == crawl._signature(b)


def test_run_paths_layout(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    assert rp.screens_dir.is_dir()
    assert rp.screens_dir == rp.run_dir / "screens"
    assert rp.screens_json == rp.run_dir / "screens.json"
    assert rp.flutter_app == rp.run_dir / "flutter_app"


def test_prepare_workspace_copies_inputs_and_prompt(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    (rp.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.screens_json.write_text('{"package":"com.x","screens":[]}')

    claude_gen._prepare_workspace(rp)

    assert (rp.claude_ws / "screens" / "0000.png").exists()
    assert (rp.claude_ws / "screens.json").read_text().startswith('{"package"')
    assert "Flutter app" in (rp.claude_ws / "PROMPT.md").read_text()
