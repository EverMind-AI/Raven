# syntax=docker/dockerfile:1.7

# Raven in one image: the Python engine, the built web page, and the nginx that
# serves it.
#
# Why one image and not two. The engine binds its HTTP surface to 127.0.0.1
# (raven/cli/_gateway_page.py and raven/cli/serve_commands.py both call
# `web.TCPSite(runner, "127.0.0.1", port)`), so a reverse proxy in a second
# container has no address to reach it on. nginx therefore shares the engine's
# network namespace by living beside it, which is the same answer ragflow's
# image gives to the same shape. Splitting the two into separate services needs
# a bind address the engine does not offer yet.
#
# Build:
#   docker build -t raven:local .
#   docker build -t raven:local --build-arg RAVEN_EXTRAS="channels,tools,sandbox" .
#
# Run: see docker/docker-compose.yml.

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

# --- the page ---------------------------------------------------------------
# `make build-ui` in a stage: vite emits the island bundle, then ui-web/build.py
# assembles dist/index.html around it. build.py is stdlib-only but it is the
# second half of one build, so python3 comes along rather than the page being
# split across two stages.
FROM node:${NODE_VERSION}-bookworm-slim AS web

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY ui-web/package.json ui-web/package-lock.json ./ui-web/
RUN --mount=type=cache,target=/root/.npm npm ci --prefix ui-web

# i18n/messages.json is the catalogue build.py inlines; it is shared with the
# TUI and lives at the repo root, not under ui-web.
COPY ui-web ./ui-web
COPY i18n ./i18n
RUN npm run --prefix ui-web build && python3 ui-web/build.py

# --- the engine's dependencies ----------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

# Most of the tree resolves to manylinux wheels, but a compiler keeps a missing
# one a slow build instead of a failed image. Dropped with this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Optional extras baked in. `channels` is what makes the gateway more than a
# web page; `tools` is the readability extractor. `sandbox`, `browser` and
# `eval` are opt-in -- each pulls a payload (boxlite, a browser binary, the
# huggingface client) that a default deployment does not open.
ARG RAVEN_EXTRAS="channels,tools"

# Dependencies before the project, so editing raven/ does not re-resolve the
# tree. uv reads the workspace from the members' manifests, so those three
# files come first too -- copying the members' sources here would put them back
# in the cache key this layer exists to avoid.
COPY pyproject.toml uv.lock README.md hatch_build.py ./
COPY plugins-dist/everos-memory/pyproject.toml ./plugins-dist/everos-memory/
COPY plugins-dist/ppt-engine/pyproject.toml ./plugins-dist/ppt-engine/
COPY plugins-dist/design-engine/pyproject.toml ./plugins-dist/design-engine/
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    flags=""; \
    for extra in $(echo "${RAVEN_EXTRAS}" | tr ',' ' '); do flags="${flags} --extra ${extra}"; done; \
    uv sync --frozen --no-install-project --no-dev ${flags}

# The project itself, editable on purpose: `agents/` and the packaged page are
# discovered beside the package the way a checkout finds them (see
# raven/agent/subagent/vendored_agents.py), so the image needs no wheel-time
# git tree and behaves like the environment the tests run in.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    flags=""; \
    for extra in $(echo "${RAVEN_EXTRAS}" | tr ',' ' '); do flags="${flags} --extra ${extra}"; done; \
    uv sync --frozen --no-dev ${flags}

# The distributables beside the host wheel, discovered through the
# `raven.plugins` entry-point group. everos-memory by default: the gateway
# starts the everos memory server on boot, so without it memory is the one
# capability that silently does nothing. ppt-engine and design-engine are
# opt-in -- between them they add matplotlib, pymupdf and cairo.
ARG RAVEN_PLUGINS="everos-memory"
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    if [ -n "${RAVEN_PLUGINS}" ]; then \
        paths=""; \
        for plugin in $(echo "${RAVEN_PLUGINS}" | tr ',' ' '); do paths="${paths} ./plugins-dist/${plugin}"; done; \
        uv pip install ${paths}; \
    fi

# --- runtime ----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="Raven" \
      org.opencontainers.image.description="AI-native agent: web page, RPC gateway and IM channels in one container." \
      org.opencontainers.image.licenses="Apache-2.0"

# nginx serves the page and proxies the engine. curl is the health check and
# the sign-in mint. git and ca-certificates are for the agent itself: the
# in-container sandbox backend is the container, so agent commands run in this
# filesystem and a workspace is usually a repository.
#
# procps is not optional here. The slim base ships no `ps`, and the everos
# memory server identifies processes with `ps -ww -p <pid> -o command=`
# (raven_everos/server.py). A missing binary is caught and read as "no match",
# so both callers get a wrong answer that looks like a normal one: the lock
# holder reads as absent, so a running server is invisible and a second one is
# started onto a lock it then dies on; and a stop declares STOPPED on its first
# poll and unlinks the pidfile while the process is still draining. `top`,
# `kill` and `free` come with the package, which is what an operator in
# `docker exec` was reaching for.
#
# lsof is the same module's lock-holder probe. Its port lookup falls back to
# /proc/net; the holder lookup has no fallback. ripgrep is what the agent's
# file search runs when it is there -- without it the tool degrades to a Python
# scan of the tree.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gettext-base \
        git \
        lsof \
        nginx \
        procps \
        ripgrep \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

# /data is the single persistence root: HOME and RAVEN_HOME both point into it,
# so config.json, the workspace, sessions, logs and the memory store land under
# one mountable volume.
ENV HOME=/data \
    RAVEN_HOME=/data/.raven \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RAVEN_PAGE_PORT=18792 \
    RAVEN_DIST_ROOT=/app/ui-web/dist

WORKDIR /app

COPY --from=deps /app /app
COPY --from=web /build/ui-web/dist /app/ui-web/dist

COPY docker/nginx/nginx.conf docker/nginx/proxy.conf /etc/nginx/
COPY docker/nginx/raven.conf.template /etc/nginx/templates/raven.conf.template
COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Root, deliberately: nginx as packaged writes its pid and logs under paths a
# non-root user cannot take, and the agent's own tools install into /data. The
# container is the isolation boundary here -- do not also publish the engine's
# port straight to a network you do not trust.
VOLUME ["/data"]
EXPOSE 80

# tini reaps what the agent spawns: the engine launches ACP agents and the
# everos memory server as children, and PID 1 in a shell would leave them.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["run"]
