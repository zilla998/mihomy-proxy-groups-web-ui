#!/bin/sh
# Pick the appropriate Caddyfile based on whether basic auth env vars are set.
# WEBUI_USER and WEBUI_PASSWORD_HASH must both be non-empty to enable auth.

set -eu

CADDY_BIN=/usr/bin/caddy
CFG_NOAUTH=/etc/caddy/Caddyfile
CFG_AUTH=/etc/caddy/Caddyfile.auth

HAS_USER=
HAS_HASH=
[ -n "${WEBUI_USER:-}" ] && HAS_USER=1
[ -n "${WEBUI_PASSWORD_HASH:-}" ] && HAS_HASH=1

if [ -n "$HAS_USER" ] && [ -n "$HAS_HASH" ]; then
    echo "[start-caddy] basic auth enabled (user=${WEBUI_USER})" >&2
    exec "$CADDY_BIN" run --config "$CFG_AUTH" --adapter caddyfile
fi

# Refuse to fail open: a misnamed/blank password var on a router-mutating UI
# must abort startup so the operator notices, not silently serve unauthed.
if [ -n "$HAS_USER" ] || [ -n "$HAS_HASH" ]; then
    echo "[start-caddy] ERROR: WEBUI_USER and WEBUI_PASSWORD_HASH must both be set or both unset" >&2
    exit 1
fi

echo "[start-caddy] basic auth disabled" >&2
exec "$CADDY_BIN" run --config "$CFG_NOAUTH" --adapter caddyfile
