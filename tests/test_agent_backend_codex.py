"""The codex backend through a fake docker: container argv, refusals, workspace scrub, login.

The fake ``docker`` answers ``info``/``image inspect``/``kill`` and, for ``run``, plays the
codex CLI inside the container: it records the docker and codex argv and stdin, writes the
result file into ``-C <ws>`` the way the real agent would, and prints chatter on stdout that
no caller may depend on. The real container is probed in test_agent_backend_isolation.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import ad_analysis, agent_backend, analyze, compliance, feasibility, screen_filter
from tests.test_ad_analysis import _seed as _seed_ads
from tests.test_agent_backend import _no_agent_env, _settings
from tests.test_feasibility import _proposal_payload, _spec_three_screens, _write_spec
from tests.test_mvp_analyze import _VALID_APP_SPEC
from tests.test_mvp_analyze import _make_paths as _analyze_paths
from tests.test_mvp_compliance import _WEIGHTS
from tests.test_mvp_compliance import _make_paths as _compliance_paths
from tests.test_screen_filter import _seed_run

__all__ = ["_no_agent_env"]

_REFRESH = "rt-0123456789abcdefghijklmnop"

_FAKE_DOCKER = """#!{python}
import json, os, sys, time
argv = sys.argv[1:]
env = os.environ
if argv[0] == "info":
    sys.exit(int(env.get("FAKE_DOCKER_INFO_EXIT", "0")))
if argv[0] == "image":
    sys.exit(int(env.get("FAKE_DOCKER_IMAGE_EXIT", "0")))
if argv[0] == "kill":
    with open(env["FAKE_CODEX_LOG"] + ".kill", "w") as fh:
        fh.write(argv[1])
    sys.exit(0)
at = argv.index(env["FAKE_CODEX_IMAGE"])
docker, codex = argv[1:at], argv[at + 1:]
ws = codex[codex.index("-C") + 1]
last = codex[codex.index("-o") + 1]
mounts = dict(docker[i + 1].rsplit(":", 1)[::-1] for i, a in enumerate(docker) if a == "-v")
prompt = sys.stdin.read()
with open(env["FAKE_CODEX_LOG"], "w") as fh:
    json.dump({{"docker": docker, "argv": codex, "stdin": prompt, "cwd": os.getcwd()}}, fh)
if env.get("FAKE_CODEX_REFRESH"):
    with open(os.path.join(mounts["/codex-home"], "auth.json"), "w") as fh:
        fh.write(env["FAKE_CODEX_REFRESH"])
if env.get("FAKE_CODEX_HOST_BUMP"):
    with open(env["FAKE_CODEX_HOST_BUMP"], "w") as fh:
        fh.write('{{"tokens": "newer"}}')
if env.get("FAKE_CODEX_SYMLINK"):
    os.makedirs(os.path.join(ws, "App"), exist_ok=True)
    os.symlink(env["FAKE_CODEX_SYMLINK"], os.path.join(ws, "App", "Leak.swift"))
    os.symlink(os.path.dirname(env["FAKE_CODEX_SYMLINK"]), os.path.join(ws, "HostDir"))
if env.get("FAKE_CODEX_LEAK"):
    with open(os.path.join(mounts["/codex-home"], "auth.json")) as src:
        login = src.read()
    with open(os.path.join(ws, "notes.txt"), "w") as fh:
        fh.write("copied: " + login)
time.sleep(float(env.get("FAKE_CODEX_SLEEP", "0")))
name = env.get("FAKE_CODEX_FILE")
if name:
    with open(os.path.join(ws, name), "w") as fh:
        fh.write(env["FAKE_CODEX_PAYLOAD"])
with open(last, "w") as fh:
    fh.write("done: wrote " + (name or "nothing"))
print("codex chatter that is not JSON {{")
sys.exit(int(env.get("FAKE_CODEX_EXIT", "0")))
"""


@pytest.fixture
def fake_codex(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Path]:
    """Point the configured backend at a fake docker; returns a setter for codex's output."""
    binary = tmp_path / "bin" / "docker"
    binary.parent.mkdir()
    binary.write_text(_FAKE_DOCKER.format(python=sys.executable))
    binary.chmod(0o755)
    login = tmp_path_factory.mktemp("host_codex_home")
    (login / "auth.json").write_text(json.dumps({"tokens": {"refresh_token": _REFRESH}}))
    monkeypatch.setenv("CODEX_HOME", str(login))
    log = tmp_path / "codex_call.json"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    settings = _settings(codegen_agent="codex", docker_bin=str(binary))
    monkeypatch.setenv("FAKE_CODEX_IMAGE", settings.codex_image)
    monkeypatch.setattr(agent_backend, "get_settings", lambda: settings)

    def produce(filename: str | None = None, payload: object = None) -> Path:
        if filename:
            monkeypatch.setenv("FAKE_CODEX_FILE", filename)
            monkeypatch.setenv("FAKE_CODEX_PAYLOAD", json.dumps(payload))
        return log

    return produce


