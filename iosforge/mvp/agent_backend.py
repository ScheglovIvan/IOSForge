"""The one place that starts the coding agent for a model task.

:func:`run_agent` runs the agent non-interactively in a workspace ``ws`` with a prompt and
lets it edit files right there. Every caller reads its result from files in ``ws``
(``app_spec.json``, ``judge.json``, the Swift sources ...), never from stdout, so both
backends return the same :class:`AgentResult` and stdout is kept only for diagnostics.

``claude`` runs the Claude Code CLI exactly as the pipeline always has. ``codex`` runs the
Codex CLI (OpenAI models such as ``gpt-6-astra``) in its ``workspace-write`` sandbox. The
backend comes from the ``backend`` argument, then :func:`override_backend`, then
``Settings.codegen_agent``. Prompts are passed through unchanged.

codex has no tool whitelist and its sandbox can read the whole filesystem it sees, and what
it reads goes to OpenAI. So codex never runs on the host: :func:`codex_container_command`
starts it in ``Settings.codex_image`` (``docker/codex/Dockerfile``) with only ``ws``, a
temporary directory for its last message and a temporary copy of the codex login mounted;
the host's home, ``.env`` files and environment are not there. The agent's shell gets no
network and can write only inside ``ws``. Without Docker, a running daemon, the image or a
codex login the backend refuses with :class:`AgentBackendUnavailable` instead of running.
WebSearch/WebFetch map to codex's live web search; fetching an arbitrary URL is not possible.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, get_args

import structlog

from iosforge.common.config import CodegenAgent, Settings, get_settings
from iosforge.common.logging import get_logger

AgentBackend = CodegenAgent
AGENT_BACKENDS: Final[tuple[AgentBackend, ...]] = get_args(AgentBackend)
WEB_TOOLS: Final = frozenset({"WebSearch", "WebFetch"})

CONTAINER_CODEX_HOME: Final = "/codex-home"
SECCOMP_PROFILE: Final = Path(__file__).resolve().parents[2] / "docker" / "codex" / "seccomp.json"
_MIN_TOKEN_LEN: Final = 20

log = get_logger("mvp.agent_backend")

_override: AgentBackend | None = None


class AgentBackendUnavailable(RuntimeError):
    """The configured backend cannot run safely on this machine."""


class AgentOutputRejected(RuntimeError):
    """The agent left something in the workspace that must not leave the sandbox."""


@dataclass(frozen=True, slots=True)
class AgentResult:
    backend: AgentBackend
    returncode: int
    stdout: str
    stderr: str
    last_message: str | None = None


@contextmanager
def override_backend(backend: AgentBackend | None) -> Iterator[None]:
    """Run the enclosed tasks on ``backend`` (``None`` keeps the configured one).

    A process-wide value rather than a ContextVar, so the screen tasks that swiftui_gen
    runs in a ThreadPoolExecutor see it too. Correct for prefork workers (one task per
    process); not for a threaded Celery pool.
    """
    global _override
    previous = _override
    _override = backend
    try:
        yield
    finally:
        _override = previous


def resolve_backend(
    backend: AgentBackend | None = None, settings: Settings | None = None
) -> AgentBackend:
    if backend is not None:
        return backend
    if _override is not None:
        return _override
    return (settings or get_settings()).codegen_agent


def claude_command(claude_bin: str, prompt: str, allowed_tools: str | None) -> list[str]:
    cmd = [claude_bin, "-p", prompt, "--permission-mode", "acceptEdits"]
    if allowed_tools:
        cmd += ["--allowed-tools", allowed_tools]
    return cmd


def codex_command(
    settings: Settings, ws: Path, allowed_tools: str | None, last_message_path: Path
) -> list[str]:
    """``codex exec`` reading the prompt from stdin (``-``); run it via the container.

    Web search is on only when the caller would have allowed WebSearch/WebFetch.
    """
    web = "live" if WEB_TOOLS & set((allowed_tools or "").split()) else "disabled"
    cmd = [
        settings.codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "-c",
        f'web_search="{web}"',
        "-c",
        'shell_environment_policy.inherit="core"',
        "-m",
        settings.codex_model,
    ]
    if settings.codex_reasoning_effort:
        cmd += ["-c", f'model_reasoning_effort="{settings.codex_reasoning_effort}"']
    if settings.codex_ignore_user_config:
        cmd.append("--ignore-user-config")
    cmd += ["-C", str(ws), "-o", str(last_message_path), "-"]
    return cmd


def host_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def ensure_codex_runtime(settings: Settings) -> Path:
    """Check Docker, the codex image and the codex login; return the host ``auth.json``."""
    docker = settings.docker_bin
    if shutil.which(docker) is None:
        raise AgentBackendUnavailable(
            f"codex backend needs Docker to isolate the agent, but '{docker}' is not installed"
        )
    info = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    if info.returncode != 0:
        raise AgentBackendUnavailable(
            "codex backend needs a running Docker daemon to isolate the agent: "
            + (info.stderr.strip()[-300:] or "docker info failed")
        )
    image = subprocess.run(
        [docker, "image", "inspect", settings.codex_image],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    if image.returncode != 0:
        raise AgentBackendUnavailable(
            f"codex image '{settings.codex_image}' is missing; build it with "
            f"`docker build -t {settings.codex_image} docker/codex`"
        )
    if not SECCOMP_PROFILE.is_file():
        raise AgentBackendUnavailable(f"codex seccomp profile missing at {SECCOMP_PROFILE}")
    auth = host_codex_home() / "auth.json"
    if not auth.is_file():
        raise AgentBackendUnavailable(f"no codex login at {auth}; run `codex login` first")
    return auth


def codex_container_command(
    settings: Settings, ws: Path, out_dir: Path, codex_home: Path, name: str, inner: list[str]
) -> list[str]:
    """``docker run`` that executes ``inner`` with only ``ws``, ``out_dir`` and ``codex_home``.

    Not root, no capabilities, read-only image, no host environment. The seccomp profile
    (docker/codex/seccomp.json) is Docker's default plus the namespace calls bubblewrap
    needs to build codex's own agent sandbox, which keeps the agent's shell off the network
    and out of everything but ``ws``; apparmor is unconfined for the same reason on Linux.
    """
    return [
        settings.docker_bin,
        "run",
        "--rm",
        "-i",
        "--name",
        name,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--security-opt",
        f"seccomp={SECCOMP_PROFILE}",
        "--security-opt",
        "apparmor=unconfined",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--pids-limit",
        "512",
        "-e",
        f"HOME={CONTAINER_CODEX_HOME}",
        "-e",
        f"CODEX_HOME={CONTAINER_CODEX_HOME}",
        "-v",
        f"{codex_home}:{CONTAINER_CODEX_HOME}",
        "-v",
        f"{out_dir}:{out_dir}",
        "-v",
        f"{ws}:{ws}",
        "-w",
        str(ws),
        settings.codex_image,
        *inner,
    ]


def _sync_back_login(copy: Path, original: bytes, host: Path) -> None:
    """Keep a login codex refreshed in the container; ChatGPT rotates refresh tokens.

    Only a copy codex actually changed is written back, under a lock, so a parallel task
    that did not refresh cannot put an already used token back over a newer one.
    """
    try:
        fresh = copy.read_bytes()
        if fresh == original or not isinstance(json.loads(fresh), dict):
            return
    except (OSError, ValueError):
        return
    staged = host.with_name(f".{host.name}.{uuid.uuid4().hex}")
    try:
        with open(host.with_name(".iosforge-auth.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(fresh)
            staged.replace(host)
    except OSError:
        log.warning("agent.codex.login_sync_failed", host=str(host))
    finally:
        staged.unlink(missing_ok=True)


def _login_tokens(auth: bytes) -> list[bytes]:
    found: list[bytes] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node) >= _MIN_TOKEN_LEN:
            found.append(node.encode())

    try:
        walk(json.loads(auth))
    except ValueError:
        pass
    return found


def scrub_workspace(ws: Path, tokens: list[bytes]) -> tuple[list[Path], list[Path]]:
    """Remove what the agent must not hand to the host from ``ws``.

    Symlinks go: inside the container they point at nothing, but the host would follow
    them when it copies or zips the workspace (the pipeline itself never puts one there).
    Files that contain the codex login go too. Returns (symlinks, files with the login).
    """
    links: list[Path] = []
    leaked: list[Path] = []
    for root, dirs, files in os.walk(ws, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(root) / name
            if path.is_symlink():
                path.unlink()
                links.append(path)
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            path = Path(root) / name
            if path in links or not tokens:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if any(token in data for token in tokens):
                path.unlink()
                leaked.append(path)
    return links, leaked


def _run_codex(
    settings: Settings, ws: Path, prompt: str, timeout: int, allowed_tools: str | None
) -> AgentResult:
    auth = ensure_codex_runtime(settings)
    ws = ws.resolve()
    original = auth.read_bytes()
    with tempfile.TemporaryDirectory(prefix="iosforge-codex-") as tmp:
        home, out = Path(tmp) / "home", Path(tmp) / "out"
        home.mkdir()
        out.mkdir()
        fd = os.open(home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(original)
        last = out / "last_message.txt"
        name = f"iosforge-codex-{uuid.uuid4().hex[:12]}"
        cmd = codex_container_command(
            settings, ws, out, home, name, codex_command(settings, ws, allowed_tools, last)
        )
        try:
            res = subprocess.run(
                cmd, cwd=ws, input=prompt, capture_output=True, text=True, timeout=timeout,
                check=False,
            )  # fmt: skip
        except BaseException:
            kill = subprocess.run(
                [settings.docker_bin, "kill", name], capture_output=True, timeout=60, check=False
            )
            if kill.returncode != 0:
                log.warning("agent.codex.kill_failed", container=name, code=kill.returncode)
            raise
        finally:
            _sync_back_login(home / "auth.json", original, auth)
            refreshed = home / "auth.json"
            tokens = _login_tokens(original) + (
                _login_tokens(refreshed.read_bytes()) if refreshed.is_file() else []
            )
            links, leaked = scrub_workspace(ws, tokens)
            if links or leaked:
                log.warning(
                    "agent.codex.workspace_scrubbed",
                    ws=str(ws),
                    symlinks=[str(p) for p in links],
                    login_leaks=[str(p) for p in leaked],
                )
        message = last.read_text(encoding="utf-8") if last.is_file() else None
    if leaked:
        raise AgentOutputRejected(
            f"codex wrote its login into the workspace ({len(leaked)} file(s) removed)"
        )
    return AgentResult("codex", res.returncode, res.stdout, res.stderr, message)


def run_agent(
    ws: Path,
    prompt: str,
    *,
    timeout: int,
    allowed_tools: str | None = None,
    backend: AgentBackend | None = None,
    tlog: structlog.stdlib.BoundLogger | None = None,
    settings: Settings | None = None,
) -> AgentResult:
    """Run one agent task in ``ws``; never raises on a non-zero exit (callers decide).

    ``allowed_tools`` is the Claude ``--allowed-tools`` value; codex maps it to web search
    on/off. ``subprocess.TimeoutExpired`` propagates as before (a codex container is killed
    first). codex's last message is written by the CLI itself (not the sandboxed agent) into
    a temporary directory outside ``ws``, so nothing extra lands in workspaces that get
    copied into sandboxes or artifacts. Raises :class:`AgentBackendUnavailable` when codex
    cannot be isolated and :class:`AgentOutputRejected` when it wrote its login into ``ws``.
    """
    settings = settings or get_settings()
    chosen = resolve_backend(backend, settings)
    if chosen == "claude":
        res = subprocess.run(
            claude_command(settings.claude_bin, prompt, allowed_tools),
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        result = AgentResult("claude", res.returncode, res.stdout, res.stderr)
    else:
        result = _run_codex(settings, ws, prompt, timeout, allowed_tools)
    if tlog is not None:
        tlog.debug("agent.run", backend=chosen, code=result.returncode)
    return result
