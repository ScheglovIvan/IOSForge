"""Smoke tests for the MVP vertical — pure logic on the critical path.

No emulator / no Claude CLI here (those are live integration, exercised by a real run).
Covers: uiautomator XML -> screen elements, bounds math, run layout, claude workspace prep,
and the staged B->C->D entry-point orchestration with the claude subprocess mocked.
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


def test_run_task_keeps_the_cli_transcript_for_a_silent_no_op(tmp_path, monkeypatch) -> None:
    # the CLI sometimes exits 0 writing nothing; without the transcript a refusal and a
    # crash look identical in the logs
    import subprocess as sp

    def fake_run(*args, **kwargs):
        return sp.CompletedProcess(args=[], returncode=0, stdout="wrote nothing", stderr="warn")

    monkeypatch.setattr(claude_gen.subprocess, "run", fake_run)
    log = claude_gen.get_logger("test")
    assert claude_gen.run_task(tmp_path, "do it", timeout=5, tlog=log) == 0
    tail = claude_gen.task_tail(tmp_path)
    assert "wrote nothing" in tail and "warn" in tail


def test_task_tail_is_empty_when_nothing_ran(tmp_path) -> None:

    assert claude_gen.task_tail(tmp_path) == ""
