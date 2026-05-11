"""Tests for the FastAPI app — JSON endpoints and the SSE streams.

The app is constructed via :func:`backend.app.create_app` with a hand-rolled
:class:`Settings` so no environment variables affect the run. Outbound httpx
calls (to MikroTik and GitHub) are intercepted via respx; inbound traffic
goes through ``httpx.ASGITransport`` so we exercise the real FastAPI
routing/serialisation path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

from backend.app import create_app
from backend.config import Settings


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "mikrotik_host": "http://router.test",
        "mikrotik_user": "admin",
        "mikrotik_password": "pw",
        "mikrotik_verify_tls": False,
        "mikrotik_container_comment": "MihomoProxyRoS",
        "mikrotik_envs_list": "MihomoProxyRoS",
        "mikrotik_timeout": 2.0,
        "container_wait_timeout": 5.0,
        "wait_stopped_timeout": 5.0,
        "wait_running_timeout": 5.0,
        "run_script_timeout": 600.0,
        "mihomo_api_url": "",
        "mihomo_api_secret": "",
        "mihomo_ready_timeout": 5.0,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10.0,
    )


# ----------------------------------------------------------------- /api/health


@respx.mock
async def test_health_ok(app) -> None:
    respx.get("http://router.test/rest/system/identity").mock(
        return_value=httpx.Response(200, json={"name": "MikroTik"})
    )
    async with _client(app) as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "identity": {"name": "MikroTik"}}


@respx.mock
async def test_health_502_on_mikrotik_error(app) -> None:
    respx.get("http://router.test/rest/system/identity").mock(
        return_value=httpx.Response(401, text="bad creds")
    )
    async with _client(app) as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 502
    body = r.json()
    assert body["ok"] is False
    assert "401" in body["error"]


async def test_health_503_when_unconfigured() -> None:
    app = create_app(_make_settings(mikrotik_host=""))
    async with _client(app) as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 503
    assert r.json()["ok"] is False


@respx.mock
async def test_health_omits_mihomo_when_disabled(app) -> None:
    """No MIHOMO_API_URL ⇒ no mihomo key in /api/health response.

    The /api/health endpoint is the UI banner's primary signal — it must keep
    its existing shape when the optional mihomo probe is not configured."""

    respx.get("http://router.test/rest/system/identity").mock(
        return_value=httpx.Response(200, json={"name": "MikroTik"})
    )
    async with _client(app) as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "mihomo" not in body


@respx.mock
async def test_health_includes_mihomo_when_enabled() -> None:
    app = create_app(_make_settings(mihomo_api_url="http://mihomo.test:9090"))
    respx.get("http://router.test/rest/system/identity").mock(
        return_value=httpx.Response(200, json={"name": "MikroTik"})
    )
    respx.get("http://mihomo.test:9090/version").mock(
        return_value=httpx.Response(200, json={"version": "v1.19.24", "meta": True})
    )
    async with _client(app) as ac:
        r = await ac.get("/api/health")
    # Soft-signal: even though mihomo is configured the top-level status
    # remains 200 — UI uses body.mihomo.ok to decide whether to show the
    # secondary indicator.
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mihomo"] == {
        "ok": True,
        "version": {"version": "v1.19.24", "meta": True},
    }


@respx.mock
async def test_health_mihomo_failure_is_soft() -> None:
    """A 503 from mihomo /version must not downgrade the overall response."""

    app = create_app(_make_settings(mihomo_api_url="http://mihomo.test:9090"))
    respx.get("http://router.test/rest/system/identity").mock(
        return_value=httpx.Response(200, json={"name": "MikroTik"})
    )
    respx.get("http://mihomo.test:9090/version").mock(
        return_value=httpx.Response(503, json={"message": "starting"})
    )
    async with _client(app) as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mihomo"]["ok"] is False
    assert "503" in body["mihomo"]["error"]


@respx.mock
async def test_health_mihomo_secret_sent_as_bearer() -> None:
    app = create_app(
        _make_settings(
            mihomo_api_url="http://mihomo.test:9090",
            mihomo_api_secret="topsecret",
        )
    )
    respx.get("http://router.test/rest/system/identity").mock(
        return_value=httpx.Response(200, json={"name": "MikroTik"})
    )
    route = respx.get("http://mihomo.test:9090/version").mock(
        return_value=httpx.Response(200, json={"version": "v1"})
    )
    async with _client(app) as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 200
    sent = route.calls.last.request
    assert sent.headers.get("authorization") == "Bearer topsecret"


@respx.mock
async def test_health_mihomo_timeout_is_soft() -> None:
    """A transport timeout from mihomo /version (MihomoTimeout, a
    MihomoClientError subclass) must be caught by the same handler that
    surfaces non-2xx responses — otherwise the unauthenticated 60s timeout
    typical of an unreachable mihomo would propagate as a 500 from
    /api/health and break the UI banner."""

    app = create_app(_make_settings(mihomo_api_url="http://mihomo.test:9090"))
    respx.get("http://router.test/rest/system/identity").mock(
        return_value=httpx.Response(200, json={"name": "MikroTik"})
    )
    respx.get("http://mihomo.test:9090/version").mock(
        side_effect=httpx.ConnectError("conn refused")
    )
    async with _client(app) as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mihomo"]["ok"] is False
    assert "conn refused" in body["mihomo"]["error"]


# ---------------------------------------------------------- /api/groups/current


@respx.mock
async def test_groups_current(app, settings: Settings) -> None:
    # Envs surfaced here must match what RemoveGroupWorkflow would actually
    # delete — including TYPE/USE/AS/IPCIDR/PROXIES added by script21.rsc,
    # not just the 5 rule-kind suffixes the UI offers as Add options.
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": settings.mikrotik_envs_list},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*1", "key": "GROUP", "value": "telegram,youtube"},
                {".id": "*2", "key": "TELEGRAM_GEOSITE", "value": "telegram"},
                {".id": "*3", "key": "TELEGRAM_TYPE", "value": "select"},
                {".id": "*4", "key": "TELEGRAM_AS", "value": "AS62041"},
                {".id": "*5", "key": "YOUTUBE_GEOSITE", "value": "youtube"},
                {".id": "*6", "key": "RANDOM_VAR", "value": "x"},
                {".id": "*7", "key": "_GEOSITE", "value": "ignored"},
            ],
        )
    )
    async with _client(app) as ac:
        r = await ac.get("/api/groups/current")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["groups"] == ["telegram", "youtube"]
    keys = sorted(e["key"] for e in body["rule_envs"])
    assert keys == [
        "TELEGRAM_AS",
        "TELEGRAM_GEOSITE",
        "TELEGRAM_TYPE",
        "YOUTUBE_GEOSITE",
    ]


@respx.mock
async def test_groups_current_excludes_system_envs_sharing_suffixes(
    app, settings: Settings
) -> None:
    # System envs like HEALTHCHECK_INTERVAL, EXTERNAL_UI_URL, GROUP_TYPE share
    # the GROUP_ENV_SUFFIXES tail (_INTERVAL, _URL, _TYPE) but their prefix
    # (HEALTHCHECK, EXTERNAL_UI, GROUP) doesn't map to a registered group.
    # They must not appear in rule_envs.
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": settings.mikrotik_envs_list},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*1", "key": "GROUP", "value": "telegram"},
                {".id": "*2", "key": "TELEGRAM_GEOSITE", "value": "telegram"},
                {".id": "*3", "key": "HEALTHCHECK_INTERVAL", "value": "120"},
                {".id": "*4", "key": "HEALTHCHECK_URL", "value": "https://x"},
                {".id": "*5", "key": "EXTERNAL_UI_URL", "value": "https://y"},
                {".id": "*6", "key": "GROUP_TYPE", "value": "select"},
                {".id": "*7", "key": "SUB_LINK_INTERVAL", "value": "3600"},
            ],
        )
    )
    async with _client(app) as ac:
        r = await ac.get("/api/groups/current")
    assert r.status_code == 200
    body = r.json()
    assert body["groups"] == ["telegram"]
    keys = sorted(e["key"] for e in body["rule_envs"])
    assert keys == ["TELEGRAM_GEOSITE"]


@respx.mock
async def test_groups_current_matches_longest_suffix_first(
    app, settings: Settings
) -> None:
    # ``TELEGRAM_EXCLUDE_TYPE`` ends with both ``_TYPE`` and
    # ``_EXCLUDE_TYPE``. Longest-first matching must derive prefix
    # ``TELEGRAM`` (a registered group) rather than ``TELEGRAM_EXCLUDE``
    # (which is not). Otherwise the env would be hidden from the UI.
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": settings.mikrotik_envs_list},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*1", "key": "GROUP", "value": "telegram"},
                {".id": "*2", "key": "TELEGRAM_EXCLUDE_TYPE", "value": "select"},
                {".id": "*3", "key": "TELEGRAM_EXCLUDE", "value": "DIRECT"},
                {".id": "*4", "key": "TELEGRAM_TYPE", "value": "select"},
            ],
        )
    )
    async with _client(app) as ac:
        r = await ac.get("/api/groups/current")
    assert r.status_code == 200
    body = r.json()
    keys = sorted(e["key"] for e in body["rule_envs"])
    assert keys == [
        "TELEGRAM_EXCLUDE",
        "TELEGRAM_EXCLUDE_TYPE",
        "TELEGRAM_TYPE",
    ]


@respx.mock
async def test_groups_current_falls_through_to_shorter_suffix(
    app, settings: Settings
) -> None:
    # Group ``main-exclude`` yields valid_prefixes={'MAIN_EXCLUDE'}. Env
    # ``MAIN_EXCLUDE_TYPE`` matches the longest suffix ``_EXCLUDE_TYPE``,
    # leaving prefix ``MAIN`` (not registered). The matcher must fall through
    # to the shorter ``_TYPE`` suffix so prefix becomes ``MAIN_EXCLUDE`` and
    # the env is surfaced. Otherwise a regression breaks UI display for any
    # group whose name ends with EXCLUDE or URL.
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": settings.mikrotik_envs_list},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*1", "key": "GROUP", "value": "main-exclude"},
                {".id": "*2", "key": "MAIN_EXCLUDE_TYPE", "value": "select"},
                {".id": "*3", "key": "MAIN_EXCLUDE_GEOSITE", "value": "x"},
            ],
        )
    )
    async with _client(app) as ac:
        r = await ac.get("/api/groups/current")
    assert r.status_code == 200
    body = r.json()
    assert body["groups"] == ["main-exclude"]
    keys = sorted(e["key"] for e in body["rule_envs"])
    assert keys == ["MAIN_EXCLUDE_GEOSITE", "MAIN_EXCLUDE_TYPE"]


@respx.mock
async def test_groups_current_no_group_env(app, settings: Settings) -> None:
    # When the GROUP env is absent (fresh container), groups must be empty
    # and no rule_envs surfaced even if env keys end with known suffixes.
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": settings.mikrotik_envs_list},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*1", "key": "TELEGRAM_GEOSITE", "value": "telegram"},
                {".id": "*2", "key": "HEALTHCHECK_INTERVAL", "value": "60"},
            ],
        )
    )
    async with _client(app) as ac:
        r = await ac.get("/api/groups/current")
    assert r.status_code == 200
    body = r.json()
    assert body["groups"] == []
    assert body["rule_envs"] == []


@respx.mock
async def test_groups_current_tolerates_null_env_fields(
    app, settings: Settings
) -> None:
    """RouterOS may return env entries with null/missing key or value;
    /api/groups/current must skip them rather than 500."""

    list_name = settings.mikrotik_envs_list
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*1", "key": "GROUP", "value": "telegram"},
                {".id": "*2", "key": None, "value": "ignored"},
                {".id": "*3", "key": "TELEGRAM_GEOSITE", "value": None},
                {".id": "*4", "value": "missing-key"},
            ],
        )
    )
    async with _client(app) as ac:
        r = await ac.get("/api/groups/current")
    assert r.status_code == 200
    body = r.json()
    assert body["groups"] == ["telegram"]
    # Null/missing key entries skipped; entry with null value coerced to "".
    assert {e["key"] for e in body["rule_envs"]} == {"TELEGRAM_GEOSITE"}
    assert body["rule_envs"][0]["value"] == ""


@respx.mock
async def test_groups_current_502_on_error(app) -> None:
    respx.get("http://router.test/rest/container/envs").mock(
        return_value=httpx.Response(500, text="boom")
    )
    async with _client(app) as ac:
        r = await ac.get("/api/groups/current")
    assert r.status_code == 502
    assert r.json()["ok"] is False


# ----------------------------------------------------------- /api/rules/categories


@respx.mock
async def test_rules_categories_geosite(app) -> None:
    respx.get(
        "https://api.github.com/repos/MetaCubeX/meta-rules-dat/git/trees/meta:geo/geosite",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "sha": "abc",
                "truncated": False,
                "tree": [
                    {"path": "youtube.mrs", "type": "blob"},
                    {"path": "telegram.mrs", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ],
            },
        )
    )
    async with _client(app) as ac:
        r = await ac.get("/api/rules/categories", params={"kind": "GEOSITE"})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "ok": True,
        "kind": "GEOSITE",
        "categories": ["telegram", "youtube"],
    }


@respx.mock
async def test_rules_categories_geoip(app) -> None:
    respx.get(
        "https://api.github.com/repos/MetaCubeX/meta-rules-dat/git/trees/meta:geo/geoip",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "sha": "abc",
                "truncated": False,
                "tree": [
                    {"path": "us.mrs", "type": "blob"},
                    {"path": "ru.mrs", "type": "blob"},
                    {"path": "cn.mrs", "type": "blob"},
                ],
            },
        )
    )
    async with _client(app) as ac:
        r = await ac.get("/api/rules/categories", params={"kind": "geoip"})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "ok": True,
        "kind": "GEOIP",
        "categories": ["cn", "ru", "us"],
    }


async def test_rules_categories_invalid_kind(app) -> None:
    async with _client(app) as ac:
        r = await ac.get("/api/rules/categories", params={"kind": "DOMAIN"})
    assert r.status_code == 400


async def test_rules_categories_missing_kind(app) -> None:
    async with _client(app) as ac:
        r = await ac.get("/api/rules/categories")
    # FastAPI surfaces missing required query params as 422.
    assert r.status_code == 422


@respx.mock
async def test_rules_categories_502_on_github_error(app) -> None:
    respx.get(
        "https://api.github.com/repos/MetaCubeX/meta-rules-dat/git/trees/meta:geo/geosite",
    ).mock(return_value=httpx.Response(403, json={"message": "rate limit"}))
    async with _client(app) as ac:
        r = await ac.get("/api/rules/categories", params={"kind": "GEOSITE"})
    assert r.status_code == 502
    assert r.json()["ok"] is False


@respx.mock
async def test_rules_categories_uses_shared_cache(app) -> None:
    """Repeated requests must hit the per-app GithubClient cache, not GitHub.

    Otherwise a single user clicking through the rule-kind dropdown would burn
    through the unauthenticated 60 req/h GitHub quota almost immediately.
    """

    route = respx.get(
        "https://api.github.com/repos/MetaCubeX/meta-rules-dat/git/trees/meta:geo/geosite",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "sha": "abc",
                "truncated": False,
                "tree": [{"path": "youtube.mrs", "type": "blob"}],
            },
        )
    )
    async with _client(app) as ac:
        await ac.get("/api/rules/categories", params={"kind": "GEOSITE"})
        await ac.get("/api/rules/categories", params={"kind": "GEOSITE"})
    assert route.call_count == 1


@respx.mock
async def test_rules_categories_force_refresh_bypasses_cache(app) -> None:
    route = respx.get(
        "https://api.github.com/repos/MetaCubeX/meta-rules-dat/git/trees/meta:geo/geosite",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "sha": "abc",
                "truncated": False,
                "tree": [{"path": "youtube.mrs", "type": "blob"}],
            },
        )
    )
    async with _client(app) as ac:
        await ac.get("/api/rules/categories", params={"kind": "GEOSITE"})
        await ac.get(
            "/api/rules/categories",
            params={"kind": "GEOSITE", "force_refresh": "true"},
        )
    assert route.call_count == 2


# ------------------------------------------------------------ /api/groups/add


@respx.mock
async def test_add_group_sse_stream(app, settings: Settings) -> None:
    list_name = settings.mikrotik_envs_list

    # GROUP env present — set_env path.
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "GROUP"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{".id": "*1", "list": list_name, "key": "GROUP", "value": "youtube"}],
        )
    )
    respx.patch("http://router.test/rest/container/envs/*1").mock(
        return_value=httpx.Response(200, json={})
    )

    # TELEGRAM_GEOSITE missing — add_env path.
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "TELEGRAM_GEOSITE"},
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.put("http://router.test/rest/container/envs").mock(
        return_value=httpx.Response(201, json={".id": "*42"})
    )

    # GitHub fetch — geosite .list source from meta-rules-dat.
    respx.get(
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/telegram.list"
    ).mock(return_value=httpx.Response(200, text="+.t.me\n+.telegram.org\n"))

    # RouterOS script lifecycle.
    respx.put("http://router.test/rest/system/script").mock(
        return_value=httpx.Response(201, json={".id": "*99"})
    )
    respx.post("http://router.test/rest/system/script/run").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.delete("http://router.test/rest/system/script/*99").mock(
        return_value=httpx.Response(204)
    )

    # Container lifecycle.
    respx.get(
        "http://router.test/rest/container",
        params={"comment": settings.mikrotik_container_comment},
    ).mock(
        return_value=httpx.Response(200, json=[{".id": "*c", "status": "running"}])
    )
    respx.post("http://router.test/rest/container/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post("http://router.test/rest/container/start").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container",
        params={".id": "*c"},
    ).mock(
        side_effect=[
            httpx.Response(200, json=[{".id": "*c", "status": "stopped"}]),
            httpx.Response(200, json=[{".id": "*c", "status": "running"}]),
        ]
    )

    respx.post("http://router.test/rest/ip/dns/cache/flush").mock(
        return_value=httpx.Response(200, json={})
    )

    events: list[dict[str, Any]] = []
    async with _client(app) as ac:
        async with ac.stream(
            "POST",
            "/api/groups/add",
            json={"name": "telegram", "rule_kind": "GEOSITE"},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))

    assert events[0]["type"] == "init"
    assert len(events[0]["steps"]) == 9
    assert events[-1] == {"type": "done", "ok": True, "failed_step": None}
    # We should see at least one "running" step in the middle of the stream.
    assert any(
        e.get("type") == "step" and e["step"]["status"] == "running" for e in events
    )


async def test_add_group_invalid_name(app) -> None:
    async with _client(app) as ac:
        r = await ac.post("/api/groups/add", json={"name": "bad name!!"})
    assert r.status_code == 400


async def test_add_group_empty_name(app) -> None:
    async with _client(app) as ac:
        r = await ac.post("/api/groups/add", json={"name": ""})
    assert r.status_code == 400


async def test_add_group_invalid_kind(app) -> None:
    async with _client(app) as ac:
        r = await ac.post(
            "/api/groups/add", json={"name": "ok", "rule_kind": "WHAT"}
        )
    assert r.status_code == 400


async def test_add_group_whitespace_rule_value_falls_back_to_name(app) -> None:
    """A whitespace-only rule_value must not slip past route validation —
    otherwise AddGroupWorkflow's constructor raises inside the detached
    runner and the client sees a 200 SSE stream that just ends without
    an error event. The route should fall back to name (which is already
    validated) so the workflow gets a usable rule_value."""

    async with _client(app) as ac:
        r = await ac.post(
            "/api/groups/add",
            json={"name": "ok", "rule_kind": "GEOSITE", "rule_value": "   "},
        )
    # Falls back to name → workflow streams normally.
    assert r.status_code == 200


@pytest.mark.parametrize(
    "bad_value",
    [
        "foo\nbar",
        "foo bar",
        'foo"bar',
        "foo;bar",
        "foo\tbar",
        "foo/bar",
        "foo:bar",
    ],
)
async def test_add_group_rejects_unsafe_rule_value(app, bad_value: str) -> None:
    """rule_value is stored verbatim as a RouterOS env that entrypoint.sh
    interpolates into the generated config.yaml — characters outside the
    safe set could break YAML parsing or inject unintended config."""

    async with _client(app) as ac:
        r = await ac.post(
            "/api/groups/add",
            json={"name": "ok", "rule_kind": "GEOSITE", "rule_value": bad_value},
        )
    assert r.status_code == 400


@pytest.mark.parametrize(
    "good_value",
    [
        # script21.rsc:194 ships AI_GEOSITE=category-ai-!cn,openai,google-gemini
        # as a default. The web-ui must accept the same shape so a re-edit
        # of an existing group doesn't fail validation client- or server-side.
        "category-ai-!cn,openai,google-gemini",
        "openai,google-gemini",
        "category-ai-!cn",
        "youtube",
        # meta-rules-dat ships ``@``-suffixed categories (``adobe@ads``,
        # ``steam@cn``, ``category-games@cn``) as first-class entries.
        # Selecting one in the frontend dropdown forwards it verbatim as
        # ``rule_value`` — server-side validation must accept it.
        "adobe@ads",
        "category-games@cn",
        "steam@cn,adobe@ads",
    ],
)
async def test_add_group_accepts_geosite_negation_and_csv_values(
    app, good_value: str
) -> None:
    """``,`` (multi-category separator) and ``!`` (geosite negation prefix)
    are part of real entrypoint.sh defaults — neither must be rejected by
    server-side validation."""

    async with _client(app) as ac:
        r = await ac.post(
            "/api/groups/add",
            json={
                "name": "ai",
                "rule_kind": "GEOSITE",
                "rule_value": good_value,
            },
        )
    # Validation passes → request enters the SSE stream (200) instead of the
    # 400 fast path. The stream itself may emit any number of step events;
    # we only care that the route accepted the input.
    assert r.status_code == 200


async def test_add_group_unconfigured() -> None:
    app = create_app(_make_settings(mikrotik_host=""))
    async with _client(app) as ac:
        r = await ac.post("/api/groups/add", json={"name": "ok"})
    assert r.status_code == 503


@pytest.mark.parametrize(
    "payload",
    [
        {"name": 123},
        {"name": "ok", "rule_kind": 7},
        {"name": "ok", "rule_value": ["list"]},
    ],
)
async def test_add_group_rejects_non_string_fields(app, payload) -> None:
    """A non-string ``name``/``rule_kind``/``rule_value`` must surface as
    400 (validated path) not 500 (AttributeError on .strip()/.upper())."""

    async with _client(app) as ac:
        r = await ac.post("/api/groups/add", json=payload)
    assert r.status_code == 400


async def test_remove_group_rejects_non_string_name(app) -> None:
    async with _client(app) as ac:
        r = await ac.post("/api/groups/remove", json={"name": 123})
    assert r.status_code == 400


@pytest.mark.parametrize(
    "name",
    [
        "group",
        "GROUP",
        "healthcheck",
        "external-ui",
        "fake-ip",
        "sub-link",
        "sub_link1",
        "link",
        "link2",
        "global",
        "GLOBAL",
        "dns",
        "DNS",
    ],
)
async def test_add_group_rejects_reserved_name_with_400(app, name: str) -> None:
    """Reserved-prefix names must surface as a synchronous 400 with a clear
    message — not a 200 SSE stream that ends silently because the workflow
    constructor raised inside the detached runner. Direct API clients (curl,
    scripts) need the response status to reflect the rejection."""

    async with _client(app) as ac:
        r = await ac.post(
            "/api/groups/add",
            json={"name": name, "rule_kind": "GEOSITE"},
        )
    assert r.status_code == 400
    assert "reserved" in r.json()["detail"]


@pytest.mark.parametrize("name", ["group", "global", "dns", "healthcheck"])
async def test_remove_group_rejects_reserved_name_with_400(app, name: str) -> None:
    async with _client(app) as ac:
        r = await ac.post("/api/groups/remove", json={"name": name})
    assert r.status_code == 400
    assert "reserved" in r.json()["detail"]


@respx.mock
async def test_add_group_sse_stream_with_mihomo_enabled(settings: Settings) -> None:
    """When MIHOMO_API_URL is configured the init payload must list the new
    wait_mihomo_ready step and the SSE stream must poll mihomo before
    flush_dns."""

    s = _make_settings(mihomo_api_url="http://mihomo.test:9090")
    app = create_app(s)
    list_name = s.mikrotik_envs_list

    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "GROUP"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{".id": "*1", "list": list_name, "key": "GROUP", "value": "youtube"}],
        )
    )
    respx.patch("http://router.test/rest/container/envs/*1").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "TELEGRAM_GEOSITE"},
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.put("http://router.test/rest/container/envs").mock(
        return_value=httpx.Response(201, json={".id": "*42"})
    )

    respx.get(
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/telegram.list"
    ).mock(return_value=httpx.Response(200, text="+.t.me\n+.telegram.org\n"))

    respx.put("http://router.test/rest/system/script").mock(
        return_value=httpx.Response(201, json={".id": "*99"})
    )
    respx.post("http://router.test/rest/system/script/run").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.delete("http://router.test/rest/system/script/*99").mock(
        return_value=httpx.Response(204)
    )

    respx.get(
        "http://router.test/rest/container",
        params={"comment": s.mikrotik_container_comment},
    ).mock(
        return_value=httpx.Response(200, json=[{".id": "*c", "status": "running"}])
    )
    respx.post("http://router.test/rest/container/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post("http://router.test/rest/container/start").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container",
        params={".id": "*c"},
    ).mock(
        side_effect=[
            httpx.Response(200, json=[{".id": "*c", "status": "stopped"}]),
            httpx.Response(200, json=[{".id": "*c", "status": "running"}]),
        ]
    )
    respx.post("http://router.test/rest/ip/dns/cache/flush").mock(
        return_value=httpx.Response(200, json={})
    )

    # mihomo readiness probes — root for wait_running, providers/rules for
    # wait_mihomo_ready. Both succeed on the first poll.
    respx.get("http://mihomo.test:9090/").mock(
        return_value=httpx.Response(200, text='{"hello":"clash.meta"}')
    )
    respx.get("http://mihomo.test:9090/providers/rules").mock(
        return_value=httpx.Response(
            200,
            json={
                "providers": {
                    "geosite-telegram": {
                        "vehicleType": "HTTP",
                        "updatedAt": "2026-05-08T01:23:45Z",
                    }
                }
            },
        )
    )

    events: list[dict[str, Any]] = []
    async with _client(app) as ac:
        async with ac.stream(
            "POST",
            "/api/groups/add",
            json={"name": "telegram", "rule_kind": "GEOSITE"},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))

    assert events[0]["type"] == "init"
    init_ids = [s["id"] for s in events[0]["steps"]]
    assert init_ids == [
        "update_group_env",
        "add_rule_env",
        "fetch_geosite_list",
        "run_router_script",
        "stop_container",
        "wait_stopped",
        "start_container",
        "wait_running",
        "wait_mihomo_ready",
        "flush_dns",
    ]
    assert events[-1] == {"type": "done", "ok": True, "failed_step": None}


@respx.mock
async def test_add_group_sse_stream_run_router_script_timeout(
    app, settings: Settings
) -> None:
    """Regression for the amazon.rsc symptom: when ``script_run`` times out
    (RouterOS still executing while httpx gives up), the SSE stream must
    surface ``run_router_script`` as ``error`` with a ``timeout`` message —
    not a silent stuck workflow."""

    list_name = settings.mikrotik_envs_list

    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "GROUP"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{".id": "*1", "list": list_name, "key": "GROUP", "value": "youtube"}],
        )
    )
    respx.patch("http://router.test/rest/container/envs/*1").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "AMAZON_GEOSITE"},
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.put("http://router.test/rest/container/envs").mock(
        return_value=httpx.Response(201, json={".id": "*42"})
    )
    respx.get(
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/amazon.list"
    ).mock(return_value=httpx.Response(200, text="+.amazon.com\n"))
    respx.put("http://router.test/rest/system/script").mock(
        return_value=httpx.Response(201, json={".id": "*99"})
    )
    # The run endpoint hangs past the deadline — httpx surfaces it as
    # ReadTimeout, MikrotikClient wraps it as MikrotikTimeout.
    respx.post("http://router.test/rest/system/script/run").mock(
        side_effect=httpx.ReadTimeout("read timeout")
    )
    delete_route = respx.delete(
        "http://router.test/rest/system/script/*99"
    ).mock(return_value=httpx.Response(204))
    respx.get(
        "http://router.test/rest/container",
        params={"comment": settings.mikrotik_container_comment},
    ).mock(
        return_value=httpx.Response(200, json=[{".id": "*c", "status": "running"}])
    )

    events: list[dict[str, Any]] = []
    async with _client(app) as ac:
        async with ac.stream(
            "POST",
            "/api/groups/add",
            json={"name": "amazon", "rule_kind": "GEOSITE"},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))

    assert events[-1]["ok"] is False
    assert events[-1]["failed_step"] == "run_router_script"
    err_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "run_router_script"
        and e["step"]["status"] == "error"
    )
    assert "timeout" in err_step["step"]["message"]
    # Cleanup-on-timeout invariant at the HTTP boundary: even when the
    # script run times out, the workflow's finally branch must still hit
    # the DELETE endpoint so the imported script is removed from RouterOS.
    assert delete_route.called


@respx.mock
async def test_add_group_sse_stream_404_list_skips_run_router(
    app, settings: Settings
) -> None:
    """End-to-end SSE: a 404 from meta-rules-dat must let the stream finish
    ok with fetch_geosite_list + run_router_script both reporting ``skipped``."""

    list_name = settings.mikrotik_envs_list

    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "GROUP"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{".id": "*1", "list": list_name, "key": "GROUP", "value": ""}],
        )
    )
    respx.patch("http://router.test/rest/container/envs/*1").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "4PDA_GEOSITE"},
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.put("http://router.test/rest/container/envs").mock(
        return_value=httpx.Response(201, json={".id": "*42"})
    )

    # 404 from meta-rules-dat for this category — many categories don't ship
    # a .list (or use a different name); the workflow must downgrade to skip.
    respx.get(
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/4pda.list"
    ).mock(return_value=httpx.Response(404, text="Not Found"))

    # script_add must NOT be hit — assert via respx's call counter on a
    # registered PUT route.
    script_route = respx.put("http://router.test/rest/system/script").mock(
        return_value=httpx.Response(201, json={".id": "*99"})
    )

    respx.get(
        "http://router.test/rest/container",
        params={"comment": settings.mikrotik_container_comment},
    ).mock(
        return_value=httpx.Response(200, json=[{".id": "*c", "status": "running"}])
    )
    respx.post("http://router.test/rest/container/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post("http://router.test/rest/container/start").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container",
        params={".id": "*c"},
    ).mock(
        side_effect=[
            httpx.Response(200, json=[{".id": "*c", "status": "stopped"}]),
            httpx.Response(200, json=[{".id": "*c", "status": "running"}]),
        ]
    )
    respx.post("http://router.test/rest/ip/dns/cache/flush").mock(
        return_value=httpx.Response(200, json={})
    )

    events: list[dict[str, Any]] = []
    async with _client(app) as ac:
        async with ac.stream(
            "POST",
            "/api/groups/add",
            json={"name": "4pda", "rule_kind": "GEOSITE"},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))

    assert events[-1] == {"type": "done", "ok": True, "failed_step": None}
    assert script_route.called is False

    fetch_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "fetch_geosite_list"
        and e["step"]["status"] == "ok"
    )
    assert "skipped" in fetch_step["step"]["message"]
    run_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "run_router_script"
        and e["step"]["status"] == "ok"
    )
    assert "skipped" in run_step["step"]["message"]


# --------------------------------------------------------- /api/groups/remove


@respx.mock
async def test_remove_group_sse_stream(app, settings: Settings) -> None:
    list_name = settings.mikrotik_envs_list

    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "GROUP"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    ".id": "*1",
                    "list": list_name,
                    "key": "GROUP",
                    "value": "telegram,youtube",
                }
            ],
        )
    )
    respx.patch("http://router.test/rest/container/envs/*1").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*10", "key": "TELEGRAM_GEOSITE", "value": "telegram"},
                {".id": "*11", "key": "YOUTUBE_GEOSITE", "value": "youtube"},
            ],
        )
    )
    respx.delete("http://router.test/rest/container/envs/*10").mock(
        return_value=httpx.Response(204)
    )
    respx.get(
        "http://router.test/rest/container",
        params={"comment": settings.mikrotik_container_comment},
    ).mock(
        return_value=httpx.Response(200, json=[{".id": "*c", "status": "running"}])
    )
    respx.post("http://router.test/rest/container/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post("http://router.test/rest/container/start").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container",
        params={".id": "*c"},
    ).mock(
        side_effect=[
            httpx.Response(200, json=[{".id": "*c", "status": "stopped"}]),
            httpx.Response(200, json=[{".id": "*c", "status": "running"}]),
        ]
    )
    respx.post("http://router.test/rest/ip/dns/cache/flush").mock(
        return_value=httpx.Response(200, json={})
    )

    events: list[dict[str, Any]] = []
    async with _client(app) as ac:
        async with ac.stream(
            "POST", "/api/groups/remove", json={"name": "telegram"}
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))

    assert events[0]["type"] == "init"
    assert len(events[0]["steps"]) == 7
    assert events[-1] == {"type": "done", "ok": True, "failed_step": None}


async def test_remove_group_invalid_name(app) -> None:
    async with _client(app) as ac:
        r = await ac.post("/api/groups/remove", json={"name": ""})
    assert r.status_code == 400


async def test_remove_group_unconfigured() -> None:
    app = create_app(_make_settings(mikrotik_host=""))
    async with _client(app) as ac:
        r = await ac.post("/api/groups/remove", json={"name": "ok"})
    assert r.status_code == 503


@respx.mock
async def test_remove_group_sse_stream_with_mihomo_enabled(settings: Settings) -> None:
    """RemoveGroupWorkflow must include wait_mihomo_ready when configured —
    mirrors the add-flow integration test for symmetry."""

    s = _make_settings(mihomo_api_url="http://mihomo.test:9090")
    app = create_app(s)
    list_name = s.mikrotik_envs_list

    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "GROUP"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    ".id": "*1",
                    "list": list_name,
                    "key": "GROUP",
                    "value": "telegram,youtube",
                }
            ],
        )
    )
    respx.patch("http://router.test/rest/container/envs/*1").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name},
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.get(
        "http://router.test/rest/container",
        params={"comment": s.mikrotik_container_comment},
    ).mock(
        return_value=httpx.Response(200, json=[{".id": "*c", "status": "running"}])
    )
    respx.post("http://router.test/rest/container/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post("http://router.test/rest/container/start").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container",
        params={".id": "*c"},
    ).mock(
        side_effect=[
            httpx.Response(200, json=[{".id": "*c", "status": "stopped"}]),
            httpx.Response(200, json=[{".id": "*c", "status": "running"}]),
        ]
    )
    respx.post("http://router.test/rest/ip/dns/cache/flush").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get("http://mihomo.test:9090/").mock(
        return_value=httpx.Response(200, text='{"hello":"clash.meta"}')
    )
    respx.get("http://mihomo.test:9090/providers/rules").mock(
        return_value=httpx.Response(200, json={"providers": {}})
    )

    events: list[dict[str, Any]] = []
    async with _client(app) as ac:
        async with ac.stream(
            "POST", "/api/groups/remove", json={"name": "telegram"}
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))

    init_ids = [s["id"] for s in events[0]["steps"]]
    assert "wait_mihomo_ready" in init_ids
    assert len(init_ids) == 8
    assert events[-1] == {"type": "done", "ok": True, "failed_step": None}


@respx.mock
async def test_remove_workflow_completes_after_client_disconnects(
    app, settings: Settings
) -> None:
    # When the SSE client closes mid-stream, the workflow must still run to
    # completion — otherwise the container could be left ``stopped`` because
    # ``start_container`` never fires. This test reads only the first event,
    # closes the response, then asserts that ``start_container`` was still
    # called by the detached runner task.
    import asyncio

    list_name = settings.mikrotik_envs_list
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name, "key": "GROUP"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {".id": "*1", "list": list_name, "key": "GROUP", "value": "telegram"}
            ],
        )
    )
    respx.patch("http://router.test/rest/container/envs/*1").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container/envs",
        params={"list": list_name},
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.get(
        "http://router.test/rest/container",
        params={"comment": settings.mikrotik_container_comment},
    ).mock(
        return_value=httpx.Response(200, json=[{".id": "*c", "status": "running"}])
    )
    stop_route = respx.post("http://router.test/rest/container/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    start_route = respx.post("http://router.test/rest/container/start").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(
        "http://router.test/rest/container",
        params={".id": "*c"},
    ).mock(
        side_effect=[
            httpx.Response(200, json=[{".id": "*c", "status": "stopped"}]),
            httpx.Response(200, json=[{".id": "*c", "status": "running"}]),
        ]
    )
    flush_route = respx.post("http://router.test/rest/ip/dns/cache/flush").mock(
        return_value=httpx.Response(200, json={})
    )

    async with _client(app) as ac:
        async with ac.stream(
            "POST", "/api/groups/remove", json={"name": "telegram"}
        ) as resp:
            assert resp.status_code == 200
            # Read only the first SSE frame, then close the stream.
            iterator = resp.aiter_lines()
            async for line in iterator:
                if line.startswith("data: "):
                    break

        # Wait for any in-flight runner task to finish — runner_tasks holds
        # a strong reference until the task's done callback fires.
        for _ in range(50):
            if not app.state.runner_tasks:
                break
            await asyncio.sleep(0.05)

    assert stop_route.called
    assert start_route.called
    assert flush_route.called
    assert app.state.runner_tasks == set()


# --------------------------------------------------------------- shutdown drain


async def test_shutdown_drain_timeout_covers_run_script_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain must outlast the longest workflow step.

    Regression for the case where ``drain_timeout`` was sized to a single
    container-wait window — a SIGTERM during ``run_router_script`` would
    then cancel the runner mid-``script_run`` and the cleanup ``finally``
    would delete the still-executing RouterOS script. Drain now sums
    ``run_script_timeout + wait_stopped_timeout + wait_running_timeout +
    mihomo_ready_timeout + 30s`` headroom.
    """

    settings = _make_settings(
        run_script_timeout=600.0,
        wait_stopped_timeout=60.0,
        wait_running_timeout=180.0,
        container_wait_timeout=180.0,
        mihomo_ready_timeout=90.0,
    )
    app = create_app(settings)

    captured: dict[str, Any] = {}

    real_wait = asyncio.wait

    async def spy_wait(tasks, *, timeout=None, **kwargs):
        captured["timeout"] = timeout
        return await real_wait(tasks, timeout=0, **kwargs)

    monkeypatch.setattr("backend.app.asyncio.wait", spy_wait)

    # asyncio.wait is only invoked when there are runners to drain.
    async def _placeholder() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_placeholder())
    app.state.runner_tasks.add(task)
    task.add_done_callback(app.state.runner_tasks.discard)
    try:
        for handler in app.router.on_shutdown:
            await handler()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass

    # 600 (run_script) + 60 (wait_stopped) + 180 (wait_running) + 90 (mihomo)
    # + 30 headroom = 960 seconds. The test asserts the lower bound so
    # future adjustments to headroom don't break this regression coverage.
    assert captured["timeout"] is not None
    assert captured["timeout"] >= 960.0


