"""Deterministic stub for :class:`EmulatorProvider` (T-3.3, SPEC §5.4/§9).

``install``/``teardown`` are no-ops; ``walkthrough`` returns two fixture screens
with a minimal navigation ``screen_map`` and logs. No online emulator (Firebase
Test Lab, T-7.2) is touched. Registered under ``(emulator, "stub")``.
"""

from __future__ import annotations

from iosforge.providers.base import (
    ScreenShot,
    WalkthroughResult,
    WalkthroughStrategy,
)
from iosforge.providers.registry import ProviderKind, registry
from iosforge.storage import ArtifactRef

STUB_NAME = "stub"


def _fixture_screen(index: int) -> ScreenShot:
    return ScreenShot(
        screen_id=f"screen-{index}",
        title=f"Stub Screen {index}",
        artifact_ref=ArtifactRef(
            bucket="iosforge",
            key=f"jobs/stub/screens/screen-{index}.png",
            size=16,
            content_type="image/png",
            kind="screenshot",
            job_id="stub",
        ),
    )


class StubEmulatorProvider:
    """Fixture emulator: no-op install/teardown, deterministic walkthrough."""

    def name(self) -> str:
        return STUB_NAME

    def install(self, apk: ArtifactRef) -> None:
        return None

    def walkthrough(self, strategy: WalkthroughStrategy) -> WalkthroughResult:
        screens = [_fixture_screen(0), _fixture_screen(1)]
        screen_map: dict[str, object] = {
            "nodes": [s.screen_id for s in screens],
            "edges": [{"from": "screen-0", "to": "screen-1", "action": "tap"}],
        }
        return WalkthroughResult(
            screens=screens,
            screen_map=screen_map,
            logs="stub walkthrough: 2 screens captured",
            partial=False,
        )

    def teardown(self) -> None:
        return None


registry.add(ProviderKind.EMULATOR, STUB_NAME, StubEmulatorProvider, override=True)
