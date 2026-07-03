"""Drop non-app frames (ads, iOS home screen) before analysis.

Video recordings inevitably capture full-screen ads and the iPhone springboard
(home/lock/control-centre) alongside real app screens. Feeding those into
:mod:`iosforge.mvp.analyze` pollutes the App Spec with fake screens. This step
classifies every extracted frame with the local Claude CLI (vision) and rewrites
``screens.json`` / the ``screens/`` dir to keep only ``app`` frames; excluded
frames are moved aside and recorded in ``screen_labels.json`` for audit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from iosforge.common.logging import get_logger
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.screen_filter")

CLAUDE_BIN = "claude"
CLASSIFY_TOOLS = "Read Write Glob"
KEEP_LABEL = "app"
AD_LABEL = "ad"
# Genuine junk — dropped as before. Ad frames are preserved (marked), not dropped.
DROP_LABELS = frozenset({"ios_home", "other"})

CLASSIFY_PROMPT = """\
The `screens/` directory holds PNG frames captured from a phone SCREEN RECORDING
of an app. Some frames are NOT app screens and must be flagged so they are
excluded from analysis.

LOOK at every PNG (vision) and classify each into exactly one label:
- "app"      — a screen of the app itself (its real UI).
- "ad"       — an advertisement: full-screen interstitial, rewarded video, a
               banner takeover, an app-store / "install now" promo, sponsored
               overlay. Anything selling a DIFFERENT product than the app.
- "ios_home" — the iPhone system UI, not the app: home screen / springboard with
               app icons, lock screen, Control Centre, notification shade, app
               switcher, Settings.
- "other"    — blank/black frames, pure transitions, unreadable partial frames.

Output ONLY a file `screen_labels.json` in this directory:

{
  "labels": [
    {"file": "0000.png", "label": "app|ad|ios_home|other", "reason": "<short>"}
  ]
}

Rules:
- Include EVERY file in `screens/` exactly once; use its bare filename.
- When genuinely unsure whether a frame is the app, label it "app" (do not drop
  real screens). Only flag ad / ios_home / other when clearly not the app.
Output nothing but `screen_labels.json`.
"""


def _run_claude(prompt: str, workdir: Path, timeout: int) -> None:
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--permission-mode",
        "acceptEdits",
        "--allowed-tools",
        CLASSIFY_TOOLS,
    ]
    res = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout, check=False
    )
    if res.returncode != 0:
        log.error("screen_filter.cli_failed", code=res.returncode, stderr=res.stderr[-2000:])
        raise RuntimeError(f"claude CLI exited {res.returncode}: {res.stderr[-500:]}")


def _classify(paths: RunPaths, timeout: int) -> dict[str, dict[str, str]]:
    """Return {filename: {"label": ..., "reason": ...}} for every screenshot."""
    ws = paths.run_dir / "classify_ws"
    ws_screens = ws / "screens"
    if ws_screens.exists():
        shutil.rmtree(ws_screens)
    shutil.copytree(paths.screens_dir, ws_screens)
    (ws / "CLASSIFY_PROMPT.md").write_text(CLASSIFY_PROMPT)

    _run_claude(CLASSIFY_PROMPT, ws, timeout)

    produced = ws / "screen_labels.json"
    if not produced.exists():
        raise RuntimeError("Claude did not produce screen_labels.json")
    payload = json.loads(produced.read_text())
    labels = payload.get("labels", []) if isinstance(payload, dict) else []
    return {
        str(row["file"]): {
            "label": str(row.get("label", "app")),
            "reason": str(row.get("reason", "")),
        }
        for row in labels
        if isinstance(row, dict) and "file" in row
    }


def filter_screens(paths: RunPaths, *, timeout: int = 600) -> dict[str, object]:
    """Route frames three ways: keep ``app``, preserve+mark ``ad``, drop the rest.

    ``app`` frames stay in screens.json for analysis. ``ad`` frames are moved to
    ``ad_screens/`` and recorded in the audit with their sequence position (for
    later ad-placement inference) — never discarded. ``ios_home``/``other`` are
    dropped to ``excluded_screens/`` as before. Fail-open: if *no* app frame
    survives, nothing is moved (a mis-classification must not empty the run).
    """
    data = json.loads(paths.screens_json.read_text())
    screens = data.get("screens", []) if isinstance(data, dict) else []
    labels = _classify(paths, timeout)

    def label_of(scr: dict[str, object]) -> str:
        return labels.get(str(scr.get("screenshot", "")), {}).get("label", KEEP_LABEL)

    def reason_of(scr: dict[str, object]) -> str:
        return labels.get(str(scr.get("screenshot", "")), {}).get("reason", "")

    kept = [s for s in screens if label_of(s) == KEEP_LABEL]
    ads = [s for s in screens if label_of(s) == AD_LABEL]
    dropped = [s for s in screens if label_of(s) not in (KEEP_LABEL, AD_LABEL)]

    app_indices = [i for i, s in enumerate(screens) if label_of(s) == KEEP_LABEL]

    def _neighbour_app(index: int, *, forward: bool) -> str | None:
        pool = [j for j in app_indices if (j > index if forward else j < index)]
        if not pool:
            return None
        pick = pool[0] if forward else pool[-1]
        return str(screens[pick].get("id"))

    ads_audit = [
        {
            "id": str(s.get("id")),
            "screenshot": str(s.get("screenshot")),
            "reason": reason_of(s),
            "ordinal": i,
            "prev_app_id": _neighbour_app(i, forward=False),
            "next_app_id": _neighbour_app(i, forward=True),
        }
        for i, s in enumerate(screens)
        if label_of(s) == AD_LABEL
    ]

    counts = {
        "total": len(screens),
        "kept": len(kept),
        "ads": len(ads),
        "excluded": len(dropped),
    }
    audit = {
        "kept": [str(s.get("id")) for s in kept],
        "ads": ads_audit,
        "excluded": [
            {"id": str(s.get("id")), "screenshot": str(s.get("screenshot")), "reason": reason_of(s)}
            for s in dropped
        ],
        "counts": counts,
    }
    paths.screen_labels_json.write_text(json.dumps(audit, indent=2, ensure_ascii=False))

    if not kept:
        log.warning("screen_filter.all_excluded_keeping_all", total=len(screens))
        return audit

    excluded_dir = paths.run_dir / "excluded_screens"
    excluded_dir.mkdir(parents=True, exist_ok=True)
    for s in dropped:
        src = paths.screens_dir / str(s.get("screenshot"))
        if src.exists():
            src.rename(excluded_dir / src.name)

    if ads:
        paths.ad_screens_dir.mkdir(parents=True, exist_ok=True)
        for s in ads:
            src = paths.screens_dir / str(s.get("screenshot"))
            if src.exists():
                src.rename(paths.ad_screens_dir / src.name)

    kept_ids = {str(s.get("id")) for s in kept}
    for s in kept:
        if str(s.get("from")) not in kept_ids:
            s["from"] = None
        nav = s.get("navigates_to")
        if isinstance(nav, list):
            s["navigates_to"] = [n for n in nav if str(n) in kept_ids]

    data["screens"] = kept
    data["screen_count"] = len(kept)
    paths.screens_json.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    log.info("screen_filter.done", **counts)
    return audit