async def test_shutdown_cancels_queued_runners_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued runners must be cancelled at shutdown so a single drain
    window is enough.

    Without this, two serialized runners could both fit within the
    ``drain_timeout``: the first consumes most of the window, then the
    second acquires the workflow_lock near the end, enters
    ``script_run``, and gets cancelled by loop teardown — the very
    failure mode the longer drain timeout was meant to prevent.
    """

    settings = _make_settings()
    app = create_app(settings)

    async def _sleeper() -> None:
        await asyncio.sleep(3600)

    active_task = asyncio.create_task(_sleeper())
    queued_task = asyncio.create_task(_sleeper())
    app.state.runner_tasks.add(active_task)
    app.state.runner_tasks.add(queued_task)
    active_task.add_done_callback(app.state.runner_tasks.discard)
    queued_task.add_done_callback(app.state.runner_tasks.discard)
    # Pretend ``active_task`` already acquired workflow_lock.
    app.state.active_runner = active_task

    async def spy_wait(tasks, *, timeout=None, **kwargs):
        # Return immediately so the test doesn't hang on the full window.
        return set(), set(tasks)

    monkeypatch.setattr("backend.app.asyncio.wait", spy_wait)

    try:
        for handler in app.router.on_shutdown:
            await handler()
        # Allow scheduled cancellations to propagate.
        await asyncio.sleep(0)
        assert queued_task.cancelled() or queued_task.done()
        assert not active_task.cancelled()
        assert not active_task.done()
    finally:
        active_task.cancel()
        queued_task.cancel()
        for t in (active_task, queued_task):
            try:
                await t
            except (asyncio.CancelledError, BaseException):
                pass
