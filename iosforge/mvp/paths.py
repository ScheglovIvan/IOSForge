"""Single source of truth for the run-directory layout (keeps steps in sync)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    """Layout of one MVP run: runs/<ts>/{screens/,screens.json,claude_ws/,flutter_app/}."""

    run_dir: Path
    screens_dir: Path
    screens_json: Path
    claude_ws: Path
    flutter_app: Path

    @classmethod
    def create(cls, base: Path) -> RunPaths:
        ts = time.strftime("%Y%m%d-%H%M%S")
        run_dir = base / ts
        rp = cls(
            run_dir=run_dir,
            screens_dir=run_dir / "screens",
            screens_json=run_dir / "screens.json",
            claude_ws=run_dir / "claude_ws",
            flutter_app=run_dir / "flutter_app",
        )
        rp.screens_dir.mkdir(parents=True, exist_ok=True)
        return rp
