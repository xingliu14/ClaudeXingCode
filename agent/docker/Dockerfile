FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (LTS)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Non-root user — Claude Code refuses --dangerously-skip-permissions as root
ARG HOST_UID=501
RUN useradd -m -s /bin/bash -u ${HOST_UID} agent \
    && mkdir -p /home/agent/.claude \
    && chown agent:agent /home/agent/.claude
USER agent

# This image is used as a sandboxed execution environment.
# The dispatcher spawns `docker run --rm claude-agent:latest claude -p ...`
# No agent source is needed inside — only the Claude CLI.
WORKDIR /workspace
