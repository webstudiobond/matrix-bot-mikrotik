FROM ghcr.io/astral-sh/uv:0.12.3-python3.14-dhi@sha256:b2609059244cd2fbb343a4cf92ef1f4dc2c904bc430c86c18640d6fa24a366a7 AS uv-bin

FROM ghcr.io/underhax/matrix-cli:0.4.1@sha256:db4bc5a7182490acd56b7170c54d507e8b229a2d8e580e876b282720419c59d0

USER 0

# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv-bin /uv /usr/local/bin/uv

WORKDIR /home/bot/app
COPY pyproject.toml uv.lock ./

RUN UV_PROJECT_ENVIRONMENT=/home/bot/.venv uv sync \
        --frozen \
        --no-dev \
        --no-cache \
    && chown -R bot:bot /home/bot/.venv

COPY bot/bot.py /home/bot/app/bot.py
COPY docker/mikrotik_entrypoint.sh /usr/local/bin/mikrotik_entrypoint.sh

RUN chmod 0755 /usr/local/bin/mikrotik_entrypoint.sh \
    && chown -R bot:bot /home/bot/app

RUN mkdir -p /home/bot/config && chown bot:bot /home/bot/config

USER 10001

ENV VIRTUAL_ENV=/home/bot/.venv
ENV PATH="/home/bot/.venv/bin:$PATH"

ENTRYPOINT ["/usr/local/bin/mikrotik_entrypoint.sh"]
