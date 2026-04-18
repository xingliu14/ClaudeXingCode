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

# Use Hetzner's mirror (HTTP — ca-certificates not yet installed so HTTPS can't be verified).
RUN sed -i \
    -e 's|http://archive.ubuntu.com/ubuntu|http://mirror.hetzner.com/ubuntu/packages|g' \
    -e 's|http://security.ubuntu.com/ubuntu|http://mirror.hetzner.com/ubuntu/security|g' \
    /etc/apt/sources.list

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
# Install via binary tarball; nodesource apt repo can conflict with Ubuntu's nodejs 12 package.
RUN curl -fsSL https://nodejs.org/dist/v20.19.0/node-v20.19.0-linux-x64.tar.xz \
    | tar -xJ -C /usr/local --strip-components=1

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Non-root user — Claude Code refuses --dangerously-skip-permissions as root.
# HOST_UID defaults to 1000 (standard Linux first-user UID).
# Override at build time to match the host user: --build-arg HOST_UID=$(id -u)
# macOS first-user is typically 501; Linux first-user is typically 1000.
ARG HOST_UID=1000
RUN useradd -m -s /bin/bash -u ${HOST_UID} agent \
    && mkdir -p /home/agent/.claude \
    && chown agent:agent /home/agent/.claude

# Entrypoint wires up gh as the git credential helper at container start,
# enabling `git push` over HTTPS when GH_TOKEN is injected by the dispatcher.
# Must be copied and chmod'd before USER agent (needs root to write to /).
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER agent

# Bake git config for the agent user — always use 'main' as the default branch.
RUN git config --global init.defaultBranch main \
    && git config --global push.autoSetupRemote true \
    && git config --global user.name "Ralph Agent" \
    && git config --global user.email "agent@ralph-loop"

# Bake execution-specific instructions into the image.
# CC reads ~/.claude/CLAUDE.md as user-level instructions, so Execution Claude
# sees the safety rules (no push, no commit, output to agent_log/).
# NOTE: When the dispatcher uses credential-file auth, it mounts the host's
# ~/.claude over this directory (read-only), hiding this baked-in CLAUDE.md.
# In that case, execution rules come only from the task prompt.
COPY --chown=agent:agent docker/CLAUDE.md /home/agent/.claude/CLAUDE.md

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
