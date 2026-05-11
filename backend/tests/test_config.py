"""Tests for :class:`backend.config.Settings.from_env`.

These cover the new mihomo-related fields (``mihomo_api_url``,
``mihomo_api_secret``, ``mihomo_ready_timeout``) plus a sanity check for the
existing fields so we do not silently lose a default when the dataclass grows.
"""

from __future__ import annotations

from pathlib import Path

from backend.config import Settings


def test_settings_from_env_defaults_blank() -> None:
    s = Settings.from_env({})
    assert s.mikrotik_host == ""
    # mihomo is opt-in — empty URL means the feature is off.
    assert s.mihomo_api_url == ""
    assert s.mihomo_api_secret == ""
    assert s.mihomo_ready_timeout == 90.0
    # run_script_timeout default is much larger than mikrotik_timeout because
    # an imported .rsc with hundreds of /ip dns static lines (e.g. amazon.rsc)
    # takes well over a minute on RouterOS — the standard 10s would kill it.
    assert s.run_script_timeout == 600.0


def test_settings_from_env_reads_run_script_timeout() -> None:
    s = Settings.from_env({"RUN_SCRIPT_TIMEOUT": "120"})
    assert s.run_script_timeout == 120.0


def test_settings_from_env_reads_mihomo_url() -> None:
    s = Settings.from_env(
        {
            "MIHOMO_API_URL": "  http://mihomo.test:9090  ",
            "MIHOMO_API_SECRET": "topsecret",
            "MIHOMO_READY_TIMEOUT": "30.5",
        }
    )
    # base URL is stripped of leading/trailing whitespace so callers don't have
    # to defensively .strip() it on every use.
    assert s.mihomo_api_url == "http://mihomo.test:9090"
    assert s.mihomo_api_secret == "topsecret"
    assert s.mihomo_ready_timeout == 30.5


def test_settings_from_env_keeps_existing_fields() -> None:
    s = Settings.from_env(
        {
            "MIKROTIK_HOST": "http://router",
            "MIKROTIK_USER": "admin",
            "MIKROTIK_PASSWORD": "pw",
            "MIKROTIK_VERIFY_TLS": "true",
            "MIKROTIK_TIMEOUT": "5",
            "CONTAINER_WAIT_TIMEOUT": "30",
        }
    )
    assert s.mikrotik_host == "http://router"
    assert s.mikrotik_verify_tls is True
    assert s.mikrotik_timeout == 5.0
    # container_wait_timeout now mirrors max(wait_stopped, wait_running);
    # when CONTAINER_WAIT_TIMEOUT is the only knob set, both halves get the
    # same value, so the legacy field equals it.
    assert s.container_wait_timeout == 30.0
    # mihomo defaults still apply when only the existing fields are set.
    assert s.mihomo_api_url == ""
    assert s.mihomo_ready_timeout == 90.0


def test_wait_timeouts_default_split() -> None:
    s = Settings.from_env({})
    # cold start needs more than the legacy 60s default — the new dedicated
    # WAIT_RUNNING_TIMEOUT defaults to 180s while WAIT_STOPPED_TIMEOUT keeps
    # the old 60s.
    assert s.wait_stopped_timeout == 60.0
    assert s.wait_running_timeout == 180.0
    assert s.container_wait_timeout == 180.0


def test_wait_timeouts_legacy_container_wait_propagates() -> None:
    s = Settings.from_env({"CONTAINER_WAIT_TIMEOUT": "45"})
    # legacy env still works: it applies to both halves so existing operator
    # configs keep their intent.
    assert s.wait_stopped_timeout == 45.0
    assert s.wait_running_timeout == 45.0
    assert s.container_wait_timeout == 45.0


def test_wait_timeouts_explicit_values_take_precedence() -> None:
    s = Settings.from_env(
        {
            "CONTAINER_WAIT_TIMEOUT": "30",
            "WAIT_STOPPED_TIMEOUT": "20",
            "WAIT_RUNNING_TIMEOUT": "240",
        }
    )
    # explicit per-half values win over the legacy fallback.
    assert s.wait_stopped_timeout == 20.0
    assert s.wait_running_timeout == 240.0
    # container_wait_timeout = max so drain_timeout based on it stays generous.
    assert s.container_wait_timeout == 240.0


def test_wait_timeouts_partial_override() -> None:
    # only WAIT_STOPPED_TIMEOUT is overridden — the running half falls back to
    # CONTAINER_WAIT_TIMEOUT, not the hard-coded 180s default.
    s = Settings.from_env(
        {"CONTAINER_WAIT_TIMEOUT": "90", "WAIT_STOPPED_TIMEOUT": "15"}
    )
    assert s.wait_stopped_timeout == 15.0
    assert s.wait_running_timeout == 90.0


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal `KEY=VALUE` parser matching the systemd EnvironmentFile rules.

    Skips blank lines and `#`-comments; does NOT do shell expansion (the
    example file explicitly documents that systemd reads values verbatim).
    """

    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def test_env_example_file_wait_timeouts_parse_as_expected() -> None:
    # the documented example file ships the new split timeouts; if we ever
    # rename the env vars, this test will catch the README/example drift.
    example = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "mihomo-webui.env.example"
    )
    env = _parse_env_file(example)
    # the legacy CONTAINER_WAIT_TIMEOUT should be commented out (only
    # mentioned, not active) so the new defaults govern.
    assert "CONTAINER_WAIT_TIMEOUT" not in env
    assert env["WAIT_STOPPED_TIMEOUT"] == "60"
    assert env["WAIT_RUNNING_TIMEOUT"] == "180"
    s = Settings.from_env(env)
    assert s.wait_stopped_timeout == 60.0
    assert s.wait_running_timeout == 180.0
    # drain_timeout in app.py is computed from wait_stopped + wait_running;
    # container_wait_timeout (= max) should still be the larger half.
    assert s.container_wait_timeout == 180.0
