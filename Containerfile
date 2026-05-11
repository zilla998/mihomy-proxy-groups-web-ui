# syntax=docker/dockerfile:1.7

# Stage 1 — builder: same base as runtime so the venv's interpreter symlink
# (/usr/bin/python3) is valid in the runtime stage. We install build deps to
# compile any wheels that lack a musllinux build, then drop them.
#
# Caddy >= 2.8 is required: Caddyfile.auth uses the renamed `basic_auth`
# directive (was `basicauth` in 2.7 and earlier). The `caddy:2-alpine` tag
# tracks the latest 2.x and is well past 2.8 at the time of writing.
FROM --platform=$TARGETPLATFORM caddy:2-alpine AS builder

RUN apk add --no-cache \
        python3 \
        py3-pip \
    && apk add --no-cache --virtual .build-deps \
        gcc \
        musl-dev \
        libffi-dev \
        openssl-dev \
        python3-dev \
        make

COPY backend/requirements.txt /tmp/requirements.txt

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Stage 2 — runtime. Same base, only python3/py3-pip installed (no compilers).
FROM --platform=$TARGETPLATFORM caddy:2-alpine

RUN apk add --no-cache \
        python3 \
        supervisor \
        ca-certificates \
        tini

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY backend/ /app/backend/
COPY frontend/ /srv/frontend/
COPY Caddyfile /etc/caddy/Caddyfile
COPY Caddyfile.auth /etc/caddy/Caddyfile.auth
COPY supervisord.conf /etc/supervisor/conf.d/mihomo-webui.conf
COPY start-caddy.sh /usr/local/bin/start-caddy.sh
RUN chmod +x /usr/local/bin/start-caddy.sh

ENV WEBUI_PORT=80 \
    BACKEND_HOST=127.0.0.1 \
    BACKEND_PORT=8000 \
    MIKROTIK_HOST="" \
    MIKROTIK_USER="" \
    MIKROTIK_PASSWORD="" \
    MIKROTIK_VERIFY_TLS=false \
    MIKROTIK_CONTAINER_COMMENT=MihomoProxyRoS \
    MIKROTIK_ENVS_LIST=MihomoProxyRoS \
    WEBUI_USER="" \
    WEBUI_PASSWORD_HASH=""

EXPOSE 80

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/mihomo-webui.conf", "-n"]
