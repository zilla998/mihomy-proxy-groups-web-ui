"""Environment-driven configuration for the web-ui backend.

Settings are read from process environment via :func:`Settings.from_env`.
All knobs have sensible defaults so the container starts even when the
operator forgets to wire up an env file — only ``MIKROTIK_HOST`` and
credentials are strictly required for live operation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mikrotik_host: str
    mikrotik_user: str
    mikrotik_password: str
    mikrotik_verify_tls: bool
    mikrotik_container_comment: str
    mikrotik_envs_list: str
    mikrotik_timeout: float
    container_wait_timeout: float
    wait_stopped_timeout: float
    wait_running_timeout: float
    run_script_timeout: float
    mihomo_api_url: str
    mihomo_api_secret: str
    mihomo_ready_timeout: float

    @classmethod
    def from_env(cls, env: "dict[str, str] | None" = None) -> "Settings":
        e = env if env is not None else os.environ
        legacy_wait = e.get("CONTAINER_WAIT_TIMEOUT")
        wait_stopped = float(
            e.get("WAIT_STOPPED_TIMEOUT", legacy_wait if legacy_wait is not None else "60")
        )
        wait_running = float(
            e.get("WAIT_RUNNING_TIMEOUT", legacy_wait if legacy_wait is not None else "180")
        )
        return cls(
            mikrotik_host=e.get("MIKROTIK_HOST", "").strip(),
            mikrotik_user=e.get("MIKROTIK_USER", "").strip(),
            mikrotik_password=e.get("MIKROTIK_PASSWORD", ""),
            mikrotik_verify_tls=_truthy(e.get("MIKROTIK_VERIFY_TLS", "false")),
            mikrotik_container_comment=e.get(
                "MIKROTIK_CONTAINER_COMMENT", "MihomoProxyRoS"
            ),
            mikrotik_envs_list=e.get("MIKROTIK_ENVS_LIST", "MihomoProxyRoS"),
            mikrotik_timeout=float(e.get("MIKROTIK_TIMEOUT", "10")),
            container_wait_timeout=max(wait_stopped, wait_running),
            wait_stopped_timeout=wait_stopped,
            wait_running_timeout=wait_running,
            run_script_timeout=float(e.get("RUN_SCRIPT_TIMEOUT", "600")),
            mihomo_api_url=e.get("MIHOMO_API_URL", "").strip(),
            mihomo_api_secret=e.get("MIHOMO_API_SECRET", ""),
            mihomo_ready_timeout=float(e.get("MIHOMO_READY_TIMEOUT", "90")),
        )


def _truthy(s: str) -> bool:
    return str(s).strip().lower() in {"1", "true", "yes", "on", "y"}


_settings: "Settings | None" = None


def get_settings() -> Settings:
    """Lazily load and cache settings from the process environment."""

    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings_cache() -> None:
    """Test helper — drops the memoised settings instance."""

    global _settings
    _settings = None
