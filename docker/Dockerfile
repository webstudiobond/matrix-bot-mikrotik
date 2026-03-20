# docker/Dockerfile — MikroTik Matrix Bot
#
# Build strategy
# --------------
# Stage 1 (uv-bin): Named alias for the upstream uv distroless image, used
#   purely as a COPY source for the static uv binary.  Adds zero bytes to
#   the final image.
# Stage 2 (final): Extends the gateway base image.  Installs Python,
#   copies the uv binary, creates the venv, and syncs dependencies in a
#   single chained RUN so no intermediate build tools survive into a
#   separate layer.  No pip, no setuptools, no wheel in the final image.
#
# Dependency installation runs before application source is copied so that
# code-only changes do not invalidate the (heavier) uv sync layer.
#
# Build context is the repository root (set in docker-compose files).
# All COPY paths are relative to that root.

# --- Stage 1: uv binary source -------------------------------------------
# hadolint ignore=DL3007
FROM ghcr.io/astral-sh/uv:latest AS uv-bin

# --- Stage 2: final runtime image ----------------------------------------
# hadolint ignore=DL3007
FROM ghcr.io/webstudiobond/matrix-commander-rs-gateway:latest

USER root

# Python interpreter only — no pip, no setuptools, no wheel.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# uv: single statically-linked binary, zero additional apt dependencies.
COPY --from=uv-bin /uv /usr/local/bin/uv

# Copy dependency manifests before application source to exploit layer cache:
# uv sync reruns only when pyproject.toml or uv.lock change.
WORKDIR /home/bot/app
COPY bot/pyproject.toml bot/uv.lock ./

# Sync dependencies into a fixed venv path.
#   UV_PROJECT_ENVIRONMENT : instructs uv to create and use the venv at this
#                            path instead of the default .venv relative to
#                            WORKDIR.  This is the correct way to control the
#                            venv location — VIRTUAL_ENV is ignored by uv when
#                            a pyproject.toml is present.
#   --frozen               : lockfile is authoritative; fails loudly if stale.
#   --no-dev               : exclude test/lint extras from the runtime image.
#   --no-cache             : skip the uv HTTP cache — build envs are ephemeral.
RUN UV_PROJECT_ENVIRONMENT=/home/bot/.venv uv sync \
        --frozen \
        --no-dev \
        --no-cache \
    && chown -R bot:bot /home/bot/.venv

# Application source — copied after venv to keep code-change rebuilds fast.
COPY bot/bot.py /home/bot/app/bot.py

# Process supervisor entrypoint.
COPY docker/mikrotik_entrypoint.sh /usr/local/bin/mikrotik_entrypoint.sh

RUN chmod 0755 /usr/local/bin/mikrotik_entrypoint.sh \
    && chown -R bot:bot /home/bot/app

# Runtime config directory — config.yaml is bind-mounted here read-only.
RUN mkdir -p /home/bot/config && chown bot:bot /home/bot/config

USER bot

# VIRTUAL_ENV + PATH ensure both `python3` and any direct venv binary
# resolve to the uv-managed environment without project-root discovery.
ENV VIRTUAL_ENV=/home/bot/.venv
ENV PATH="/home/bot/.venv/bin:$PATH"

ENTRYPOINT ["/usr/local/bin/mikrotik_entrypoint.sh"]
