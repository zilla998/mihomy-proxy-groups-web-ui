"""Unit tests for :mod:`backend.mihomo` — all HTTP traffic mocked via respx."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from backend.mihomo import (
    MihomoClient,
    MihomoClientError,
    MihomoError,
    MihomoTimeout,
)

BASE = "http://mihomo.test:9090"


@pytest.fixture
async def client() -> AsyncIterator[MihomoClient]:
    c = MihomoClient(BASE, timeout=2.0)
    try:
        yield c
    finally:
        await c.aclose()


# --------------------------------------------------------------------- version


@respx.mock
async def test_version_ok(client: MihomoClient) -> None:
    respx.get(f"{BASE}/version").mock(
        return_value=httpx.Response(200, json={"version": "v1.19.24", "meta": True})
    )
    assert await client.version() == {"version": "v1.19.24", "meta": True}


@respx.mock
async def test_version_503(client: MihomoClient) -> None:
    respx.get(f"{BASE}/version").mock(
        return_value=httpx.Response(503, json={"message": "starting"})
    )
    with pytest.raises(MihomoError) as info:
        await client.version()
    assert info.value.status_code == 503
    assert info.value.body == {"message": "starting"}


@respx.mock
async def test_version_timeout(client: MihomoClient) -> None:
    respx.get(f"{BASE}/version").mock(side_effect=httpx.ReadTimeout("read"))
    with pytest.raises(MihomoTimeout):
        await client.version()


@respx.mock
async def test_version_transport_error(client: MihomoClient) -> None:
    respx.get(f"{BASE}/version").mock(side_effect=httpx.ConnectError("conn refused"))
    with pytest.raises(MihomoClientError):
        await client.version()


@respx.mock
async def test_version_non_json(client: MihomoClient) -> None:
    respx.get(f"{BASE}/version").mock(
        return_value=httpx.Response(200, text="not json")
    )
    with pytest.raises(MihomoClientError):
        await client.version()


@respx.mock
async def test_version_non_object(client: MihomoClient) -> None:
    respx.get(f"{BASE}/version").mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(MihomoClientError):
        await client.version()


# ----------------------------------------------------------------- secret auth


@respx.mock
async def test_secret_sent_as_bearer() -> None:
    c = MihomoClient(BASE, secret="topsecret", timeout=2.0)
    try:
        route = respx.get(f"{BASE}/version").mock(
            return_value=httpx.Response(200, json={"version": "v1"})
        )
        await c.version()
        sent = route.calls.last.request
        assert sent.headers.get("authorization") == "Bearer topsecret"
    finally:
        await c.aclose()


@respx.mock
async def test_no_authorization_header_when_secret_empty(client: MihomoClient) -> None:
    route = respx.get(f"{BASE}/version").mock(
        return_value=httpx.Response(200, json={"version": "v1"})
    )
    await client.version()
    sent = route.calls.last.request
    assert "authorization" not in {k.lower() for k in sent.headers.keys()}


# ------------------------------------------------------------- providers_rules


@respx.mock
async def test_providers_rules_ok(client: MihomoClient) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(
            200,
            json={
                "providers": {
                    "geosite-youtube": {
                        "name": "geosite-youtube",
                        "vehicleType": "HTTP",
                        "updatedAt": "2026-05-08T01:23:45Z",
                    },
                    "inline-rule": {
                        "name": "inline-rule",
                        "vehicleType": "Inline",
                        "updatedAt": "",
                    },
                }
            },
        )
    )
    providers = await client.providers_rules()
    assert "geosite-youtube" in providers
    assert providers["geosite-youtube"]["vehicleType"] == "HTTP"


@respx.mock
async def test_providers_rules_unwrapped_envelope(client: MihomoClient) -> None:
    """Some forks may return the providers map at the top level."""
    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(
            200,
            json={
                "geosite-youtube": {
                    "vehicleType": "HTTP",
                    "updatedAt": "2026-05-08T01:23:45Z",
                }
            },
        )
    )
    providers = await client.providers_rules()
    # Without a "providers" key, the response itself is treated as the map.
    assert "geosite-youtube" in providers


@respx.mock
async def test_providers_rules_500(client: MihomoClient) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(MihomoError) as info:
        await client.providers_rules()
    assert info.value.status_code == 500
    assert info.value.body == "boom"


@respx.mock
async def test_providers_rules_non_object(client: MihomoClient) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )
    with pytest.raises(MihomoClientError):
        await client.providers_rules()


@respx.mock
async def test_providers_rules_bad_providers_key(client: MihomoClient) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(200, json={"providers": [1, 2, 3]})
    )
    with pytest.raises(MihomoClientError):
        await client.providers_rules()


# ---------------------------------------------------------------- wait_started


@respx.mock
async def test_wait_started_immediate(client: MihomoClient) -> None:
    respx.get(f"{BASE}/").mock(
        return_value=httpx.Response(200, json={"hello": "clash.meta"})
    )
    body = await client.wait_started(timeout=1.0, poll_interval=0.01)
    assert body.startswith("{")
    assert "clash.meta" in body


@respx.mock
async def test_wait_started_after_connect_errors(client: MihomoClient) -> None:
    """Cold-start sequence: a few ConnectErrors before mihomo's HTTP listener
    binds, then a normal welcome response — must succeed before deadline."""

    respx.get(f"{BASE}/").mock(
        side_effect=[
            httpx.ConnectError("conn refused"),
            httpx.ConnectError("conn refused"),
            httpx.Response(200, json={"hello": "clash.meta"}),
        ]
    )
    body = await client.wait_started(timeout=2.0, poll_interval=0.01)
    assert "clash.meta" in body


@respx.mock
async def test_wait_started_timeout_on_non_json_body(client: MihomoClient) -> None:
    """If something else binds the port (e.g. an HTML 502 from a misconfigured
    proxy) we must keep retrying — root endpoint always returns ``{...}`` when
    it's actually mihomo. On deadline raises with the offending body."""

    respx.get(f"{BASE}/").mock(
        return_value=httpx.Response(200, text="<html>nginx</html>")
    )
    with pytest.raises(MihomoTimeout) as info:
        await client.wait_started(timeout=0.05, poll_interval=0.01)
    assert "non-JSON body" in str(info.value)


