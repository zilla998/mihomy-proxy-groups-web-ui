"""Async client for ``MetaCubeX/meta-rules-dat`` (geosite/geoip).

Two read paths:

* ``list_rule_categories(kind)`` enumerates ``geo/geosite`` or ``geo/geoip``
  ``.mrs`` basenames via the GitHub Git Trees API
  (``GET /repos/{repo}/git/trees/{branch}:{path}``). The Contents API silently
  caps directory responses at 1000 entries, which truncates ``geo/geosite/``
  (>1700 ``.mrs`` files) — the bug that surfaced as "groups list only goes up
  to letter A". Trees API has no such cap (only a sentinel ``truncated`` flag
  which we treat as a hard error if ever set). Listings are cached in memory
  for a configurable TTL (default 24 h) so we stay well under the
  unauthenticated GitHub API limit (60 req/h per IP).
* ``fetch_geosite_list(name)`` pulls the raw ``geo/geosite/<name>.list`` body
  from ``raw.githubusercontent.com``. The caller parses it into a DNS-FWD
  RouterOS script.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx


GITHUB_API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

# Source of geosite/geoip MRS files consumed by entrypoint.sh — must match the
# branch and paths referenced there (`MetaCubeX/meta-rules-dat`, branch `meta`).
RULE_REPO = "MetaCubeX/meta-rules-dat"
RULE_BRANCH = "meta"
RULE_KIND_PATHS = {
    "GEOSITE": "geo/geosite",
    "GEOIP": "geo/geoip",
}
DEFAULT_RULE_CACHE_TTL = 24 * 3600.0


class GithubClientError(Exception):
    """Base error for GitHub client failures."""


class GithubError(GithubClientError):
    """Non-2xx response from GitHub."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"GitHub API returned HTTP {status_code}: {body!r}")


class GithubTimeout(GithubClientError):
    """Transport-level timeout while talking to GitHub."""


class GithubClient:
    """Async wrapper around the public GitHub API + raw content host.

    Parameters
    ----------
    rule_cache_ttl:
        Seconds to cache the ``list_rule_categories`` result per kind. ``0``
        disables caching.
    timeout:
        Per-request HTTP timeout passed to the internal :class:`httpx.AsyncClient`
        (ignored when an explicit ``client`` is supplied).
    client:
        Optional pre-built :class:`httpx.AsyncClient`. When given, the caller
        retains ownership and is responsible for closing it.
    """

    def __init__(
        self,
        *,
        rule_cache_ttl: float = DEFAULT_RULE_CACHE_TTL,
        timeout: float = 10.0,
        client: "httpx.AsyncClient | None" = None,
    ) -> None:
        self._rule_cache_ttl = rule_cache_ttl
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "mihomo-proxy-ros-webui",
            },
        )
        self._rule_cache: "dict[str, tuple[float, list[str]]]" = {}
        self._rule_cache_lock = asyncio.Lock()

    async def __aenter__(self) -> "GithubClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ----------------------------------------------------------------- public

    async def fetch_geosite_list(self, name: str) -> str:
        """Fetch the raw ``geo/geosite/<name>.list`` body from meta-rules-dat.

        Raises :class:`GithubError` for any non-2xx response (callers downgrade
        404 to a "no list for this category" skip), :class:`GithubTimeout` on
        transport timeouts, and :class:`GithubClientError` for other transport
        failures.
        """

        path = RULE_KIND_PATHS["GEOSITE"]
        url = f"{RAW_BASE}/{RULE_REPO}/{RULE_BRANCH}/{path}/{name}.list"
        return await self._fetch_text(url)

    async def list_rule_categories(
        self, kind: str, *, force_refresh: bool = False
    ) -> list[str]:
        """List geosite/geoip category names from ``MetaCubeX/meta-rules-dat``.

        Returns sorted ``.mrs`` basenames (without extension) found under
        ``geo/geosite/`` or ``geo/geoip/`` of the ``meta`` branch — the exact
        source ``entrypoint.sh`` consumes at runtime.
        """

        kind_upper = kind.upper()
        if kind_upper not in RULE_KIND_PATHS:
            raise ValueError(
                f"unsupported rule kind: {kind!r} (expected one of "
                f"{sorted(RULE_KIND_PATHS)})"
            )
        path = RULE_KIND_PATHS[kind_upper]

        async with self._rule_cache_lock:
            now = time.monotonic()
            cached = self._rule_cache.get(kind_upper)
            if (
                not force_refresh
                and cached is not None
                and now - cached[0] < self._rule_cache_ttl
            ):
                return cached[1]

            data = await self._fetch_listing_at(RULE_REPO, path, RULE_BRANCH)
            names: list[str] = []
            for item in data:
                if item.get("type") != "file":
                    continue
                fname = item.get("name", "")
                if not fname.endswith(".mrs"):
                    continue
                names.append(fname[:-4])
            names.sort()
            self._rule_cache[kind_upper] = (now, names)
            return names

    # --------------------------------------------------------------- internals

    async def _fetch_listing_at(
        self, repo: str, path: str, branch: str
    ) -> list[dict[str, Any]]:
        tree_ish = quote(f"{branch}:{path}", safe=":/")
        url = f"{GITHUB_API_BASE}/repos/{repo}/git/trees/{tree_ish}"
        try:
            resp = await self._client.get(url)
        except httpx.TimeoutException as exc:
            raise GithubTimeout(f"GET {url} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise GithubClientError(f"GET {url} failed: {exc}") from exc

        if resp.status_code >= 400:
            raise GithubError(resp.status_code, _safe_body(resp))
        try:
            data = resp.json()
        except ValueError as exc:
            raise GithubClientError(f"non-JSON listing response: {exc}") from exc
        if not isinstance(data, dict):
            raise GithubClientError(
                "expected JSON object from GitHub Git Trees API, got "
                f"{type(data).__name__}"
            )
        if data.get("truncated"):
            raise GithubClientError("listing truncated by GitHub")
        tree = data.get("tree")
        if not isinstance(tree, list):
            raise GithubClientError(
                "expected 'tree' to be a list in GitHub Git Trees API "
                f"response, got {type(tree).__name__}"
            )
        out: list[dict[str, Any]] = []
        for entry in tree:
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type")
            if etype == "blob":
                norm = "file"
            elif etype == "tree":
                norm = "dir"
            else:
                continue
            out.append({"name": entry.get("path", ""), "type": norm})
        return out

    async def _fetch_text(self, url: str) -> str:
        try:
            resp = await self._client.get(url)
        except httpx.TimeoutException as exc:
            raise GithubTimeout(f"GET {url} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise GithubClientError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise GithubError(resp.status_code, _safe_body(resp))
        return resp.text


def _safe_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text
