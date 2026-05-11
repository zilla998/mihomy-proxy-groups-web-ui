"""Unit tests for :mod:`backend.mikrotik`.

All HTTP traffic is mocked via :mod:`respx`; no live RouterOS is needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from backend.mikrotik import (
    MikrotikClient,
    MikrotikClientError,
    MikrotikError,
    MikrotikTimeout,
)

BASE = "http://router.test"


@pytest.fixture
async def client() -> AsyncIterator[MikrotikClient]:
    c = MikrotikClient(BASE, "admin", "pass", verify_tls=False, timeout=2.0)
    try:
        yield c
    finally:
        await c.aclose()


# --------------------------------------------------------------------- identity


@respx.mock
async def test_system_identity_ok(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/system/identity").mock(
        return_value=httpx.Response(200, json={"name": "MikroTik"})
    )
    assert await client.system_identity() == {"name": "MikroTik"}


@respx.mock
async def test_system_identity_401(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/system/identity").mock(
        return_value=httpx.Response(401, json={"error": 401, "message": "unauthorized"})
    )
    with pytest.raises(MikrotikError) as info:
        await client.system_identity()
    assert info.value.status_code == 401
    assert info.value.body == {"error": 401, "message": "unauthorized"}


@respx.mock
async def test_system_identity_timeout(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/system/identity").mock(
        side_effect=httpx.ReadTimeout("read timeout")
    )
    with pytest.raises(MikrotikTimeout):
        await client.system_identity()


@respx.mock
async def test_transport_error_wrapped(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/system/identity").mock(
        side_effect=httpx.ConnectError("conn refused")
    )
    with pytest.raises(MikrotikClientError):
        await client.system_identity()


# --------------------------------------------------------------------- envs


@respx.mock
async def test_list_envs(client: MikrotikClient) -> None:
    route = respx.get(f"{BASE}/rest/container/envs", params={"list": "MihomoProxyRoS"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*1", "list": "MihomoProxyRoS", "key": "GROUP", "value": "youtube"},
                {".id": "*2", "list": "MihomoProxyRoS", "key": "LINK1", "value": "vless://..."},
            ],
        )
    )
    items = await client.list_envs("MihomoProxyRoS")
    assert route.called
    assert len(items) == 2
    assert items[0]["key"] == "GROUP"


@respx.mock
async def test_list_envs_empty(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/container/envs", params={"list": "X"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    assert await client.list_envs("X") == []


@respx.mock
async def test_find_env_hit(client: MikrotikClient) -> None:
    respx.get(
        f"{BASE}/rest/container/envs", params={"list": "MihomoProxyRoS", "key": "GROUP"}
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{".id": "*1", "list": "MihomoProxyRoS", "key": "GROUP", "value": "youtube"}],
        )
    )
    env = await client.find_env("MihomoProxyRoS", "GROUP")
    assert env is not None
    assert env[".id"] == "*1"
    assert env["value"] == "youtube"


@respx.mock
async def test_find_env_miss(client: MikrotikClient) -> None:
    respx.get(
        f"{BASE}/rest/container/envs", params={"list": "MihomoProxyRoS", "key": "NONE"}
    ).mock(return_value=httpx.Response(200, json=[]))
    assert await client.find_env("MihomoProxyRoS", "NONE") is None


@respx.mock
async def test_add_env(client: MikrotikClient) -> None:
    route = respx.put(f"{BASE}/rest/container/envs").mock(
        return_value=httpx.Response(
            201,
            json={".id": "*9", "list": "MihomoProxyRoS", "key": "TG_GEOSITE", "value": "telegram"},
        )
    )
    result = await client.add_env("MihomoProxyRoS", "TG_GEOSITE", "telegram")
    assert route.called
    assert route.calls.last.request.read() == (
        b'{"list":"MihomoProxyRoS","key":"TG_GEOSITE","value":"telegram"}'
    )
    assert result[".id"] == "*9"


@respx.mock
async def test_add_env_500(client: MikrotikClient) -> None:
    respx.put(f"{BASE}/rest/container/envs").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    with pytest.raises(MikrotikError) as info:
        await client.add_env("L", "K", "V")
    assert info.value.status_code == 500


@respx.mock
async def test_set_env(client: MikrotikClient) -> None:
    route = respx.patch(f"{BASE}/rest/container/envs/*1").mock(
        return_value=httpx.Response(
            200,
            json={".id": "*1", "list": "MihomoProxyRoS", "key": "GROUP", "value": "youtube,telegram"},
        )
    )
    result = await client.set_env("*1", "youtube,telegram")
    assert route.called
    assert result["value"] == "youtube,telegram"


@respx.mock
async def test_set_env_404(client: MikrotikClient) -> None:
    respx.patch(f"{BASE}/rest/container/envs/*missing").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    with pytest.raises(MikrotikError) as info:
        await client.set_env("*missing", "x")
    assert info.value.status_code == 404


@respx.mock
async def test_remove_env(client: MikrotikClient) -> None:
    route = respx.delete(f"{BASE}/rest/container/envs/*1").mock(
        return_value=httpx.Response(204)
    )
    await client.remove_env("*1")
    assert route.called


@respx.mock
async def test_remove_env_timeout(client: MikrotikClient) -> None:
    respx.delete(f"{BASE}/rest/container/envs/*1").mock(
        side_effect=httpx.ReadTimeout("timeout")
    )
    with pytest.raises(MikrotikTimeout):
        await client.remove_env("*1")


# --------------------------------------------------------------------- container


@respx.mock
async def test_find_container_hit(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/container", params={"comment": "MihomoProxyRoS"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    ".id": "*7",
                    "comment": "MihomoProxyRoS",
                    "status": "running",
                }
            ],
        )
    )
    found = await client.find_container("MihomoProxyRoS")
    assert found is not None
    assert found[".id"] == "*7"
    assert found["status"] == "running"


@respx.mock
async def test_find_container_miss(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/container", params={"comment": "Nope"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    assert await client.find_container("Nope") is None


@respx.mock
async def test_find_container_500(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/container", params={"comment": "x"}).mock(
        return_value=httpx.Response(500, text="oops")
    )
    with pytest.raises(MikrotikError) as info:
        await client.find_container("x")
    assert info.value.status_code == 500
    assert info.value.body == "oops"


@respx.mock
async def test_stop_container(client: MikrotikClient) -> None:
    route = respx.post(f"{BASE}/rest/container/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    await client.stop_container("*7")
    assert route.called
    assert route.calls.last.request.read() == b'{".id":"*7"}'


@respx.mock
async def test_start_container(client: MikrotikClient) -> None:
    route = respx.post(f"{BASE}/rest/container/start").mock(
        return_value=httpx.Response(200, json={})
    )
    await client.start_container("*7")
    assert route.called
    assert route.calls.last.request.read() == b'{".id":"*7"}'


@respx.mock
async def test_start_container_500(client: MikrotikClient) -> None:
    respx.post(f"{BASE}/rest/container/start").mock(
        return_value=httpx.Response(500, json={"detail": "image missing"})
    )
    with pytest.raises(MikrotikError):
        await client.start_container("*7")


@respx.mock
async def test_wait_container_status_immediate(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(
        return_value=httpx.Response(
            200, json=[{".id": "*7", "status": "stopped"}]
        )
    )
    container = await client.wait_container_status(
        "*7", "stopped", timeout=1.0, poll_interval=0.01
    )
    assert container["status"] == "stopped"


@respx.mock
async def test_wait_container_status_eventual(client: MikrotikClient) -> None:
    responses = [
        httpx.Response(200, json=[{".id": "*7", "status": "running"}]),
        httpx.Response(200, json=[{".id": "*7", "status": "running"}]),
        httpx.Response(200, json=[{".id": "*7", "status": "stopped"}]),
    ]
    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(
        side_effect=responses
    )
    container = await client.wait_container_status(
        "*7", "stopped", timeout=2.0, poll_interval=0.01
    )
    assert container["status"] == "stopped"


@respx.mock
async def test_wait_container_status_timeout(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(
        return_value=httpx.Response(200, json=[{".id": "*7", "status": "running"}])
    )
    with pytest.raises(MikrotikTimeout):
        await client.wait_container_status(
            "*7", "stopped", timeout=0.05, poll_interval=0.01
        )


@respx.mock
async def test_wait_container_status_stopped_missing_field(client: MikrotikClient) -> None:
    """RouterOS sometimes drops the `status` field entirely once stopped."""
    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(
        return_value=httpx.Response(200, json=[{".id": "*7"}])
    )
    container = await client.wait_container_status(
        "*7", "stopped", timeout=1.0, poll_interval=0.01
    )
    assert container[".id"] == "*7"


@respx.mock
async def test_wait_container_status_stopped_empty_string(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(
        return_value=httpx.Response(200, json=[{".id": "*7", "status": ""}])
    )
    container = await client.wait_container_status(
        "*7", "stopped", timeout=1.0, poll_interval=0.01
    )
    assert container[".id"] == "*7"


@respx.mock
async def test_wait_container_status_stopped_null(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(
        return_value=httpx.Response(200, json=[{".id": "*7", "status": None}])
    )
    container = await client.wait_container_status(
        "*7", "stopped", timeout=1.0, poll_interval=0.01
    )
    assert container[".id"] == "*7"


@respx.mock
async def test_wait_container_status_running_not_matched_by_empty(
    client: MikrotikClient,
) -> None:
    """Regression: empty/None status must NOT satisfy a wait for `running`."""
    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(
        return_value=httpx.Response(200, json=[{".id": "*7"}])
    )
    with pytest.raises(MikrotikTimeout) as info:
        await client.wait_container_status(
            "*7", "running", timeout=0.05, poll_interval=0.01
        )
    # The error message should report `<empty>` rather than `None`.
    assert "<empty>" in str(info.value)


@respx.mock
async def test_wait_container_status_records_history(
    client: MikrotikClient,
) -> None:
    """Timeout error must include the full sequence of unique statuses."""
    statuses: list[dict[str, Any]] = [
        {".id": "*7"},  # status missing
        {".id": "*7", "status": "starting"},
        {".id": "*7", "status": "starting"},  # duplicate, must be deduped
        {".id": "*7", "status": "stopped"},
    ]
    state = {"i": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        idx = min(state["i"], len(statuses) - 1)
        state["i"] += 1
        return httpx.Response(200, json=[statuses[idx]])

    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(side_effect=respond)
    with pytest.raises(MikrotikTimeout) as info:
        await client.wait_container_status(
            "*7", "running", timeout=0.05, poll_interval=0.005
        )
    msg = str(info.value)
    assert "observed:" in msg
    # Unique transitions: <empty> → starting → stopped (duplicates deduped).
    assert msg.count("<empty>") >= 1
    assert "starting" in msg
    assert "stopped" in msg
    # Compact "<label>@<offset>s" formatting.
    assert "@0.0s" in msg
    # Backward-compat: `last status=` field must still be present.
    assert "last status=" in msg


@respx.mock
async def test_wait_container_status_records_missing_when_container_disappears(
    client: MikrotikClient,
) -> None:
    """If the container record vanishes mid-wait, the timeout error must show
    ``<missing>`` in both the ``observed:`` trace and the ``last status=``
    field — otherwise the operator sees a stale status and can't tell that
    RouterOS dropped the record."""

    statuses: list[list[dict[str, Any]]] = [
        [{".id": "*7", "status": "starting"}],
        [],  # container disappeared and stays missing
    ]
    state = {"i": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        idx = min(state["i"], len(statuses) - 1)
        state["i"] += 1
        return httpx.Response(200, json=statuses[idx])

    respx.get(f"{BASE}/rest/container", params={".id": "*7"}).mock(side_effect=respond)
    with pytest.raises(MikrotikTimeout) as info:
        await client.wait_container_status(
            "*7", "running", timeout=0.05, poll_interval=0.005
        )
    msg = str(info.value)
    assert "<missing>" in msg
    assert "last status=<missing>" in msg
    assert "starting" in msg


# --------------------------------------------------------------------- DNS


@respx.mock
async def test_flush_dns_cache(client: MikrotikClient) -> None:
    route = respx.post(f"{BASE}/rest/ip/dns/cache/flush").mock(
        return_value=httpx.Response(200, json={})
    )
    await client.flush_dns_cache()
    assert route.called


@respx.mock
async def test_flush_dns_cache_500(client: MikrotikClient) -> None:
    respx.post(f"{BASE}/rest/ip/dns/cache/flush").mock(
        return_value=httpx.Response(500, json={"detail": "no DNS"})
    )
    with pytest.raises(MikrotikError):
        await client.flush_dns_cache()


# --------------------------------------------------------------------- script


@respx.mock
async def test_script_add(client: MikrotikClient) -> None:
    route = respx.put(f"{BASE}/rest/system/script").mock(
        return_value=httpx.Response(
            201, json={".id": "*42", "name": "fwd-add-tg", "source": "/log info hi"}
        )
    )
    result = await client.script_add("fwd-add-tg", "/log info hi")
    assert route.called
    assert result[".id"] == "*42"


@respx.mock
async def test_script_run(client: MikrotikClient) -> None:
    route = respx.post(f"{BASE}/rest/system/script/run").mock(
        return_value=httpx.Response(200, json={})
    )
    await client.script_run("fwd-add-tg")
    assert route.called
    assert route.calls.last.request.read() == b'{".id":"fwd-add-tg"}'


@respx.mock
async def test_script_run_404(client: MikrotikClient) -> None:
    respx.post(f"{BASE}/rest/system/script/run").mock(
        return_value=httpx.Response(404, json={"detail": "no such script"})
    )
    with pytest.raises(MikrotikError):
        await client.script_run("does-not-exist")


@respx.mock
async def test_script_run_timeout_passes_through_to_httpx(
    client: MikrotikClient,
) -> None:
    """Per-call ``timeout`` must override the client's default; verified by
    inspecting the request extensions where httpx records the effective
    timeout for a given request."""

    route = respx.post(f"{BASE}/rest/system/script/run").mock(
        return_value=httpx.Response(200, json={})
    )
    await client.script_run("*99", timeout=300.0)
    assert route.called
    sent = route.calls.last.request
    timeout_ext = sent.extensions.get("timeout")
    assert timeout_ext is not None
    # All four timeout buckets get the override (httpx normalises a bare
    # float into ``connect``/``read``/``write``/``pool``).
    assert timeout_ext["connect"] == 300.0
    assert timeout_ext["read"] == 300.0


@respx.mock
async def test_script_run_default_uses_client_timeout(
    client: MikrotikClient,
) -> None:
    """Without an explicit ``timeout`` the client default (2.0s in the fixture)
    must remain in force — otherwise other long-running calls might silently
    inherit a ``script_run`` override."""

    route = respx.post(f"{BASE}/rest/system/script/run").mock(
        return_value=httpx.Response(200, json={})
    )
    await client.script_run("*99")
    assert route.called
    sent = route.calls.last.request
    timeout_ext = sent.extensions.get("timeout")
    assert timeout_ext is not None
    assert timeout_ext["read"] == 2.0


@respx.mock
async def test_script_run_read_timeout_raises_mikrotik_timeout(
    client: MikrotikClient,
) -> None:
    """An httpx ReadTimeout from the run endpoint must surface as
    MikrotikTimeout so the workflow can categorise it as a transport-level
    failure (and not generic MikrotikClientError)."""

    respx.post(f"{BASE}/rest/system/script/run").mock(
        side_effect=httpx.ReadTimeout("read timeout")
    )
    with pytest.raises(MikrotikTimeout):
        await client.script_run("*99", timeout=1.0)


@respx.mock
async def test_script_remove(client: MikrotikClient) -> None:
    route = respx.delete(f"{BASE}/rest/system/script/*42").mock(
        return_value=httpx.Response(204)
    )
    await client.script_remove("*42")
    assert route.called


@respx.mock
async def test_script_remove_timeout(client: MikrotikClient) -> None:
    respx.delete(f"{BASE}/rest/system/script/*42").mock(
        side_effect=httpx.ReadTimeout("timeout")
    )
    with pytest.raises(MikrotikTimeout):
        await client.script_remove("*42")


# --------------------------------------------------------------------- misc


async def test_base_url_trailing_slash_stripped() -> None:
    c = MikrotikClient("http://router.test/", "u", "p")
    try:
        assert c.base_url == "http://router.test"
    finally:
        await c.aclose()


@respx.mock
async def test_error_with_text_body(client: MikrotikClient) -> None:
    respx.get(f"{BASE}/rest/system/identity").mock(
        return_value=httpx.Response(503, text="overloaded")
    )
    with pytest.raises(MikrotikError) as info:
        await client.system_identity()
    assert info.value.body == "overloaded"


# Sanity check: avoid an unused-import warning for asyncio in environments
# where the fixture path isn't exercised.
_ = asyncio
