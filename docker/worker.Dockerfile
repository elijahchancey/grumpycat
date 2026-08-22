# Worker base image: Grumpycat + the core tools every engine run needs. Published as
# grumpycat-worker. Deployments build their own image FROM this one and add the toolchains
# and CLIs their repositories' skills use (see client.example.Dockerfile).
FROM python:3.14-slim-bookworm

ARG NODE_MAJOR=22
ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest
ARG GH_VERSION=2.76.2

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 UV_NO_CACHE=1 \
    PATH="/opt/grumpycat/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq openssh-client gnupg xz-utils unzip \
    && rm -rf /var/lib/apt/lists/*

# gh
RUN set -eux; arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
      | tar -xz -C /tmp; \
    install -m 0755 /tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh /usr/local/bin/gh; rm -rf /tmp/gh_*

# node + the two engine CLIs
RUN set -eux; curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -; \
    apt-get install -y --no-install-recommends nodejs; rm -rf /var/lib/apt/lists/*; \
    npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}"; \
    npm cache clean --force

# grumpycat
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /opt/grumpycat/src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN UV_PROJECT_ENVIRONMENT=/opt/grumpycat uv sync --frozen --no-dev --no-editable --extra all \
    && rm -rf /opt/grumpycat/src

# Unprivileged runtime user; the engines refuse to run as root in some modes anyway.
RUN useradd --create-home --uid 10001 grumpycat
USER grumpycat
WORKDIR /home/grumpycat
ENV HOME=/home/grumpycat

ENTRYPOINT ["grumpycat-worker"]
