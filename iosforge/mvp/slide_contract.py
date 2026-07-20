"""Per-slide contracts for the App Store listing — analysis split from rendering.

The listing planner used to read the source screenshots and emit finished slides in
one pass. Judging composition and writing copy at the same time made it average the
set: it looked closely at the first slide, then produced generic variants and quietly
dropped the rest, so a listing of eight became five and lost the things that carried
each shot (a prop, a callout, a feature list).

So the work is split, and a contract sits between the halves:

1. :func:`analyse` studies **every** source slide on its own and writes one contract
   per slide — what it sells, how the headline is set, the backdrop, how the device
   sits, and the furniture that must not be lost.
2. :func:`verify_analysis` refuses to continue unless a contract exists for each.
3. Rendering then works **from contracts only** and never reopens the images, so our
   design system decides appearance while the contract decides structure.
4. :func:`missing_slides` compares what was analysed against what was produced, so a
   listing that silently shrinks is an error rather than a surprise.

Contracts describe the source's *composition*. Copy, palette and screens are ours.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger

log = get_logger("mvp.slide_contract")

CONTRACT_NAME = "{index:02d}.json"


class IncompleteAnalysisError(RuntimeError):
    """A source slide has no contract — rendering must not start."""


ANALYSE_PROMPT = """\
You are reverse-engineering an App Store listing's ART DIRECTION, slide by slide.

`original/NN.png` holds the source app's store screenshots in order.
Analyse EVERY file. Do not summarise the set, do not describe two slides together,
do not skip one because it looks similar to another — each gets its own contract.

For each `original/NN.png` write `contracts/NN.json`:

{
  "index": 1,
  "sells": "the single benefit this slide communicates",
  "hero": "the ONE element the slide is built around, e.g. 'app UI composited into the
           car's dashboard screen, roughly 45% of the canvas'",
  "key_elements": ["everything that must survive, most important first"],
  "headline": {
    "lines": ["exact line 1", "exact line 2"],
    "emphasis_line": 2,
    "emphasis_style": "pill" | "outline" | "color" | "none",
    "position": "top" | "middle" | "bottom",
    "align": "center" | "left",
    "size_ratio": 0.10,
    "weight": 800,
    "colors": ["#FFFFFF", "#0B0B0F"]
  },
  "background": {
    "kind": "photo" | "gradient" | "solid" | "texture",
    "tone": "dark" | "light",
    "description": "what the photo/texture shows, in a few words",
    "colors": ["#0A0A0A", "#123"]
  },
  "device": {
    "present": true,
    "treatment": "centered" | "angled" | "cropped" | "in_context" | "none",
    "angle_deg": 0,
    "width_ratio": 0.72,
    "notes": "e.g. 'tilted ~20 degrees, bleeding off the right edge'"
  },
  "extras": [
    {"kind": "badge" | "bullets" | "callout" | "prop" | "logo" | "none",
     "position": "top" | "middle" | "bottom",
     "content": "visible text, if any",
     "description": "e.g. 'laurel wreath around 1M+ Users and five stars'"}
  ],
  "depth_devices": ["how the slide creates depth: UI cards lifted past the frame,
                    layered 3D props, drop shadows, magnifier cutout, ..."]
}

Rules:
- Record what you SEE, verbatim for text. This is analysis; invent nothing here.
- `key_elements` is the anti-loss list. If a slide is built on a map, a chart, a
  player, a carousel, a bottom sheet or a floating button, say so explicitly.
- Measure proportionally (fractions of canvas width/height), never in pixels.
- Reproduce their typos in `headline.lines` — this is a record of their slide.
- One file per slide, named to match its source file. Write nothing else.
"""


def analyse(workspace: Path, *, timeout: int = 900) -> list[Path]:
    """Run the per-slide analysis in ``workspace`` and return the contracts written."""
    from iosforge.mvp.claude_gen import run_task

    bound = log.bind(stage="slide_analysis", workdir=str(workspace))
    (workspace / "contracts").mkdir(parents=True, exist_ok=True)
    (workspace / "ANALYSE_PROMPT.md").write_text(ANALYSE_PROMPT, encoding="utf-8")
    bound.info("slide_contract.analysing")
    run_task(workspace, ANALYSE_PROMPT, timeout=timeout, tlog=bound)
    written = sorted((workspace / "contracts").glob("*.json"))
    bound.info("slide_contract.analysed", count=len(written))
    return written


def source_indices(originals_dir: Path) -> list[int]:
    """Indices of the source slides found on disk, in order."""
    indices: list[int] = []
    for png in sorted(originals_dir.glob("*.png")):
        try:
            indices.append(int(png.stem))
        except ValueError:
            continue
    return indices


def verify_analysis(originals_dir: Path, contracts_dir: Path) -> list[dict[str, Any]]:
    """Load one contract per source slide, or raise :class:`IncompleteAnalysisError`."""
    indices = source_indices(originals_dir)
    if not indices:
        raise IncompleteAnalysisError("Не найдено ни одного экрана оригинала для анализа.")

    contracts: list[dict[str, Any]] = []
    missing: list[str] = []
    for index in indices:
        path = contracts_dir / CONTRACT_NAME.format(index=index)
        if not path.is_file():
            missing.append(f"{index:02d}")
            continue
        try:
            payload = json.loads(path.read_text())
        except ValueError:
            missing.append(f"{index:02d} (битый JSON)")
            continue
        if not isinstance(payload, dict) or not payload.get("sells"):
            missing.append(f"{index:02d} (пустой контракт)")
            continue
        payload.setdefault("index", index)
        contracts.append(payload)

    if missing:
        raise IncompleteAnalysisError(
            "Не все экраны были проанализированы. "
            f"Проанализировано {len(contracts)} из {len(indices)}; "
            f"нет контракта для: {', '.join(missing)}"
        )
    log.info("slide_contract.verified", slides=len(contracts))
    return contracts


def missing_slides(contracts: list[dict[str, Any]], rendered: list[Path]) -> list[str]:
    """Contracted slides that never made it into the rendered listing."""
    produced = {p.stem for p in rendered}
    return [
        f"{int(c.get('index', 0)):02d}"
        for c in contracts
        if f"{int(c.get('index', 0)):02d}" not in produced
    ]
