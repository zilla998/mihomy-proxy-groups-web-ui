"""Async client for the mihomo (Clash Meta) external-controller HTTP API.

Used by the web-ui workflow to wait until mihomo inside the container has
finished loading proxy/rule providers before flushing the upstream DNS cache.

Endpoints exercised:

- ``GET /`` — welcome endpoint that returns a small JSON object
  (e.g. ``{"hello":"clash.meta"}``). Used as the "container is up" signal
  because RouterOS REST may keep ``status=<empty>`` for the entire wait
  window on a cold start. When mihomo is configured with ``secret:``, the
  router gates ``/`` behind bearer auth too — a 401/403 response still
  proves the HTTP listener is bound, so the readiness probe accepts it as
  "up" and lets the next API call surface the auth failure cleanly.
- ``GET /version`` — controller probe (2xx ⇒ controller is up). Bearer-auth
  required when ``secret`` is set.
- ``GET /providers/rules`` — map of rule providers; each entry that is *not*
  inline (``vehicleType`` ≠ ``"Inline"``) must have a non-zero ``updatedAt``
  timestamp before we consider mihomo "fully ready".

When ``secret`` is provided, every request carries
``Authorization: Bearer <secret>`` to match the mihomo external-controller
authentication scheme.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class MihomoClientError(Exception):
    """Base class for mihomo client failures."""


class MihomoError(MihomoClientError):
    """Non-2xx HTTP response from mihomo."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"mihomo API returned HTTP {status_code}: {body!r}")


class MihomoTimeout(MihomoClientError):
    """Transport-level timeout or readiness deadline expired."""


class MihomoClient:
    """Async REST client for the mihomo external-controller.

    Parameters
    ----------
    base_url:
        Base URL with explicit scheme, e.g. ``http://192.168.255.2:9090``.
        Trailing slashes are tolerated.
    secret:
        Optional bearer secret. When non-empty, every request gets
        ``Authorization: Bearer <secret>``.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        secret: str | None = None,
        *,
        timeout: float = 5.0,
        client: "httpx.AsyncClient | None" = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret or ""
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
        )

    async def __aenter__(self) -> "MihomoClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --------------------------------------------------------------- internals

    async def _get_json(self, path: str) -> Any:
        try:
            resp = await self._client.get(path)
        except httpx.TimeoutException as exc:
            raise MihomoTimeout(f"GET {path} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise MihomoClientError(f"GET {path} failed: {exc}") from exc

        if resp.status_code >= 400:
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            raise MihomoError(resp.status_code, body)

        try:
            return resp.json()
        except ValueError as exc:
            raise MihomoClientError(f"non-JSON response from {path}: {exc}") from exc

    # ------------------------------------------------------------------ public

    async def version(self) -> dict[str, Any]:
        result = await self._get_json("/version")
        if not isinstance(result, dict):
            raise MihomoClientError(
                f"expected JSON object from /version, got {type(result).__name__}"
            )
        return result

    async def providers_rules(self) -> dict[str, dict[str, Any]]:
        """Return the ``providers`` map from ``GET /providers/rules``.

        mihomo returns ``{"providers": {<name>: {...}, ...}}``. We unwrap the
        outer envelope so callers can iterate over provider entries directly.
        """

        result = await self._get_json("/providers/rules")
        if not isinstance(result, dict):
            raise MihomoClientError(
                f"expected JSON object from /providers/rules, got "
                f"{type(result).__name__}"
            )
        providers = result.get("providers", result)
        if not isinstance(providers, dict):
            raise MihomoClientError(
                f"expected providers map from /providers/rules, got "
                f"{type(providers).__name__}"
            )
        return providers

    async def wait_started(
        self,
        *,
        timeout: float,
        poll_interval: float = 1.0,
    ) -> str:
        """Block until mihomo's HTTP listener answers ``GET /`` or ``timeout`` elapses.

        mihomo's root endpoint returns a tiny welcome JSON like
        ``{"hello":"clash.meta"}`` once the HTTP listener is up. The probe
        accepts either of two responses as the "container is up" signal:

        - any 2xx whose body — after stripping leading whitespace — starts
          with ``{``, or
        - a 401/403 status. mihomo registers ``GET /`` inside the same chi
          group that mounts bearer-auth middleware, so when ``secret:`` is
          configured a missing/wrong ``MIHOMO_API_SECRET`` yields 401. That
          response still proves the listener is bound; surfacing it as "up"
          lets the next authenticated call (e.g. ``/providers/rules``) raise
          a clear :class:`MihomoError` instead of this method burning the
          full ``WAIT_RUNNING_TIMEOUT`` on auth failures.

        Returns the (trimmed) response body for logging. On the deadline raises
        :class:`MihomoTimeout` quoting the most recent failure reason.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_error: str = "no attempt completed"
        while True:
            try:
                resp = await self._client.get("/")
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code in (401, 403):
                    return f"HTTP {resp.status_code} (auth required, listener up)"
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:120]!r}"
                else:
                    body = resp.text.lstrip()
                    if body.startswith("{"):
                        return body[:200]
                    last_error = f"non-JSON body: {resp.text[:120]!r}"
            if loop.time() >= deadline:
                raise MihomoTimeout(
                    f"mihomo / not ready within {timeout}s; "
                    f"last error: {last_error}"
                )
            await asyncio.sleep(poll_interval)

    async def wait_providers_ready(
        self,
        *,
        timeout: float,
        poll_interval: float = 1.0,
    ) -> dict[str, dict[str, Any]]:
        """Block until every non-Inline rule provider has a non-empty ``updatedAt``.

        mihomo emits ``updatedAt: ""`` for HTTP rule providers until the first
        successful download completes. Inline providers are exempt — they have
        no remote source. On the deadline raises :class:`MihomoTimeout` listing
        the still-pending provider names.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                providers = await self.providers_rules()
            except MihomoError as exc:
                # 401/403 are permanent for this run — wait_started accepts
                # them as "listener up" precisely so this call can surface
                # the auth failure cleanly instead of burning the full
                # MIHOMO_READY_TIMEOUT on a wrong/missing MIHOMO_API_SECRET.
                if exc.status_code in (401, 403):
                    raise
                if loop.time() >= deadline:
                    raise MihomoTimeout(
                        f"mihomo rule providers not ready within {timeout}s; "
                        f"last error: {exc}"
                    ) from exc
                await asyncio.sleep(poll_interval)
                continue
            except MihomoClientError as exc:
                if loop.time() >= deadline:
                    raise MihomoTimeout(
                        f"mihomo rule providers not ready within {timeout}s; "
                        f"last error: {exc}"
                    ) from exc
                await asyncio.sleep(poll_interval)
                continue

            pending: list[str] = []
            for name, entry in providers.items():
                if not isinstance(entry, dict):
                    continue
                vehicle = entry.get("vehicleType", "")
                if vehicle == "Inline":
                    continue
                updated_at = entry.get("updatedAt", "")
                if not updated_at:
                    pending.append(name)

            if not pending:
                return providers

            if loop.time() >= deadline:
                raise MihomoTimeout(
                    f"mihomo rule providers not ready within {timeout}s; "
                    f"pending={sorted(pending)}"
                )
            await asyncio.sleep(poll_interval)
