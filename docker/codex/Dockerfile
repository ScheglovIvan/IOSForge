# Codex CLI for the codex agent backend (iosforge/mvp/agent_backend.py). The backend runs every
# codex task in this image so the agent only sees the job workspace, never the host.
# Build: docker build -t iosforge-codex:0.153.4 docker/codex
FROM node:22.11.0-bookworm-slim
RUN npm install -g @openai/codex@0.153.4 \
    && apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/*
