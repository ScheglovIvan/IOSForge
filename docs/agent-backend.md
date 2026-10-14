# Coding-agent backend (claude | codex)

Every model task (screen codegen and fixes, the Vision Judge, analyze, feasibility/scope,
ad analysis, screen classification) starts its agent through one function:
`iosforge/mvp/agent_backend.py::run_agent(ws, prompt, *, timeout, allowed_tools=None, backend=None)`.
This page records the decisions behind it (the team's DECISIONS log lives in the untracked
`.claude/state/`, so the decisions that matter for running the factory are kept here too).

## Choosing the backend

| Setting | Default | Meaning |
|---|---|---|
| `CODEGEN_AGENT` | `claude` | `claude` = Claude Code CLI on the host; `codex` = Codex CLI in a container |
| `CLAUDE_BIN` | `claude` | the Claude Code binary |
| `CODEX_MODEL` | `gpt-6-astra` | codex model id (`codex debug models` lists what the account can use) |
| `CODEX_REASONING_EFFORT` | empty | `low`, `medium`, `high`, `xhigh`, `max` or `ultra`; empty = the model's default |
| `CODEX_IMAGE` | `iosforge-codex:0.153.4` | image the codex backend runs in |
| `DOCKER_BIN` | `docker` | Docker CLI |
| `CODEX_BIN` | `codex` | codex binary inside the image |
| `CODEX_IGNORE_USER_CONFIG` | `true` | ignore `~/.codex/config.toml` so a personal profile cannot change a run |

Precedence: the `backend=` argument, then `with override_backend("codex"):` (process-wide,
correct for the prefork worker with one task per process), then `CODEGEN_AGENT`. A per-job
choice (`Job.source_app_metadata["codegen_agent"]`, no migration) is not wired yet.

## What the callers consume

No caller parses the agent's stdout. Each one reads the file its prompt asks the agent to
write into `ws`: `app_spec.json` (analyze), `scope_proposal.json` (feasibility),
`ad_analysis.json`, `screen_labels.json`, `judge.json` (Vision Judge), or the edited Swift
sources (codegen). Both backends therefore return the same `AgentResult(backend, returncode,
stdout, stderr, last_message)`; stdout (or, for codex, its last message) only feeds the
task transcript `.claude_task.log` (historical name, any backend).

## claude

Unchanged from before the switch: `claude -p <prompt> --permission-mode acceptEdits`
(plus `--allowed-tools <list>` where a stage restricts tools), `cwd=ws`.

## codex: always in a container

codex has no tool whitelist and its sandbox can read every file it can see; what it reads
is sent to OpenAI. On the host that includes the repo `.env` files, `~/.ssh` and the codex
login. So the backend never runs codex on the host. Each task runs

```
docker run --rm -i --name iosforge-codex-<id> --user <uid>:<gid> --cap-drop ALL
  --security-opt no-new-privileges --security-opt seccomp=docker/codex/seccomp.json
  --security-opt apparmor=unconfined --read-only --tmpfs /tmp --pids-limit 512
  -e HOME=/codex-home -e CODEX_HOME=/codex-home
  -v <temp copy of the codex login>:/codex-home -v <temp out dir>:<same> -v <ws>:<ws> -w <ws>
  iosforge-codex:0.153.4
  codex exec --skip-git-repo-check --ephemeral -s workspace-write -c approval_policy="never"
    -c web_search="disabled|live" -c shell_environment_policy.inherit="core" -m <model>
    [-c model_reasoning_effort="<effort>"] --ignore-user-config -C <ws> -o <out>/last_message.txt -
```

with the prompt on stdin.

- Mounted: only the job workspace, a temporary directory for the last message, and a
  temporary copy of `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`). The host's home,
  repo and environment are not in the container.
- The agent's shell runs in codex's own `workspace-write` sandbox (bubblewrap) inside the
  container: it can write only in `ws`, has no network (connections fail with EPERM) and
  cannot change the login copy or the out directory.
- `docker/codex/seccomp.json` is Docker's default seccomp profile plus `clone`, `clone3`,
  `unshare`, `setns`, `mount`, `umount2` and `pivot_root`, which bubblewrap needs to build
  that sandbox as an unprivileged user. Docker's default profile alone makes bubblewrap
  fail. AppArmor is unconfined for the same reason on Linux workers (Ubuntu 24.04 also
  restricts unprivileged user namespaces through AppArmor). The container runs as the host
  user, without capabilities and with `no-new-privileges`, on a read-only image.
- After every run (also on a timeout or an error) the backend scrubs `ws`: every symlink is
  removed, because the host follows symlinks when it copies or zips a workspace and a link
  the agent planted to a host path would carry that file into artifacts and into the next
  agent run. The pipeline itself never puts symlinks in a workspace.
- Files in `ws` that contain a token from the codex login are removed and the run fails with
  `AgentOutputRejected`: the agent can read the login copy, and a copy of it in `ws` would
  end up in storage and admin downloads. Both the login codex started with and one it
  refreshed during the run are checked. This is a byte-for-byte search, a second line of
  defence: an encoded or split token would get through.
- If codex refreshed its login inside the container, the refreshed `auth.json` is written
  back to the host under a lock (ChatGPT rotates refresh tokens). A task whose copy did not
  change never writes back, so it cannot restore a token another task has already rotated.
- The container is killed by name if the run is interrupted for any reason (timeout,
  worker shutdown) before the error propagates.
- Without the Docker CLI, a running daemon, the image, the seccomp profile or a codex login,
  `run_agent` raises `AgentBackendUnavailable` and nothing runs.

What the isolation does not cover:

- The codex process itself (not the agent's shell) has normal container networking, which
  it needs for the OpenAI API. That includes `host.docker.internal` and the published local
  ports (Postgres, Redis, MinIO). Only the agent's commands are cut off.
- Hard links: `store_assets` hard-links source files into its workspace. The agent writing
  to such a file changes the original outside `ws` (an integrity issue, not a leak).
- Web search: `live` only where a stage would have given Claude `WebSearch`/`WebFetch`
  (analyze). Fetching an arbitrary URL is not possible. Search queries do leave the
  sandbox, so anything the agent read in `ws` could be put into a query.

Codex CLI 0.153 specifics: `codex exec` has no `--full-auto` (use `-s workspace-write` and
`approval_policy="never"`); `-a` and `--search` exist only before `exec`.

### Setting it up on a worker

```bash
codex login                                       # once, on the host
docker build -t iosforge-codex:0.153.4 docker/codex
uv run pytest -m docker tests/test_agent_backend_isolation.py   # no model call
```

The isolation test runs the backend's exact `docker run` prefix with `codex sandbox` and
checks from inside the agent's sandbox that the repo `.env`, `~/.ssh`, the host home and
the host environment are not reachable, that writes to the login copy and the out
directory fail, that there is no network, and that a symlink the agent plants is removed by
the scrub. A control run with the host home mounted on purpose must report it as visible.
