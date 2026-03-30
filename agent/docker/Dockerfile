FROM ubuntu:22.04

# Sandboxed execution environment for the Ralph Loop agent.
#
# The dispatcher spawns: docker run --rm -v $WORKSPACE:/workspace claude-agent:latest claude -p ...
# No agent source is copied in — the workspace is mounted at runtime.
# The image provides: OS, git, python3, Node.js, and the Claude Code CLI.
#
# Build: docker compose -f agent/docker/docker-compose.yml build
# Or:    docker build -f agent/docker/Dockerfile -t claude-agent:latest agent/

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies — kept minimal for the execution sandbox
RUN apt-get update && apt-get install -y \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI — for push review flow and `gh repo create` inside the sandbox
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (LTS) — required by the Claude Code CLI
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Non-root user — Claude Code refuses --dangerously-skip-permissions as root.
# HOST_UID=501 matches the default macOS first-user UID so that files created
# inside the container have correct ownership on the mounted workspace.
ARG HOST_UID=501
RUN useradd -m -s /bin/bash -u ${HOST_UID} agent \
    && mkdir -p /home/agent/.claude \
    && chown agent:agent /home/agent/.claude
USER agent

# Bake execution-specific instructions into the image.
# CC reads ~/.claude/CLAUDE.md as user-level instructions, so Execution Claude
# sees the safety rules (no push, no commit, output to agent_log/).
# NOTE: When the dispatcher uses credential-file auth, it mounts the host's
# ~/.claude over this directory (read-only), hiding this baked-in CLAUDE.md.
# In that case, execution rules come only from the task prompt.
COPY --chown=agent:agent docker/CLAUDE.md /home/agent/.claude/CLAUDE.md

WORKDIR /workspace
