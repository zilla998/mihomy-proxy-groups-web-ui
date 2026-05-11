"""Async client for the MikroTik RouterOS REST API.

Wraps just the endpoints used by the web-ui workflow:
- ``/rest/container/envs`` (list, find, add, set, remove)
- ``/rest/container`` (find by comment, start, stop, wait status)
- ``/rest/ip/dns/cache/flush``
- ``/rest/system/script`` (add, run, remove)
- ``/rest/system/identity`` (health probe)

RouterOS REST conventions used here::

    GET    /rest/<menu>                      list all items
    GET    /rest/<menu>?<prop>=<value>       filter by property
    PUT    /rest/<menu>                      add new item        (body: json)
    PATCH  /rest/<menu>/<id>                 update item         (body: json)
    DELETE /rest/<menu>/<id>                 remove item
    POST   /rest/<menu>/<command>            invoke command      (body: json)

All errors surface as :class:`MikrotikClientError` subclasses so callers can
distinguish transport-level timeouts from RouterOS-side rejections.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx


_UNSET: Any = object()
_MISSING: Any = object()


def _format_history_entry(offset: float, status: Any) -> str:
    if status is _MISSING:
        label = "<missing>"
    elif status in (None, ""):
        label = "<empty>"
    else:
        label = str(status)
    return f"{label}@{offset:.1f}s"


class MikrotikClientError(Exception):
    """Base class for all MikroTik client errors."""


class MikrotikError(MikrotikClientError):
    """Non-2xx HTTP response from RouterOS."""

    def __init__(self, status_code: int, body: Any, *, message: str | None = None) -> None:
        self.status_code = status_code
        self.body = body
        msg = message or f"MikroTik REST returned HTTP {status_code}: {body!r}"
        super().__init__(msg)


class MikrotikTimeout(MikrotikClientError):
    """Transport-level timeout while talking to RouterOS."""


class MikrotikClient:
    """Async REST client for RouterOS.

    Parameters
    ----------
    base_url:
        Full base URL with explicit scheme, e.g. ``http://192.168.88.1`` or
        ``https://router.lan``. Trailing slashes are tolerated.
    user, password:
        HTTP Basic credentials.
    verify_tls:
        Forwarded to httpx; set to ``False`` for self-signed RouterOS certs.
    timeout:
        Per-request timeout in seconds (read + connect).
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        verify_tls: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=(user, password),
            verify=verify_tls,
            timeout=timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    async def __aenter__(self) -> "MikrotikClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --------------------------------------------------------------- internals

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            kwargs: dict[str, Any] = {"params": params, "json": json}
            if timeout is not None:
                # Per-call override: long-running RouterOS commands (e.g. running
                # an imported .rsc with hundreds of /ip dns static add lines)
                # need much longer than the client default but other calls must
                # stay short so a hung router fails fast.
                kwargs["timeout"] = timeout
            resp = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise MikrotikTimeout(f"{method} {path} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise MikrotikClientError(f"{method} {path} failed: {exc}") from exc

        if resp.status_code >= 400:
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            raise MikrotikError(resp.status_code, body)

        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------------------- identity

    async def system_identity(self) -> Any:
        """Cheap probe used by ``/api/health``."""

        return await self._request("GET", "/rest/system/identity")

    # ------------------------------------------------------------- envs

    async def list_envs(self, list_name: str) -> list[dict[str, Any]]:
        result = await self._request(
            "GET", "/rest/container/envs", params={"list": list_name}
        )
        return result or []

    async def find_env(self, list_name: str, key: str) -> dict[str, Any] | None:
        result = await self._request(
            "GET", "/rest/container/envs", params={"list": list_name, "key": key}
        )
        if not result:
            return None
        return result[0]

    async def add_env(self, list_name: str, key: str, value: str) -> dict[str, Any]:
        result = await self._request(
            "PUT",
            "/rest/container/envs",
            json={"list": list_name, "key": key, "value": value},
        )
        return result or {}

    async def set_env(self, env_id: str, value: str) -> dict[str, Any]:
        result = await self._request(
            "PATCH",
            f"/rest/container/envs/{env_id}",
            json={"value": value},
        )
        return result or {}

    async def remove_env(self, env_id: str) -> None:
        await self._request("DELETE", f"/rest/container/envs/{env_id}")

    # ------------------------------------------------------------- container

    async def find_container(self, comment: str) -> dict[str, Any] | None:
        result = await self._request(
            "GET", "/rest/container", params={"comment": comment}
        )
        if not result:
            return None
        return result[0]

    async def get_container(self, container_id: str) -> dict[str, Any] | None:
        result = await self._request(
            "GET", "/rest/container", params={".id": container_id}
        )
        if not result:
            return None
        return result[0]

    async def stop_container(self, container_id: str) -> Any:
        return await self._request(
            "POST", "/rest/container/stop", json={".id": container_id}
        )

    async def start_container(self, container_id: str) -> Any:
        return await self._request(
            "POST", "/rest/container/start", json={".id": container_id}
        )

    async def _poll_container_status(
        self,
        container_id: str,
        match: Callable[[Any], bool],
        *,
        timeout: float,
        poll_interval: float,
        timeout_message: Callable[[str, str], str],
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + timeout
        last_seen: Any = _MISSING
        history: list[tuple[float, Any]] = []
        last_recorded: Any = _UNSET
        while True:
            container = await self.get_container(container_id)
            now = loop.time()
            if container is None:
                # Track the container disappearing (deleted from RouterOS, or
                # not yet registered) as its own transition so the diagnostic
                # trace shows where it went missing instead of repeating the
                # last-known status forever.
                if last_recorded is not _MISSING:
                    history.append((now - start, _MISSING))
                    last_recorded = _MISSING
                last_seen = _MISSING
            else:
                last_seen = container
                current = container.get("status")
                if current != last_recorded:
                    history.append((now - start, current))
                    last_recorded = current
                if match(current):
                    return container
            if loop.time() >= deadline:
                if last_seen is _MISSING:
                    last_repr = "<missing>"
                else:
                    last_status = last_seen.get("status")
                    last_repr = "<empty>" if last_status in (None, "") else repr(last_status)
                observed = ", ".join(
                    _format_history_entry(t, s) for t, s in history
                ) or "none"
                raise MikrotikTimeout(timeout_message(last_repr, observed))
            await asyncio.sleep(poll_interval)

    async def wait_container_status(
        self,
        container_id: str,
        status: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        """Poll until ``status`` field on the container matches the requested one.

        When waiting for ``"stopped"``, RouterOS may drop the ``status`` field
        entirely (or return it empty/``None``) once the container is fully
        stopped — treat all of those as a match.

        Raises :class:`MikrotikTimeout` if the status is not reached in time.
        """

        def match(current: Any) -> bool:
            if current == status:
                return True
            if status == "stopped" and current in (None, ""):
                return True
            return False

        def message(last_repr: str, observed: str) -> str:
            return (
                f"container {container_id} did not reach status={status!r} "
                f"within {timeout}s; last status={last_repr}; "
                f"observed: {observed}"
            )

        return await self._poll_container_status(
            container_id,
            match,
            timeout=timeout,
            poll_interval=poll_interval,
            timeout_message=message,
        )

    # ------------------------------------------------------------- DNS cache

    async def flush_dns_cache(self) -> Any:
        # RouterOS REST command endpoints accept (and some versions require)
        # a JSON body when Content-Type is application/json — without it,
        # httpx still sends the default Content-Type from the client headers,
        # so the request becomes "JSON content, zero bytes" and RouterOS may
        # reject it. Pass an empty object to keep header and body consistent.
        return await self._request("POST", "/rest/ip/dns/cache/flush", json={})

    # ------------------------------------------------------------- scripts

    async def script_add(self, name: str, source: str) -> dict[str, Any]:
        result = await self._request(
            "PUT",
            "/rest/system/script",
            json={"name": name, "source": source},
        )
        return result or {}

    async def script_run(self, name: str, *, timeout: float | None = None) -> Any:
        return await self._request(
            "POST",
            "/rest/system/script/run",
            json={".id": name},
            timeout=timeout,
        )

    async def script_remove(self, script_id: str) -> None:
        await self._request("DELETE", f"/rest/system/script/{script_id}")