@respx.mock
async def test_wait_started_timeout_on_persistent_connect_error(
    client: MihomoClient,
) -> None:
    respx.get(f"{BASE}/").mock(side_effect=httpx.ConnectError("conn refused"))
    with pytest.raises(MihomoTimeout) as info:
        await client.wait_started(timeout=0.05, poll_interval=0.01)
    msg = str(info.value)
    assert "ConnectError" in msg
    assert "/" in msg


@respx.mock
async def test_wait_started_ignores_leading_whitespace(client: MihomoClient) -> None:
    """Some reverse proxies prepend whitespace; lstrip-then-startswith handles it."""
    respx.get(f"{BASE}/").mock(
        return_value=httpx.Response(200, text='   \n{"hello":"clash.meta"}')
    )
    body = await client.wait_started(timeout=1.0, poll_interval=0.01)
    assert body.startswith("{")


@respx.mock
async def test_wait_started_treats_5xx_as_not_ready(client: MihomoClient) -> None:
    """On the cold start mihomo may briefly return 502 from a fronted reverse
    proxy. We retry until either a 2xx-with-JSON arrives or deadline hits."""

    respx.get(f"{BASE}/").mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={"hello": "clash.meta"}),
        ]
    )
    body = await client.wait_started(timeout=2.0, poll_interval=0.01)
    assert "clash.meta" in body


@respx.mock
async def test_wait_started_omits_authorization_when_secret_unset(
    client: MihomoClient,
) -> None:
    """When ``MIHOMO_API_SECRET`` is unset we must not send a bogus Authorization
    header — mihomo without ``secret:`` configured will happily 200 the welcome
    JSON, and stray headers would only confuse log diagnostics."""

    route = respx.get(f"{BASE}/").mock(
        return_value=httpx.Response(200, json={"hello": "clash.meta"})
    )
    await client.wait_started(timeout=1.0, poll_interval=0.01)
    sent = route.calls.last.request
    assert "authorization" not in {k.lower() for k in sent.headers.keys()}


@respx.mock
async def test_wait_started_treats_401_as_listener_up(
    client: MihomoClient,
) -> None:
    """mihomo registers ``GET /`` inside the same chi group that mounts bearer
    auth, so a wrong/missing ``MIHOMO_API_SECRET`` returns 401 — but that proves
    the HTTP listener is up. We must surface this as success so the next
    authenticated call (e.g. ``/providers/rules``) raises the real auth error
    instead of this method burning the full ``WAIT_RUNNING_TIMEOUT``."""

    respx.get(f"{BASE}/").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    body = await client.wait_started(timeout=1.0, poll_interval=0.01)
    assert "401" in body
    assert "auth" in body.lower()


