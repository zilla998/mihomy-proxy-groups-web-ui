"""FastAPI application — main HTTP entrypoint for the web-ui backend.

Endpoint groups:

* ``/api/health`` — readiness probe that pings the MikroTik REST API.
* ``/api/groups/current`` — JSON listing of currently configured groups.
* ``/api/rules/categories`` — JSON listing of geosite/geoip categories from
  ``MetaCubeX/meta-rules-dat``.
* ``/api/groups/add`` and ``/api/groups/remove`` — Server-Sent Event streams
  driven by the workflow engine in :mod:`backend.workflow`.

The streaming endpoints emit ``data: <json>\\n\\n`` frames per workflow event
and tear down their per-request MikroTik client in a ``finally`` block so
abnormal client disconnects don't leak file descriptors. The GitHub client
is shared across the app lifetime so its category-listing cache actually
applies across requests.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

import asyncio

from backend.config import Settings, get_settings
from backend.github import RULE_KIND_PATHS, GithubClient, GithubClientError
from backend.mihomo import MihomoClient, MihomoClientError
from backend.mikrotik import MikrotikClient, MikrotikClientError
from backend.workflow import (
    GROUP_ENV_SUFFIXES,
    VALID_KINDS,
    AddGroupWorkflow,
    RemoveGroupWorkflow,
    WorkflowError,
    _normalize_group_name,
    _split_group_value,
)


def _payload_str(payload: dict[str, Any], key: str) -> str:
    """Return ``payload[key]`` as a string; reject non-string non-None values.

    A misbehaving client posting ``{"name": 123}`` would otherwise let the
    integer flow into ``.strip()``/``.upper()`` and surface as a 500 with an
    AttributeError traceback — masking the real "bad input" cause behind a
    server-error status code.
    """

    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"invalid {key!r}")
    return value


_GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Rule values are stored as RouterOS env values and are read back by
# entrypoint.sh into the generated config.yaml — newlines, quotes, or other
# YAML/RouterOS metacharacters would either break parsing or inject
# unintended config. Allow only the character set that real geosite names,
# country codes, domains, and substrings actually use.
#
# ``,`` is the separator entrypoint.sh splits on for multi-category envs
# (e.g. ``AI_GEOSITE=category-ai-!cn,openai,google-gemini`` shipped by
# script21.rsc). ``!`` is the geosite negation prefix used in
# ``category-ai-!cn``. Both are in real defaults and must round-trip.
# ``@`` is part of meta-rules-dat category names (e.g. ``steam@cn``,
# ``adobe@ads``, ``category-games@cn``) — selecting one of those from the
# frontend dropdown sends ``rule_value="adobe@ads"`` and must validate.
_RULE_VALUE_RE = re.compile(r"^[A-Za-z0-9._,!@-]+$")


def create_app(settings: "Settings | None" = None) -> FastAPI:
    """Build a FastAPI app bound to the given settings.

    Tests use this to inject a fully-controlled :class:`Settings` instance;
    production wiring at the bottom of this module calls it with no arguments
    so uvicorn can import ``backend.app:app``.
    """

    app = FastAPI(title="mihomo-proxy-ros web-ui")
    app.state.settings = settings or get_settings()
    # Shared GithubClient so the per-instance category-listing cache (24h
    # TTL by default) actually survives across requests — recreating it per
    # call would burn through the unauthenticated 60 req/h quota.
    app.state.github_client = GithubClient()
    # Serializes add/remove workflows so two concurrent SSE streams cannot
    # interleave their read-modify-write of the GROUP env (e.g., a user double-
    # clicking, or a stale browser tab). The workflows are short-lived (~60s)
    # and atomic per group, so a single mutex is enough.
    app.state.workflow_lock = asyncio.Lock()
    # Strong references to detached workflow runner tasks. asyncio's event
    # loop only weakly references tasks, so without this set a runner could
    # be GC'd mid-execution if the SSE client disconnects.
    app.state.runner_tasks: set[asyncio.Task] = set()
    # Tracks the runner that currently holds ``workflow_lock`` so the
    # shutdown handler can drain just that one and cancel any queued
    # runners. The drain timeout is sized for a single workflow; without
    # this, two serialized runners could need ~2x the window and the
    # second would be killed mid-``script_run``.
    app.state.active_runner: asyncio.Task | None = None

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        # Drain any in-flight workflow runners before closing shared resources
        # so a SIGTERM during stop_container/start_container doesn't leave the
        # mihomo container stopped — the very scenario the detached-runner
        # pattern was meant to prevent (client disconnect → container stuck
        # stopped) would otherwise reappear at process exit. The timeout has
        # to span the worst-case workflow duration: run_router_script can
        # block for run_script_timeout (600s default for amazon.rsc-class
        # imports), then wait_stopped consumes up to wait_stopped_timeout,
        # wait_running up to wait_running_timeout, then wait_mihomo_ready
        # up to mihomo_ready_timeout. If we time out earlier, uvicorn's
        # loop teardown cancels the runner mid-script_run and its
        # ``finally`` branch deletes the still-executing RouterOS script —
        # the exact failure mode the longer run_script_timeout default was
        # meant to fix.
        runners = list(app.state.runner_tasks)
        if runners:
            # Queued runners (blocked at ``async with workflow_lock``) have
            # not done any router-side work yet, so cancelling them is safe
            # and prevents them from acquiring the lock late in the drain
            # window and starting a fresh script_run that loop teardown
            # would then interrupt. Only the active runner needs the full
            # workflow window.
            active = app.state.active_runner
            for task in runners:
                if task is not active and not task.done():
                    task.cancel()
            s = app.state.settings
            drain_timeout = max(
                s.run_script_timeout
                + s.wait_stopped_timeout
                + s.wait_running_timeout
                + s.mihomo_ready_timeout
                + 30.0,
                30.0,
            )
            await asyncio.wait(runners, timeout=drain_timeout)
        await app.state.github_client.aclose()

    @app.get("/api/health")
    async def health() -> JSONResponse:
        s: Settings = app.state.settings
        if not s.mikrotik_host:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "MIKROTIK_HOST is not configured"},
            )
        client = _make_mikrotik_client(s)
        try:
            ident = await client.system_identity()
        except MikrotikClientError as exc:
            return JSONResponse(
                status_code=502, content={"ok": False, "error": str(exc)}
            )
        finally:
            await client.aclose()
        body: dict[str, Any] = {"ok": True, "identity": ident}
        # Soft signal — when MIHOMO_API_URL is configured we report the
        # external-controller status alongside MikroTik. A failed mihomo probe
        # does NOT downgrade the response to 502: this endpoint exists mainly
        # for the UI banner ("can we reach the router?") and mihomo is allowed
        # to be down (e.g. container restart in progress).
        mihomo = _make_mihomo_client(s)
        if mihomo is not None:
            try:
                version = await mihomo.version()
                body["mihomo"] = {"ok": True, "version": version}
            except MihomoClientError as exc:
                body["mihomo"] = {"ok": False, "error": str(exc)}
            finally:
                await mihomo.aclose()
        return JSONResponse(body)

    @app.get("/api/groups/current")
    async def groups_current() -> JSONResponse:
        s: Settings = app.state.settings
        if not s.mikrotik_host:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "MIKROTIK_HOST is not configured"},
            )
        client = _make_mikrotik_client(s)
        try:
            envs = await client.list_envs(s.mikrotik_envs_list)
        except MikrotikClientError as exc:
            return JSONResponse(
                status_code=502, content={"ok": False, "error": str(exc)}
            )
        finally:
            await client.aclose()
        groups, rule_envs = _summarize_envs(envs)
        return JSONResponse({"ok": True, "groups": groups, "rule_envs": rule_envs})

    @app.get("/api/rules/categories")
    async def rules_categories(
        kind: str, force_refresh: bool = False
    ) -> JSONResponse:
        kind_upper = (kind or "").upper()
        if kind_upper not in RULE_KIND_PATHS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"invalid 'kind': {kind!r} (expected one of "
                    f"{sorted(RULE_KIND_PATHS)})"
                ),
            )
        github: GithubClient = app.state.github_client
        try:
            categories = await github.list_rule_categories(
                kind_upper, force_refresh=force_refresh
            )
        except GithubClientError as exc:
            return JSONResponse(
                status_code=502, content={"ok": False, "error": str(exc)}
            )
        return JSONResponse(
            {"ok": True, "kind": kind_upper, "categories": categories}
        )

    @app.post("/api/groups/add")
    async def groups_add(payload: dict[str, Any] = Body(...)) -> StreamingResponse:
        s: Settings = app.state.settings
        if not s.mikrotik_host:
            raise HTTPException(
                status_code=503, detail="MIKROTIK_HOST is not configured"
            )
        name = _payload_str(payload, "name").strip()
        if not name or not _GROUP_NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="invalid 'name'")
        # Reject reserved-prefix names up front: otherwise the workflow
        # constructor inside the detached runner would raise WorkflowError,
        # the runner would queue only the sentinel, and the client would see
        # a 200 SSE stream that ends without ``init``/``done`` events.
        try:
            _normalize_group_name(name)
        except WorkflowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rule_kind_raw = _payload_str(payload, "rule_kind") or "GEOSITE"
        rule_kind = rule_kind_raw.upper()
        if rule_kind not in VALID_KINDS:
            raise HTTPException(
                status_code=400, detail=f"invalid 'rule_kind': {rule_kind}"
            )
        # Strip BEFORE the ``or name`` fallback so a whitespace-only payload
        # ("   ") falls back to ``name`` instead of becoming "" and tripping
        # WorkflowError inside the detached runner — that path leaves direct
        # API clients with a 200 stream that just ends without an error event.
        rule_value = (_payload_str(payload, "rule_value").strip() or name).strip()
        if not rule_value or not _RULE_VALUE_RE.match(rule_value):
            raise HTTPException(status_code=400, detail="invalid 'rule_value'")

        def build_workflow(
            mikrotik: MikrotikClient,
            mihomo: "MihomoClient | None",
        ) -> AddGroupWorkflow:
            return AddGroupWorkflow(
                mikrotik,
                app.state.github_client,
                name=name,
                rule_kind=rule_kind,
                rule_value=rule_value,
                envs_list=s.mikrotik_envs_list,
                container_comment=s.mikrotik_container_comment,
                wait_stopped_timeout=s.wait_stopped_timeout,
                wait_running_timeout=s.wait_running_timeout,
                mihomo=mihomo,
                mihomo_ready_timeout=s.mihomo_ready_timeout,
                run_script_timeout=s.run_script_timeout,
            )

        return StreamingResponse(
            _detached_workflow_stream(app, s, build_workflow),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @app.post("/api/groups/remove")
    async def groups_remove(payload: dict[str, Any] = Body(...)) -> StreamingResponse:
        s: Settings = app.state.settings
        if not s.mikrotik_host:
            raise HTTPException(
                status_code=503, detail="MIKROTIK_HOST is not configured"
            )
        name = _payload_str(payload, "name").strip()
        if not name or not _GROUP_NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="invalid 'name'")
        try:
            _normalize_group_name(name)
        except WorkflowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def build_workflow(
            mikrotik: MikrotikClient,
            mihomo: "MihomoClient | None",
        ) -> RemoveGroupWorkflow:
            return RemoveGroupWorkflow(
                mikrotik,
                name=name,
                envs_list=s.mikrotik_envs_list,
                container_comment=s.mikrotik_container_comment,
                wait_stopped_timeout=s.wait_stopped_timeout,
                wait_running_timeout=s.wait_running_timeout,
                mihomo=mihomo,
                mihomo_ready_timeout=s.mihomo_ready_timeout,
            )

        return StreamingResponse(
            _detached_workflow_stream(app, s, build_workflow),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    return app


async def _detached_workflow_stream(
    app: FastAPI,
    s: Settings,
    build_workflow,
) -> AsyncIterator[bytes]:
    """SSE generator that runs the workflow in a detached task.

    A client disconnect (closed tab, network blip) tears down the SSE
    generator at its next ``yield``. If the workflow ran inline, this would
    abandon it mid-step — e.g. the container could be left ``stopped``
    because the ``start_container`` step never ran. Decoupling lets the
    workflow run to completion regardless of whether anyone is watching.
    """

    queue: asyncio.Queue[Any] = asyncio.Queue()
    sentinel = object()

    async def runner() -> None:
        try:
            async with app.state.workflow_lock:
                # Mark this task as the active runner so the shutdown
                # handler knows not to cancel it (queued runners blocked
                # on the lock are cancelled to free up the drain window).
                app.state.active_runner = asyncio.current_task()
                mikrotik = _make_mikrotik_client(s)
                mihomo = _make_mihomo_client(s)
                try:
                    try:
                        workflow = build_workflow(mikrotik, mihomo)
                    except WorkflowError as exc:
                        # Defense-in-depth: route handlers already validate
                        # name/rule_value, but a WorkflowError raised here
                        # would otherwise leave the SSE client with a 200
                        # stream that ends silently. Emit a structured init
                        # + done so the UI shows an error message.
                        await queue.put(
                            {"type": "init", "steps": [], "error": str(exc)},
                        )
                        await queue.put(
                            {
                                "type": "done",
                                "ok": False,
                                "failed_step": None,
                                "error": str(exc),
                            },
                        )
                        return
                    async for event in workflow.run():
                        await queue.put(event)
                finally:
                    try:
                        await mikrotik.aclose()
                    except Exception:
                        pass
                    if mihomo is not None:
                        try:
                            await mihomo.aclose()
                        except Exception:
                            pass
                    if app.state.active_runner is asyncio.current_task():
                        app.state.active_runner = None
        finally:
            await queue.put(sentinel)

    task = asyncio.create_task(runner())
    app.state.runner_tasks.add(task)
    task.add_done_callback(app.state.runner_tasks.discard)

    while True:
        event = await queue.get()
        if event is sentinel:
            return
        yield _format_sse(event)


def _make_mikrotik_client(s: Settings) -> MikrotikClient:
    return MikrotikClient(
        s.mikrotik_host,
        s.mikrotik_user,
        s.mikrotik_password,
        verify_tls=s.mikrotik_verify_tls,
        timeout=s.mikrotik_timeout,
    )


def _make_mihomo_client(s: Settings) -> "MihomoClient | None":
    """Construct a mihomo external-controller client when configured.

    Returns ``None`` when ``MIHOMO_API_URL`` is empty so callers can treat the
    feature as disabled with a single ``is None`` check rather than threading
    a separate "is enabled" flag.
    """

    if not s.mihomo_api_url:
        return None
    return MihomoClient(
        s.mihomo_api_url,
        secret=s.mihomo_api_secret,
        timeout=s.mikrotik_timeout,
    )


def _summarize_envs(
    envs: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, str]]]:
    # Surface every per-group ENV the remove flow would delete (workflow.py's
    # GROUP_ENV_SUFFIXES allowlist), but only for prefixes that actually map
    # to a registered group. Otherwise system envs that share suffixes — e.g.
    # HEALTHCHECK_INTERVAL, EXTERNAL_UI_URL, GROUP_TYPE — would be misreported
    # as group-related.
    group_value = ""
    for env in envs:
        if env.get("key") == "GROUP":
            group_value = env.get("value", "")
            break
    groups = _split_group_value(group_value)
    # Mirror workflow._env_name(): uppercase + hyphen-to-underscore.
    valid_prefixes = {g.upper().replace("-", "_") for g in groups}
    # Longest-first ordering so e.g. ``_EXCLUDE_TYPE`` is tried before
    # ``_TYPE``; otherwise iteration over an unordered ``frozenset`` could
    # match ``TELEGRAM_EXCLUDE_TYPE`` against ``_TYPE`` first, derive prefix
    # ``TELEGRAM_EXCLUDE`` (not a registered group) and hide the env.
    suffixes = tuple(
        f"_{k}" for k in sorted(GROUP_ENV_SUFFIXES, key=len, reverse=True)
    )
    rule_envs: list[dict[str, str]] = []
    for env in envs:
        # RouterOS may surface ``key``/``value`` as null on partially-populated
        # records; coerce to "" so downstream string ops can't AttributeError.
        key_raw = env.get("key")
        value_raw = env.get("value")
        key = key_raw if isinstance(key_raw, str) else ""
        value = value_raw if isinstance(value_raw, str) else ""
        if not key or key == "GROUP":
            continue
        for suffix in suffixes:
            if key.endswith(suffix) and len(key) > len(suffix):
                prefix = key[: -len(suffix)]
                if prefix in valid_prefixes:
                    rule_envs.append({"key": key, "value": value})
                    break
                # Otherwise try shorter suffixes: e.g. group "main-exclude"
                # gives valid_prefixes={"MAIN_EXCLUDE"}, and env
                # MAIN_EXCLUDE_TYPE would peel off ``_EXCLUDE_TYPE`` to leave
                # prefix MAIN (not registered) — the correct match is the
                # shorter ``_TYPE`` leaving prefix MAIN_EXCLUDE.
    return groups, rule_envs


def _format_sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }


app = create_app()
