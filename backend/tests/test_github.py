"""Unit tests for :mod:`backend.github` — all HTTP traffic mocked via respx."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from backend.github import (
    GithubClient,
    GithubClientError,
    GithubError,
    GithubTimeout,
    RULE_BRANCH,
    RULE_REPO,
)

GEOSITE_LIST_URL = (
    f"https://api.github.com/repos/{RULE_REPO}/git/trees/{RULE_BRANCH}:geo/geosite"
)
GEOIP_LIST_URL = (
    f"https://api.github.com/repos/{RULE_REPO}/git/trees/{RULE_BRANCH}:geo/geoip"
)
GEOSITE_RAW_BASE = (
    f"https://raw.githubusercontent.com/{RULE_REPO}/{RULE_BRANCH}/geo/geosite"
)


def _tree(*entries: dict, truncated: bool = False) -> dict:
    return {
        "sha": "abc",
        "url": "https://api.github.com/...",
        "tree": list(entries),
        "truncated": truncated,
    }


def _blob(name: str) -> dict:
    return {"path": name, "type": "blob", "mode": "100644", "sha": "deadbeef"}


def _subtree(name: str) -> dict:
    return {"path": name, "type": "tree", "mode": "040000", "sha": "feedface"}


@pytest.fixture
async def client() -> AsyncIterator[GithubClient]:
    c = GithubClient()
    try:
        yield c
    finally:
        await c.aclose()


# ---------------- fetch_geosite_list (raw .list from meta-rules-dat) ---------


@respx.mock
async def test_fetch_geosite_list_ok(client: GithubClient) -> None:
    respx.get(f"{GEOSITE_RAW_BASE}/reddit.list").mock(
        return_value=httpx.Response(200, text="+.reddit.com\n+.redditblog.com\n"),
    )
    body = await client.fetch_geosite_list("reddit")
    assert body == "+.reddit.com\n+.redditblog.com\n"


@respx.mock
async def test_fetch_geosite_list_404(client: GithubClient) -> None:
    respx.get(f"{GEOSITE_RAW_BASE}/missing.list").mock(
        return_value=httpx.Response(404, text="404: Not Found"),
    )
    with pytest.raises(GithubError) as info:
        await client.fetch_geosite_list("missing")
    assert info.value.status_code == 404
    assert info.value.body == "404: Not Found"


@respx.mock
async def test_fetch_geosite_list_timeout(client: GithubClient) -> None:
    respx.get(f"{GEOSITE_RAW_BASE}/reddit.list").mock(
        side_effect=httpx.ReadTimeout("read"),
    )
    with pytest.raises(GithubTimeout):
        await client.fetch_geosite_list("reddit")


@respx.mock
async def test_fetch_geosite_list_url(client: GithubClient) -> None:
    """The URL must hit ``raw.githubusercontent.com`` on the ``meta`` branch
    under ``geo/geosite/<name>.list`` — that's the exact path entrypoint.sh
    consumes for geosite categories. A drift here would silently look up
    nothing and turn every group add into a 404-skip."""

    expected = (
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/"
        "geo/geosite/youtube.list"
    )
    route = respx.get(expected).mock(
        return_value=httpx.Response(200, text=""),
    )
    await client.fetch_geosite_list("youtube")
    assert route.called
    assert str(route.calls.last.request.url) == expected


# ---------------- list_rule_categories (geosite/geoip from meta-rules-dat) ----


@respx.mock
async def test_list_rule_categories_geosite_filters_and_sorts(
    client: GithubClient,
) -> None:
    respx.get(GEOSITE_LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json=_tree(
                _blob("youtube.mrs"),
                _blob("telegram.mrs"),
                _blob("README.md"),
                _blob("geolocation-cn.mrs"),
                _subtree("subdir"),
            ),
        )
    )
    cats = await client.list_rule_categories("GEOSITE")
    assert cats == ["geolocation-cn", "telegram", "youtube"]


@respx.mock
async def test_list_rule_categories_geoip_filters_and_sorts(
    client: GithubClient,
) -> None:
    respx.get(GEOIP_LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json=_tree(
                _blob("us.mrs"),
                _blob("ru.mrs"),
                _blob("cn.mrs"),
                _blob("_meta.json"),
            ),
        )
    )
    cats = await client.list_rule_categories("GEOIP")
    assert cats == ["cn", "ru", "us"]


@respx.mock
async def test_list_rule_categories_lowercase_kind_accepted(
    client: GithubClient,
) -> None:
    respx.get(GEOSITE_LIST_URL).mock(
        return_value=httpx.Response(200, json=_tree(_blob("x.mrs")))
    )
    cats = await client.list_rule_categories("geosite")
    assert cats == ["x"]


async def test_list_rule_categories_unknown_kind_raises(
    client: GithubClient,
) -> None:
    with pytest.raises(ValueError):
        await client.list_rule_categories("DOMAIN")


@respx.mock
async def test_list_rule_categories_caches_separately_per_kind(
    client: GithubClient,
) -> None:
    geosite_route = respx.get(GEOSITE_LIST_URL).mock(
        return_value=httpx.Response(200, json=_tree(_blob("g.mrs")))
    )
    geoip_route = respx.get(GEOIP_LIST_URL).mock(
        return_value=httpx.Response(200, json=_tree(_blob("i.mrs")))
    )
    a1 = await client.list_rule_categories("GEOSITE")
    a2 = await client.list_rule_categories("GEOSITE")
    b1 = await client.list_rule_categories("GEOIP")
    b2 = await client.list_rule_categories("GEOIP")
    assert a1 == a2 == ["g"]
    assert b1 == b2 == ["i"]
    assert geosite_route.call_count == 1
    assert geoip_route.call_count == 1


@respx.mock
async def test_list_rule_categories_force_refresh(client: GithubClient) -> None:
    route = respx.get(GEOSITE_LIST_URL).mock(
        return_value=httpx.Response(200, json=_tree(_blob("a.mrs")))
    )
    await client.list_rule_categories("GEOSITE")
    await client.list_rule_categories("GEOSITE", force_refresh=True)
    assert route.call_count == 2


@respx.mock
async def test_list_rule_categories_no_cache_when_ttl_zero() -> None:
    c = GithubClient(rule_cache_ttl=0.0)
    try:
        route = respx.get(GEOSITE_LIST_URL).mock(
            return_value=httpx.Response(200, json=_tree(_blob("a.mrs")))
        )
        await c.list_rule_categories("GEOSITE")
        await c.list_rule_categories("GEOSITE")
        assert route.call_count == 2
    finally:
        await c.aclose()


@respx.mock
async def test_list_rule_categories_404() -> None:
    c = GithubClient(rule_cache_ttl=0)
    try:
        respx.get(GEOSITE_LIST_URL).mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with pytest.raises(GithubError) as info:
            await c.list_rule_categories("GEOSITE")
        assert info.value.status_code == 404
    finally:
        await c.aclose()


@respx.mock
async def test_list_rule_categories_timeout() -> None:
    c = GithubClient(rule_cache_ttl=0)
    try:
        respx.get(GEOSITE_LIST_URL).mock(
            side_effect=httpx.ReadTimeout("read")
        )
        with pytest.raises(GithubTimeout):
            await c.list_rule_categories("GEOSITE")
    finally:
        await c.aclose()


@respx.mock
async def test_list_rule_categories_transport_error_wrapped() -> None:
    """ConnectError (the parent ``httpx.HTTPError`` branch) must be wrapped
    into ``GithubClientError`` so the route handler at /api/rules/categories
    can convert it into a 502 instead of letting raw httpx exceptions
    propagate."""

    c = GithubClient(rule_cache_ttl=0)
    try:
        respx.get(GEOSITE_LIST_URL).mock(
            side_effect=httpx.ConnectError("conn refused")
        )
        with pytest.raises(GithubClientError):
            await c.list_rule_categories("GEOSITE")
    finally:
        await c.aclose()


@respx.mock
async def test_list_rule_categories_truncated_raises() -> None:
    c = GithubClient(rule_cache_ttl=0)
    try:
        respx.get(GEOSITE_LIST_URL).mock(
            return_value=httpx.Response(
                200,
                json=_tree(_blob("a.mrs"), truncated=True),
            )
        )
        with pytest.raises(GithubClientError) as info:
            await c.list_rule_categories("GEOSITE")
        assert "truncated" in str(info.value).lower()
    finally:
        await c.aclose()


@respx.mock
async def test_list_rule_categories_non_object_payload_raises() -> None:
    """Smoke: a top-level JSON list (the old Contents-API shape) must trigger
    GithubClientError, not be silently treated as an empty tree."""

    c = GithubClient(rule_cache_ttl=0)
    try:
        respx.get(GEOSITE_LIST_URL).mock(
            return_value=httpx.Response(
                200, json=[{"path": "a.mrs", "type": "blob"}]
            )
        )
        with pytest.raises(GithubClientError):
            await c.list_rule_categories("GEOSITE")
    finally:
        await c.aclose()


@respx.mock
async def test_list_rule_categories_handles_more_than_1000_entries() -> None:
    """Regression: ``geo/geosite/`` has >1700 ``.mrs`` files. The Contents
    API silently capped the response at 1000 entries, which is the original
    "groups list only goes up to letter A" bug. Trees API has no such cap —
    verify all 1500 synthetic entries make it through."""

    c = GithubClient(rule_cache_ttl=0)
    try:
        entries = [_blob(f"cat{i:04d}.mrs") for i in range(1500)]
        respx.get(GEOSITE_LIST_URL).mock(
            return_value=httpx.Response(200, json=_tree(*entries))
        )
        cats = await c.list_rule_categories("GEOSITE")
        assert len(cats) == 1500
        assert cats[0] == "cat0000"
        assert cats[-1] == "cat1499"
    finally:
        await c.aclose()


@respx.mock
async def test_listing_url_keeps_colon_unencoded() -> None:
    """``:`` is a sub-delim in path segments and the GitHub tree-ish syntax
    relies on it being literal. Make sure we don't accidentally percent-encode
    it (which would resolve to a 404 from GitHub)."""

    c = GithubClient(rule_cache_ttl=0)
    try:
        route = respx.get(GEOSITE_LIST_URL).mock(
            return_value=httpx.Response(200, json=_tree(_blob("a.mrs"))),
        )
        await c.list_rule_categories("GEOSITE")
        called_url = str(route.calls.last.request.url)
        assert ":geo/geosite" in called_url
        assert "%3A" not in called_url
    finally:
        await c.aclose()