def test_codex_fake_binary_gets_prompt_on_stdin_and_edits_ws(
    tmp_path: Path, fake_codex: Callable[..., Path]
) -> None:
    """A long prompt goes through stdin, not argv; the -o file stays outside ws."""
    log = fake_codex("out.json", {"ok": True})
    ws = tmp_path / "ws"
    ws.mkdir()
    prompt = "line one\n" + "x" * 300_000

    result = agent_backend.run_agent(ws, prompt, timeout=30)

    call = json.loads(log.read_text())
    assert call["stdin"] == prompt
    assert prompt not in call["argv"]
    assert call["argv"][-1] == "-"
    assert Path(call["cwd"]).resolve() == ws.resolve()
    assert json.loads((ws / "out.json").read_text()) == {"ok": True}
    assert result.backend == "codex" and result.returncode == 0
    assert result.last_message == "done: wrote out.json"
    assert "chatter" in result.stdout
    assert sorted(p.name for p in ws.iterdir()) == ["out.json"]


def test_codex_runs_only_in_the_locked_down_container(
    tmp_path: Path, fake_codex: Callable[..., Path]
) -> None:
    """Only ws, a temp dir for -o and a temp copy of the login are mounted; no host env."""
    log = fake_codex()
    ws = tmp_path / "ws"
    ws.mkdir()
    agent_backend.run_agent(ws, "p", timeout=30)

    docker = json.loads(log.read_text())["docker"]
    codex = json.loads(log.read_text())["argv"]
    mounts = [docker[i + 1] for i, a in enumerate(docker) if a == "-v"]
    envs = [docker[i + 1] for i, a in enumerate(docker) if a == "-e"]
    assert docker[:2] == ["--rm", "-i"]
    assert docker[docker.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert docker[docker.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in docker and "no-new-privileges" in docker
    assert envs == ["HOME=/codex-home", "CODEX_HOME=/codex-home"]
    assert len(mounts) == 3
    home, out = Path(mounts[0].split(":")[0]), Path(mounts[1].split(":")[0])
    assert mounts[0].endswith(":/codex-home") and home.name == "home"
    assert mounts[1] == f"{out}:{out}" and out == Path(codex[codex.index("-o") + 1]).parent
    assert mounts[2] == f"{ws.resolve()}:{ws.resolve()}"
    assert home.parent == out.parent and not home.exists()
    for mount in mounts:
        assert not str(Path.home()).startswith(mount.split(":")[0])
    assert docker[docker.index("-w") + 1] == str(ws.resolve())


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("no_docker", "is not installed"),
        ("daemon_down", "running Docker daemon"),
        ("no_image", "docker build -t iosforge-codex:0.153.4 docker/codex"),
        ("no_login", "run `codex login` first"),
    ],
)
def test_codex_refuses_instead_of_running_unisolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_codex: Callable[..., Path],
    setup: str,
    message: str,
) -> None:
    log = fake_codex("out.json", {"ok": True})
    if setup == "no_docker":
        monkeypatch.setattr(
            agent_backend, "get_settings",
            lambda: _settings(codegen_agent="codex", docker_bin=str(tmp_path / "nope")),
        )  # fmt: skip
    elif setup == "daemon_down":
        monkeypatch.setenv("FAKE_DOCKER_INFO_EXIT", "1")
    elif setup == "no_image":
        monkeypatch.setenv("FAKE_DOCKER_IMAGE_EXIT", "1")
    else:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    with pytest.raises(agent_backend.AgentBackendUnavailable, match=message):
        agent_backend.run_agent(tmp_path, "p", timeout=30)
    assert not log.exists() and not (tmp_path / "out.json").exists()


def test_codex_timeout_kills_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Callable[..., Path]
) -> None:
    log = fake_codex()
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "5")
    with pytest.raises(subprocess.TimeoutExpired):
        agent_backend.run_agent(tmp_path, "p", timeout=1)
    killed = Path(str(log) + ".kill").read_text()
    assert killed.startswith("iosforge-codex-")


def test_codex_login_refreshed_in_the_container_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Callable[..., Path]
) -> None:
    """ChatGPT rotates refresh tokens, so the host must keep the refreshed login."""
    fake_codex()
    host = Path(os.environ["CODEX_HOME"]) / "auth.json"
    monkeypatch.setenv("FAKE_CODEX_REFRESH", '{"tokens": "new"}')
    agent_backend.run_agent(tmp_path, "p", timeout=30)
    assert json.loads(host.read_text()) == {"tokens": "new"}
    assert host.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("FAKE_CODEX_REFRESH", "not json")
    agent_backend.run_agent(tmp_path, "p", timeout=30)
    assert json.loads(host.read_text()) == {"tokens": "new"}


def test_codex_non_zero_exit_is_returned_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Callable[..., Path]
) -> None:
    fake_codex()
    monkeypatch.setenv("FAKE_CODEX_EXIT", "3")
    assert agent_backend.run_agent(tmp_path, "p", timeout=30).returncode == 3


# --- every model task consumes its result file the same way on codex --------------------


def test_analyze_on_codex(tmp_path: Path, fake_codex: Callable[..., Path]) -> None:
    log = fake_codex("app_spec.json", _VALID_APP_SPEC)
    out = analyze.analyze(_analyze_paths(tmp_path))
    assert json.loads(out.read_text())["app_name"] == "Todo"
    assert 'web_search="live"' in json.loads(log.read_text())["argv"]