@respx.mock
async def test_wait_started_treats_403_as_listener_up(
    client: MihomoClient,
) -> None:
    """Same rationale as the 401 case — chi auth middleware can also yield 403
    on token-format mismatches. Either status proves the listener is bound."""

    respx.get(f"{BASE}/").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    body = await client.wait_started(timeout=1.0, poll_interval=0.01)
    assert "403" in body


# -------------------------------------------------------- wait_providers_ready


@respx.mock
async def test_wait_providers_ready_immediate(client: MihomoClient) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(
            200,
            json={
                "providers": {
                    "geosite-youtube": {
                        "vehicleType": "HTTP",
                        "updatedAt": "2026-05-08T01:23:45Z",
                    }
                }
            },
        )
    )
    providers = await client.wait_providers_ready(timeout=1.0, poll_interval=0.01)
    assert "geosite-youtube" in providers


@respx.mock
async def test_wait_providers_ready_eventually_filled(
    client: MihomoClient,
) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "providers": {
                        "geosite-youtube": {
                            "vehicleType": "HTTP",
                            "updatedAt": "",
                        }
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "providers": {
                        "geosite-youtube": {
                            "vehicleType": "HTTP",
                            "updatedAt": "2026-05-08T01:23:45Z",
                        }
                    }
                },
            ),
        ]
    )
    providers = await client.wait_providers_ready(timeout=2.0, poll_interval=0.01)
    assert providers["geosite-youtube"]["updatedAt"]


@respx.mock
async def test_wait_providers_ready_pending_timeout(client: MihomoClient) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(
            200,
            json={
                "providers": {
                    "geosite-youtube": {
                        "vehicleType": "HTTP",
                        "updatedAt": "",
                    },
                    "geoip-ru": {
                        "vehicleType": "HTTP",
                        "updatedAt": "",
                    },
                }
            },
        )
    )
    with pytest.raises(MihomoTimeout) as info:
        await client.wait_providers_ready(timeout=0.1, poll_interval=0.01)
    msg = str(info.value)
    assert "geosite-youtube" in msg
    assert "geoip-ru" in msg


@respx.mock
async def test_wait_providers_ready_skips_inline(client: MihomoClient) -> None:
    """Inline providers have no remote source — they must be exempt from the
    ``updatedAt`` check, otherwise we'd block forever on a freshly loaded
    config that has only inline ``rules:`` entries."""

    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(
            200,
            json={
                "providers": {
                    "inline-rules": {
                        "vehicleType": "Inline",
                        "updatedAt": "",
                    }
                }
            },
        )
    )
    providers = await client.wait_providers_ready(timeout=1.0, poll_interval=0.01)
    assert "inline-rules" in providers


@respx.mock
async def test_wait_providers_ready_500_then_ok(client: MihomoClient) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(200, json={"providers": {}}),
        ]
    )
    providers = await client.wait_providers_ready(timeout=2.0, poll_interval=0.01)
    assert providers == {}


@respx.mock
async def test_wait_providers_ready_persistent_500_timeout(
    client: MihomoClient,
) -> None:
    respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(MihomoTimeout) as info:
        await client.wait_providers_ready(timeout=0.05, poll_interval=0.01)
    assert "rule providers not ready" in str(info.value)


@respx.mock
async def test_wait_providers_ready_401_raises_immediately(
    client: MihomoClient,
) -> None:
    """``wait_started`` accepts 401 from ``GET /`` as "listener up" so this
    method can surface auth failures cleanly. A 401 from ``/providers/rules``
    must therefore re-raise as :class:`MihomoError` immediately rather than
    retry until ``MIHOMO_READY_TIMEOUT`` and bury the cause inside a generic
    timeout."""

    route = respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    with pytest.raises(MihomoError) as info:
        await client.wait_providers_ready(timeout=5.0, poll_interval=0.01)
    assert info.value.status_code == 401
    # No retry — must be a single call.
    assert route.call_count == 1


@respx.mock
async def test_wait_providers_ready_403_raises_immediately(
    client: MihomoClient,
) -> None:
    route = respx.get(f"{BASE}/providers/rules").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    with pytest.raises(MihomoError) as info:
        await client.wait_providers_ready(timeout=5.0, poll_interval=0.01)
    assert info.value.status_code == 403
    assert route.call_count == 1


# --------------------------------------------------------------------- misc


async def test_base_url_trailing_slash_stripped() -> None:
    c = MihomoClient(f"{BASE}/", secret="s")
    try:
        assert c.base_url == BASE
    finally:
        await c.aclose()
