"""The real codex container keeps the agent away from the host (no model is called).

Runs the exact ``docker run`` prefix the codex backend uses, but with ``codex sandbox`` (the
same sandbox codex puts the agent's shell commands in) instead of ``codex exec``, and probes
what the agent could reach. A control run mounts the host home on purpose and must see it,
so the probes are shown to detect a leak. Skipped without Docker or the ``iosforge-codex``
image.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import agent_backend

_REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = r"""
for p in "$@"; do
  if [ -e "$p" ] || [ -r "$p" ]; then echo "visible $p"; else echo "hidden $p"; fi
done
echo "canary=${IOSFORGE_ISOLATION_CANARY:-absent}"
touch ./written-by-agent && echo "ws-write=ok"
for d in /codex-home "$OUT_DIR"; do
  touch "$d/escape" 2>/dev/null && echo "write-outside=$d" || true
done
ln -s "$1" ./leak-link && echo "symlink=planted"
node -e '
  const s = require("net").connect(443, "1.1.1.1");
  s.on("connect", () => { console.log("network=on"); process.exit(0); });
  s.on("error", (e) => { console.log("network=off " + e.code); process.exit(0); });
  setTimeout(() => { console.log("network=off timeout"); process.exit(0); }, 8000);
'
"""


def _docker_ready(settings: Settings) -> bool:
    if shutil.which(settings.docker_bin) is None:
        return False
    probe = subprocess.run(
        [settings.docker_bin, "image", "inspect", settings.codex_image],
        capture_output=True, timeout=30, check=False,
    )  # fmt: skip
    return probe.returncode == 0


_SETTINGS = Settings(_env_file=None)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_ready(_SETTINGS), reason="needs Docker and the codex image"),
]


def _probe(tmp_path: Path, *, mount_home: bool) -> tuple[list[str], list[Path], Path]:
    ws, out, home = tmp_path / "ws", tmp_path / "out", tmp_path / "home"
    for d in (ws, out, home):
        d.mkdir()
    secrets = [
        _REPO_ROOT / ".env",
        Path.home() / ".ssh",
        Path.home() / ".codex" / "auth.json",
        Path.home(),
        _REPO_ROOT,
    ]
    inner = [
        _SETTINGS.codex_bin, "sandbox", "-c", 'sandbox_mode="workspace-write"', "--",
        "/bin/sh", "-c", f"OUT_DIR={out}\n{_PROBE}", "probe", *map(str, secrets),
    ]  # fmt: skip
    cmd = agent_backend.codex_container_command(
        _SETTINGS, ws.resolve(), out, home, f"iosforge-codex-isolation-{tmp_path.name}", inner
    )
    if mount_home:
        at = cmd.index("-w")
        cmd[at:at] = ["-v", f"{Path.home()}:{Path.home()}:ro"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    assert res.returncode == 0, res.stderr[-1000:]
    return res.stdout.splitlines(), secrets, ws


def test_agent_sandbox_in_the_container_cannot_reach_host_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IOSFORGE_ISOLATION_CANARY", "host-secret")
    lines, secrets, ws = _probe(tmp_path, mount_home=False)

    assert [f"hidden {p}" for p in secrets] == [ln for ln in lines if ln.startswith("hidden ")]
    assert not [ln for ln in lines if ln.startswith("visible ")]
    assert "canary=absent" in lines
    assert "ws-write=ok" in lines and (ws / "written-by-agent").exists()
    assert not [ln for ln in lines if ln.startswith("write-outside=")]
    assert [ln for ln in lines if ln.startswith("network=")][0].startswith("network=off")

    assert "symlink=planted" in lines and (ws / "leak-link").is_symlink()
    links, _ = agent_backend.scrub_workspace(ws, [])
    assert links == [ws / "leak-link"] and not (ws / "leak-link").exists()


def test_the_probe_sees_the_host_when_it_is_mounted(tmp_path: Path) -> None:
    """Control: with the host home mounted on purpose the same probe reports it."""
    lines, _, _ = _probe(tmp_path, mount_home=True)
    assert f"visible {_REPO_ROOT / '.env'}" in lines or not (_REPO_ROOT / ".env").exists()
    assert f"visible {Path.home()}" in lines