def test_feasibility_on_codex(tmp_path: Path, fake_codex: Callable[..., Path]) -> None:
    from iosforge.mvp.paths import RunPaths

    log = fake_codex("scope_proposal.json", _proposal_payload())
    paths = RunPaths.create(tmp_path / "run")
    _write_spec(paths, _spec_three_screens())
    decision = feasibility.propose(paths, settings=_settings())
    assert decision.counts.included == 2 and decision.counts.excluded == 1
    assert 'web_search="disabled"' in json.loads(log.read_text())["argv"]


def test_ad_analysis_on_codex(tmp_path: Path, fake_codex: Callable[..., Path]) -> None:
    fake_codex(
        "ad_analysis.json",
        {"ad_networks": [], "ad_placements": [{"format": "rewarded", "trigger": "tap"}]},
    )
    out = ad_analysis.analyze_ads(_seed_ads(tmp_path, with_ads=True, with_spec=True))
    assert out is not None
    assert [p["format"] for p in json.loads(out.read_text())["ad_placements"]] == ["rewarded"]


def test_screen_filter_on_codex(tmp_path: Path, fake_codex: Callable[..., Path]) -> None:
    fake_codex("screen_labels.json", {"labels": [{"file": "0000.png", "label": "ad"}]})
    labels = screen_filter._classify(_seed_run(tmp_path), 30)
    assert labels == {"0000.png": {"label": "ad", "reason": ""}}


def test_vision_judge_on_codex(tmp_path: Path, fake_codex: Callable[..., Path]) -> None:
    judge = {"screens": [{"id": "0000", "score": 0.9, "diffs": []}], "flows_score": 0.8}
    fake_codex("judge.json", judge)
    report = compliance.evaluate(
        _compliance_paths(tmp_path), 1, weights=_WEIGHTS, threshold=0.95, soft_floor=0.8, history=[]
    )
    assert report["structure"] == pytest.approx(0.9)


def test_symlinks_the_agent_planted_are_removed_before_the_host_sees_ws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Callable[..., Path]
) -> None:
    """The host copies and zips ws; a link to a host file would carry the file out."""
    secret = tmp_path / "host" / "secret.env"
    secret.parent.mkdir()
    secret.write_text("S3_SECRET=host-canary")
    fake_codex("out.json", {"ok": True})
    monkeypatch.setenv("FAKE_CODEX_SYMLINK", str(secret))
    ws = tmp_path / "ws"
    ws.mkdir()

    agent_backend.run_agent(ws, "p", timeout=30)

    assert not [p for p in ws.rglob("*") if p.is_symlink()]
    assert (ws / "out.json").is_file() and (ws / "App").is_dir()
    copied = tmp_path / "copied"
    shutil.copytree(ws, copied)
    assert not any("host-canary" in p.read_text() for p in copied.rglob("*") if p.is_file())
    assert secret.read_text() == "S3_SECRET=host-canary"


def test_a_login_copied_into_ws_is_removed_and_the_run_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Callable[..., Path]
) -> None:
    fake_codex("out.json", {"ok": True})
    monkeypatch.setenv("FAKE_CODEX_LEAK", "1")
    with pytest.raises(agent_backend.AgentOutputRejected, match="login"):
        agent_backend.run_agent(tmp_path, "p", timeout=30)
    assert not (tmp_path / "notes.txt").exists()
    assert (tmp_path / "out.json").is_file()


def test_a_login_refreshed_during_the_run_is_caught_in_ws_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Callable[..., Path]
) -> None:
    """After a refresh the new token is the live one; the old one is already rotated."""
    fake_codex()
    fresh = {"tokens": {"refresh_token": "rt-fresh-9876543210zyxwvutsrqp"}}
    monkeypatch.setenv("FAKE_CODEX_REFRESH", json.dumps(fresh))
    monkeypatch.setenv("FAKE_CODEX_LEAK", "1")
    with pytest.raises(agent_backend.AgentOutputRejected):
        agent_backend.run_agent(tmp_path, "p", timeout=30)
    assert not (tmp_path / "notes.txt").exists()


def test_a_task_that_did_not_refresh_never_restores_an_older_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Callable[..., Path]
) -> None:
    """Another task refreshed the host login meanwhile; this one must not put the old back."""
    fake_codex()
    host = Path(os.environ["CODEX_HOME"]) / "auth.json"
    monkeypatch.setenv("FAKE_CODEX_HOST_BUMP", str(host))
    agent_backend.run_agent(tmp_path, "p", timeout=30)
    assert json.loads(host.read_text()) == {"tokens": "newer"}


def test_any_interruption_kills_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Callable[..., Path]
) -> None:
    log = fake_codex()
    real_run = subprocess.run

    def interrupted(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "run":
            raise KeyboardInterrupt
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(agent_backend.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        agent_backend.run_agent(tmp_path, "p", timeout=30)
    assert Path(str(log) + ".kill").read_text().startswith("iosforge-codex-")
