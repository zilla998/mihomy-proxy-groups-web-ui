"""Unit tests for :mod:`backend.workflow`.

These tests use :class:`AsyncMock` for both the MikroTik and GitHub clients so
the workflow logic can be exercised without touching httpx or respx. The
tests focus on event sequencing, state-transitions and per-step behaviour.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.github import GithubClient, GithubClientError, GithubError
from backend.mihomo import MihomoClient, MihomoTimeout
from backend.mikrotik import MikrotikClient, MikrotikError, MikrotikTimeout
from backend.workflow import (
    AddGroupWorkflow,
    RemoveGroupWorkflow,
    WorkflowError,
    _build_dns_fwd_rsc,
    _parse_geosite_list,
)


def _mock_mikrotik() -> AsyncMock:
    return AsyncMock(spec=MikrotikClient)


def _mock_github() -> AsyncMock:
    return AsyncMock(spec=GithubClient)


def _mock_mihomo() -> AsyncMock:
    m = AsyncMock(spec=MihomoClient)
    # ``wait_started`` returns the (trimmed) welcome body on success.
    m.wait_started.return_value = '{"hello":"clash.meta"}'
    # ``wait_providers_ready`` returns the providers map on success.
    m.wait_providers_ready.return_value = {
        "geosite-youtube": {
            "vehicleType": "HTTP",
            "updatedAt": "2026-05-08T00:00:00Z",
        }
    }
    return m


async def _collect(workflow: Any) -> list[dict[str, Any]]:
    return [event async for event in workflow.run()]


def _wire_happy_path(m: AsyncMock, *, group_value: str = "youtube") -> None:
    """Default mocks for a happy add/remove workflow."""

    m.find_env.return_value = {".id": "*1", "key": "GROUP", "value": group_value}
    m.set_env.return_value = {".id": "*1"}
    m.add_env.return_value = {".id": "*42"}
    m.list_envs.return_value = []
    m.script_add.return_value = {".id": "*99"}
    m.script_run.return_value = None
    m.script_remove.return_value = None
    m.find_container.return_value = {".id": "*c", "status": "running"}
    m.stop_container.return_value = None
    m.start_container.return_value = None
    m.flush_dns_cache.return_value = None
    m.wait_container_status.side_effect = [
        {".id": "*c", "status": "stopped"},
        {".id": "*c", "status": "running"},
    ]


# ----------------------------------------------------------------------- ADD


async def test_add_group_happy_path() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    # First find_env -> GROUP exists; second -> TELEGRAM_GEOSITE missing
    m.find_env.side_effect = [
        {".id": "*1", "key": "GROUP", "value": "youtube"},
        None,
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.t.me\n+.telegram.org\n"

    wf = AddGroupWorkflow(
        m,
        g,
        name="telegram",
        rule_kind="GEOSITE",
        rule_value="telegram",
        envs_list="MihomoProxyRoS",
        container_comment="MihomoProxyRoS",
        wait_stopped_timeout=5.0,
        wait_running_timeout=5.0,
    )
    events = await _collect(wf)

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
        "flush_dns",
    ]
    assert all(s["status"] == "pending" for s in events[0]["steps"])
    assert events[-1] == {"type": "done", "ok": True, "failed_step": None}

    # GROUP env updated to youtube,telegram
    m.set_env.assert_any_call("*1", "youtube,telegram")
    m.add_env.assert_any_call("MihomoProxyRoS", "TELEGRAM_GEOSITE", "telegram")
    g.fetch_geosite_list.assert_awaited_once_with("telegram")
    m.flush_dns_cache.assert_awaited_once()


async def test_add_group_already_in_group_skips_set_env() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="telegram,youtube")
    # First find_env -> GROUP, second -> TELEGRAM_GEOSITE (none)
    m.find_env.side_effect = [
        {".id": "*1", "key": "GROUP", "value": "telegram,youtube"},
        None,
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.t.me\n"

    wf = AddGroupWorkflow(
        m, g, name="telegram",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    # set_env should NOT be called since 'telegram' is already in GROUP
    m.set_env.assert_not_called()


async def test_add_group_no_existing_group_env_creates_it() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    # GROUP missing, TELEGRAM_GEOSITE missing
    m.find_env.side_effect = [None, None]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.t.me\n"

    wf = AddGroupWorkflow(
        m, g, name="telegram",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    m.add_env.assert_any_call("L", "GROUP", "telegram")
    m.add_env.assert_any_call("L", "TELEGRAM_GEOSITE", "telegram")


async def test_add_group_existing_rule_env_with_different_value_updates() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.find_env.side_effect = [
        {".id": "*1", "key": "GROUP", "value": "youtube"},
        {".id": "*9", "key": "TELEGRAM_GEOSITE", "value": "old"},
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.t.me\n"

    wf = AddGroupWorkflow(
        m, g, name="telegram", rule_value="new",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    # set_env called twice: once for GROUP, once for TELEGRAM_GEOSITE
    set_env_calls = [c.args for c in m.set_env.call_args_list]
    assert ("*1", "youtube,telegram") in set_env_calls
    assert ("*9", "new") in set_env_calls


async def test_add_group_fetch_list_failure_stops_before_container_changes() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.side_effect = GithubClientError("offline")

    wf = AddGroupWorkflow(
        m, g, name="telegram",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    done = events[-1]
    assert done == {
        "type": "done",
        "ok": False,
        "failed_step": "fetch_geosite_list",
    }
    # Container methods never called since we stopped at fetch_geosite_list
    m.stop_container.assert_not_called()
    m.start_container.assert_not_called()
    m.flush_dns_cache.assert_not_called()
    # The errored step should carry a "github: ..." message.
    err_step = next(
        e for e in events if e["type"] == "step" and e["step"]["status"] == "error"
    )
    assert err_step["step"]["id"] == "fetch_geosite_list"
    assert "github" in err_step["step"]["message"]


async def test_add_group_invalid_rule_kind_raises() -> None:
    m = _mock_mikrotik()
    g = _mock_github()
    with pytest.raises(WorkflowError):
        AddGroupWorkflow(
            m, g, name="x", rule_kind="BOGUS",
            envs_list="L", container_comment="C",
        )


async def test_add_group_empty_name_raises() -> None:
    m = _mock_mikrotik()
    g = _mock_github()
    with pytest.raises(WorkflowError):
        AddGroupWorkflow(
            m, g, name="   ",
            envs_list="L", container_comment="C",
        )


async def test_add_group_container_missing_fails_before_env_mutation() -> None:
    """A missing container must abort the workflow at the FIRST step, before
    any persistent state (GROUP env, <NAME>_<KIND> env, .rsc forwarders) is
    mutated — otherwise the operator is left to clean up by hand."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.find_container.return_value = None
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="MissingComment",
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["ok"] is False
    assert done["failed_step"] == "update_group_env"
    # No env / .rsc / script writes must have happened.
    m.set_env.assert_not_called()
    m.add_env.assert_not_called()
    g.fetch_geosite_list.assert_not_called()
    m.script_add.assert_not_called()


async def test_add_group_wait_timeout_surfaces_as_error() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.wait_container_status.side_effect = MikrotikTimeout("did not stop in time")
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["ok"] is False
    assert done["failed_step"] == "wait_stopped"
    err_step = next(
        e for e in events if e["type"] == "step" and e["step"]["status"] == "error"
    )
    assert "timeout" in err_step["step"]["message"]


async def test_add_group_script_add_missing_id_fails_loudly() -> None:
    """``script_remove`` accepts only ``.id``-form identifiers; falling back
    to the textual script name would 404 and leak the just-created script.
    A missing ``.id`` in the PUT response must be a hard failure."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.script_add.return_value = {}  # no .id
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is False
    assert events[-1]["failed_step"] == "run_router_script"
    m.script_run.assert_not_called()
    m.script_remove.assert_not_called()


async def test_add_group_script_remove_failure_does_not_mask_run_success() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.script_remove.side_effect = MikrotikError(500, "remove broke")
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    # run_router_script should still finish OK because remove is best-effort
    assert events[-1]["ok"] is True


async def test_add_group_emits_running_then_ok_per_step() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    step_events = [e for e in events if e["type"] == "step"]
    # 9 steps, two emissions each (running, ok)
    assert len(step_events) == 18
    statuses = [e["step"]["status"] for e in step_events]
    for i in range(0, len(statuses), 2):
        assert statuses[i] == "running"
        assert statuses[i + 1] == "ok"


# -------------------------------------------------------------------- REMOVE


async def test_remove_group_happy_path() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="telegram,youtube")
    m.list_envs.return_value = [
        {".id": "*10", "key": "TELEGRAM_GEOSITE", "value": "telegram"},
        {".id": "*11", "key": "TELEGRAM_DOMAIN", "value": "t.me"},
        {".id": "*12", "key": "YOUTUBE_GEOSITE", "value": "youtube"},
        {".id": "*13", "key": "GROUP", "value": "telegram,youtube"},
    ]

    wf = RemoveGroupWorkflow(
        m, name="telegram",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1] == {"type": "done", "ok": True, "failed_step": None}
    # GROUP env reduced to just 'youtube'
    m.set_env.assert_any_call("*1", "youtube")
    # Both TELEGRAM_* envs removed; YOUTUBE_GEOSITE untouched
    removed_ids = sorted(c.args[0] for c in m.remove_env.call_args_list)
    assert removed_ids == ["*10", "*11"]


async def test_remove_group_not_in_group_skips_set_env() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="youtube")
    m.list_envs.return_value = []

    wf = RemoveGroupWorkflow(
        m, name="telegram",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    m.set_env.assert_not_called()


async def test_remove_group_no_group_env_at_all_succeeds() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.find_env.return_value = None
    m.list_envs.return_value = []

    wf = RemoveGroupWorkflow(
        m, name="telegram",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True


async def test_remove_group_container_missing_fails_before_env_mutation() -> None:
    """Symmetric to the add-flow check: a missing container must abort before
    GROUP env shrinks or <NAME>_* envs are deleted, otherwise the still-running
    container would keep its old (now-stale) view of the config."""

    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="telegram")
    m.list_envs.return_value = []
    m.find_container.return_value = None

    wf = RemoveGroupWorkflow(
        m, name="telegram",
        envs_list="L", container_comment="MissingComment",
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["ok"] is False
    assert done["failed_step"] == "update_group_env"
    m.set_env.assert_not_called()
    m.remove_env.assert_not_called()


async def test_remove_group_clears_all_name_prefixed_envs() -> None:
    """Plan: ``удаляет <NAME>_* envs`` — must drop every prefixed env, not
    just the five rule-kind suffixes the UI knows how to add."""

    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="telegram")
    m.list_envs.return_value = [
        {".id": "*10", "key": "TELEGRAM_GEOSITE", "value": "telegram"},
        {".id": "*11", "key": "TELEGRAM_AS", "value": "as0"},
        {".id": "*12", "key": "TELEGRAM_IPCIDR", "value": "1.1.1.1/32"},
        {".id": "*13", "key": "TELEGRAM_TYPE", "value": "select"},
        {".id": "*14", "key": "TELEGRAM_PROXIES", "value": "MAIN"},
        {".id": "*15", "key": "META_AS", "value": "as1"},  # different group
        {".id": "*16", "key": "GROUP", "value": "telegram"},
    ]

    wf = RemoveGroupWorkflow(
        m, name="telegram",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    removed_ids = sorted(c.args[0] for c in m.remove_env.call_args_list)
    assert removed_ids == ["*10", "*11", "*12", "*13", "*14"]


async def test_add_group_with_hyphen_uses_underscore_in_env_key() -> None:
    """entrypoint.sh translates ``-``→``_`` and uppercases when looking up
    ``<NAME>_<KIND>``, so the stored env key must match that translation."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.find_env.side_effect = [
        {".id": "*1", "key": "GROUP", "value": ""},
        None,
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="my-group",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    m.add_env.assert_any_call("L", "MY_GROUP_GEOSITE", "my-group")


async def test_remove_group_does_not_touch_system_envs_with_shared_prefix() -> None:
    """A group named "log" / "fake" / etc. shares a prefix with system envs
    like LOG_LEVEL or FAKE_IP_RANGE. The remove flow must keep those alone —
    only known per-group suffixes should be erased."""

    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="log")
    m.list_envs.return_value = [
        {".id": "*10", "key": "LOG_TYPE", "value": "select"},      # group env
        {".id": "*11", "key": "LOG_GEOSITE", "value": "log"},      # group env
        {".id": "*12", "key": "LOG_LEVEL", "value": "info"},       # SYSTEM env
        {".id": "*13", "key": "GROUP", "value": "log"},
    ]

    wf = RemoveGroupWorkflow(
        m, name="log",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    removed_ids = sorted(c.args[0] for c in m.remove_env.call_args_list)
    # LOG_LEVEL must NOT be in the removed set; only the per-group suffixes.
    assert removed_ids == ["*10", "*11"]


async def test_remove_group_with_hyphen_matches_translated_prefix() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="my-group,youtube")
    m.list_envs.return_value = [
        {".id": "*10", "key": "MY_GROUP_GEOSITE", "value": "my-group"},
        {".id": "*11", "key": "MY_GROUP_AS", "value": "x"},
        {".id": "*12", "key": "YOUTUBE_GEOSITE", "value": "youtube"},
    ]

    wf = RemoveGroupWorkflow(
        m, name="my-group",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    removed_ids = sorted(c.args[0] for c in m.remove_env.call_args_list)
    assert removed_ids == ["*10", "*11"]


async def test_add_group_run_router_script_uses_returned_id() -> None:
    """``script_run`` must POST the ``.id`` returned by ``script_add``, not
    the textual script name — RouterOS REST identifies items by ``*N``."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.script_add.return_value = {".id": "*99"}
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    m.script_run.assert_awaited_once_with("*99", timeout=600.0)
    m.script_remove.assert_awaited_once_with("*99")


async def test_add_group_run_router_script_uses_configured_timeout() -> None:
    """The plan tracks a regression: amazon.rsc takes longer than
    MIKROTIK_TIMEOUT (10s) to import its hundreds of /ip dns static lines.
    The workflow must thread an explicit, configurable timeout into
    script_run so a long import does not get killed mid-way."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        run_script_timeout=42.0,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    m.script_run.assert_awaited_once_with("*99", timeout=42.0)


async def test_add_group_run_router_script_timeout_surfaces_as_error() -> None:
    """If script_run hits its (long) deadline, the workflow must report an
    error on run_router_script with a ``timeout`` message — not silently fall
    through. The amazon.rsc symptom from the plan was the cleanup running
    DURING execution because the wrong-size timeout cancelled the call."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.script_run.side_effect = MikrotikTimeout("script run timed out")
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        run_script_timeout=5.0,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is False
    assert events[-1]["failed_step"] == "run_router_script"
    err_step = next(
        e for e in events if e["type"] == "step" and e["step"]["status"] == "error"
    )
    assert err_step["step"]["id"] == "run_router_script"
    assert "timeout" in err_step["step"]["message"]
    # Cleanup-on-timeout invariant: even when script_run times out, the
    # finally branch must still call script_remove so we don't leak the
    # imported script on RouterOS.
    m.script_remove.assert_awaited_once_with("*99")


async def test_add_group_prepends_global_initialisation_to_rsc() -> None:
    """The generated .rsc body references ``:global AddressList`` /
    ``:global ForwardTo`` but never initialises them — the daily FWD_update
    scheduler from script21.rsc does. The workflow must initialise them
    itself so it works on a freshly rebooted router before that scheduler
    fires."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.example.com\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="MihomoProxyRoS",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True

    submitted = m.script_add.await_args.args[1]
    assert submitted.startswith(':global AddressList ""\n')
    assert ':global ForwardTo "MihomoProxyRoS"\n' in submitted.split("\n", 2)[1] + "\n"
    # Generated body must still be present after the prelude.
    assert ":local s [:parse" not in submitted  # sanity (we didn't wrap in parse)
    assert 'name="example.com"' in submitted
    assert "/ip dns static" in submitted


async def test_add_group_wait_stopped_failure_triggers_recovery_start() -> None:
    """If wait_stopped times out after stop_container succeeded, the workflow
    must still issue start_container as a best-effort recovery so the
    container is not left stopped."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    # stop_container succeeds, wait_container_status raises on the first call.
    m.wait_container_status.side_effect = MikrotikTimeout("did not stop in time")
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["ok"] is False
    assert done["failed_step"] == "wait_stopped"
    # Recovery must have invoked start_container even though we never reached
    # that step in the normal flow.
    m.start_container.assert_awaited_once()
    # The start_container step should be present as ok with a recovery message.
    start_ok = next(
        (
            e for e in events
            if e.get("type") == "step"
            and e["step"]["id"] == "start_container"
            and e["step"]["status"] == "ok"
        ),
        None,
    )
    assert start_ok is not None
    assert "recovery" in start_ok["step"]["message"]


async def test_add_group_stop_container_failure_still_attempts_recovery() -> None:
    """If stop_container raises (timeout, 5xx, transport blip), RouterOS may
    have already accepted the stop — we cannot tell from the exception alone.
    Recovery must therefore still issue start_container as a best-effort
    so a half-accepted stop never leaves the container down."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.stop_container.side_effect = MikrotikError(500, "boom")
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["failed_step"] == "stop_container"
    # Best-effort recovery fires even though the visible step failed.
    m.start_container.assert_awaited_once()
    start_ok = next(
        (
            e for e in events
            if e.get("type") == "step"
            and e["step"]["id"] == "start_container"
            and e["step"]["status"] == "ok"
        ),
        None,
    )
    assert start_ok is not None
    assert "recovery" in start_ok["step"]["message"]


async def test_add_group_recovery_start_failure_surfaces_error_message() -> None:
    """If recovery start_container itself fails, the original failed step is
    still the reported failure but the start_container step is marked error
    with a recovery-failed message."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.wait_container_status.side_effect = MikrotikTimeout("stop timeout")
    m.start_container.side_effect = MikrotikError(500, "start broke")
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["failed_step"] == "wait_stopped"
    start_err = next(
        (
            e for e in events
            if e.get("type") == "step"
            and e["step"]["id"] == "start_container"
            and e["step"]["status"] == "error"
        ),
        None,
    )
    assert start_err is not None
    assert "recovery failed" in start_err["step"]["message"]


async def test_add_group_hyphen_underscore_collision_rejects_alias() -> None:
    """``foo-bar`` and ``foo_bar`` map to the same ``FOO_BAR_*`` env namespace
    in entrypoint.sh. Adding ``foo_bar`` when ``foo-bar`` is already in GROUP
    must fail loudly at update_group_env so the later steps never overwrite
    ``FOO_BAR_*`` envs or fetch a wrong-named ``foo_bar.rsc``."""

    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="foo-bar")
    m.find_env.side_effect = [
        {".id": "*1", "key": "GROUP", "value": "foo-bar"},
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="foo_bar",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is False
    assert events[-1]["failed_step"] == "update_group_env"
    # GROUP must not be touched, and no later step (rule env / list fetch /
    # script add) may run after the alias collision is detected.
    m.set_env.assert_not_called()
    m.add_env.assert_not_called()
    g.fetch_geosite_list.assert_not_called()
    m.script_add.assert_not_called()


async def test_add_group_case_only_difference_is_noop() -> None:
    """``script21.rsc`` ships a default ``GROUP=YouTube,Telegram,…`` (mixed
    case). The web UI lower-cases user input via ``_normalize_group_name``, so
    a case-only alias must collapse to a no-op rather than raising the
    alias-collision error — there is otherwise no spelling the user can
    submit to satisfy the check (resubmitting the raw alias would just be
    lower-cased again)."""

    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="YouTube,Telegram")
    m.find_env.side_effect = [
        {".id": "*1", "key": "GROUP", "value": "YouTube,Telegram"},
        None,  # TELEGRAM_GEOSITE missing — created by add_rule_env
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="Telegram",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    # GROUP is left alone — only the case-only alias should suppress the write.
    m.set_env.assert_not_called()


async def test_add_group_exact_duplicate_is_noop() -> None:
    """Adding a name that's already in GROUP verbatim is a no-op — only the
    alias case (different raw spelling, same env namespace) errors out."""

    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="foo_bar")
    m.find_env.side_effect = [
        {".id": "*1", "key": "GROUP", "value": "foo_bar"},
        None,  # FOO_BAR_GEOSITE missing — created by add_rule_env
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="foo_bar",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    m.set_env.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "group",
        "GROUP",
        "healthcheck",
        "external-ui",
        "external_ui",
        "fake-ip",
        "fake_ip",
        "sub-link",
        "sub_link1",
        "link",
        "link2",
        # GLOBAL_* and DNS_* are read by entrypoint.sh as system envs for
        # the auto-generated GLOBAL/DNS proxy-groups (entrypoint.sh:1265,
        # :1317). A web-ui group named "global"/"dns" would let the remove
        # flow wipe those system defaults.
        "global",
        "GLOBAL",
        "dns",
        "DNS",
    ],
)
async def test_add_group_rejects_reserved_env_prefix(name: str) -> None:
    """Group names whose env-name form (uppercase + ``-``→``_``) matches a
    system env namespace must be rejected at construction time. Otherwise
    the remove flow would later wipe real entrypoint.sh defaults like
    ``GROUP_TYPE`` / ``HEALTHCHECK_URL`` / ``FAKE_IP_FILTER`` /
    ``SUB_LINK_INTERVAL`` / ``GLOBAL_TYPE`` / ``DNS_PROXIES``."""

    m = _mock_mikrotik()
    g = _mock_github()
    with pytest.raises(WorkflowError, match="reserved"):
        AddGroupWorkflow(
            m, g, name=name,
            envs_list="L", container_comment="C",
        )


@pytest.mark.parametrize(
    "name",
    [
        "group",
        "healthcheck",
        "external-ui",
        "fake-ip",
        "sub-link",
        "link3",
        "global",
        "dns",
    ],
)
async def test_remove_group_rejects_reserved_env_prefix(name: str) -> None:
    m = _mock_mikrotik()
    with pytest.raises(WorkflowError, match="reserved"):
        RemoveGroupWorkflow(
            m, name=name,
            envs_list="L", container_comment="C",
        )


async def test_add_group_stop_timeout_after_routeros_accepted_triggers_recovery() -> None:
    """If stop_container raises post-send (RouterOS accepted but the response
    timed out), the run-loop sees only the timeout and would skip recovery if
    the ``stop_issued`` flag were set after success. The flag is now armed
    inside the handler before the await, so recovery still fires."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.stop_container.side_effect = MikrotikTimeout("read timed out after send")
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["ok"] is False
    assert done["failed_step"] == "stop_container"
    # Recovery start fires even though the visible stop step never reported ok.
    m.start_container.assert_awaited_once()


async def test_remove_group_with_underscore_removes_hyphen_entry() -> None:
    """Symmetric to the add-collision check: removing ``foo_bar`` must also
    drop a ``foo-bar`` entry already in GROUP, since both share the same
    ``FOO_BAR_*`` env namespace."""

    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="foo-bar,youtube")
    m.list_envs.return_value = [
        {".id": "*10", "key": "FOO_BAR_GEOSITE", "value": "foo-bar"},
        {".id": "*11", "key": "YOUTUBE_GEOSITE", "value": "youtube"},
    ]

    wf = RemoveGroupWorkflow(
        m, name="foo_bar",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    # GROUP env reduced — foo-bar removed.
    m.set_env.assert_any_call("*1", "youtube")
    removed_ids = sorted(c.args[0] for c in m.remove_env.call_args_list)
    assert removed_ids == ["*10"]


async def test_remove_group_init_lists_seven_steps() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.list_envs.return_value = []

    wf = RemoveGroupWorkflow(
        m, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    init_ids = [s["id"] for s in events[0]["steps"]]
    assert init_ids == [
        "update_group_env",
        "remove_rule_envs",
        "stop_container",
        "wait_stopped",
        "start_container",
        "wait_running",
        "flush_dns",
    ]


# --------------------------------------- TASK 3: tolerant fetch_geosite_list


async def test_add_group_geosite_404_skips_router_script() -> None:
    """A 404 from meta-rules-dat means "no list shipped for this category".
    The workflow must continue rather than abort — the user is allowed to add
    a category (e.g. a custom one) that has no upstream ``.list`` file."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.side_effect = GithubError(404, {"message": "Not Found"})

    wf = AddGroupWorkflow(
        m, g, name="custom",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True

    fetch_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "fetch_geosite_list"
        and e["step"]["status"] == "ok"
    )
    assert "skipped" in fetch_step["step"]["message"]

    # run_router_script must also be ok-skipped (no .rsc to run) — but the
    # subsequent container lifecycle still runs so the new env takes effect.
    run_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "run_router_script"
        and e["step"]["status"] == "ok"
    )
    assert "skipped" in run_step["step"]["message"]
    m.script_add.assert_not_called()
    m.script_run.assert_not_called()
    # Container lifecycle still runs.
    m.stop_container.assert_awaited_once()
    m.start_container.assert_awaited_once()
    m.flush_dns_cache.assert_awaited_once()


async def test_add_group_fetch_returns_only_unsupported_lines_skips_run_router() -> None:
    """A 200 ``.list`` whose content is entirely ``regexp:``/``keyword:``
    /``include:``/comments yields zero parseable domains. The workflow must
    treat it like a 404: the fetch step ok-skips with a clear message,
    ``script_add`` is not called, and the container lifecycle still runs."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = (
        "# header comment\n"
        "regexp:.*\\.example\\.com\n"
        "keyword:foo\n"
        "include:other-list\n"
        "\n"
    )

    wf = AddGroupWorkflow(
        m, g, name="custom", rule_value="weirdcat",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True

    fetch_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "fetch_geosite_list"
        and e["step"]["status"] == "ok"
    )
    msg = fetch_step["step"]["message"]
    assert "no DNS-FWD-able domains" in msg
    assert "weirdcat" in msg

    run_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "run_router_script"
        and e["step"]["status"] == "ok"
    )
    assert "skipped" in run_step["step"]["message"]
    m.script_add.assert_not_called()
    m.script_run.assert_not_called()
    m.stop_container.assert_awaited_once()
    m.start_container.assert_awaited_once()


async def test_add_group_fetch_list_500_still_fails_workflow() -> None:
    """Only 404 is downgraded — 5xx is GitHub being broken and must surface."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.side_effect = GithubError(500, "boom")

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is False
    assert events[-1]["failed_step"] == "fetch_geosite_list"
    m.stop_container.assert_not_called()


@pytest.mark.parametrize("kind", ["DOMAIN", "SUFFIX", "KEYWORD", "GEOIP"])
async def test_add_group_skips_fetch_for_non_geosite_kind(kind: str) -> None:
    """Only ``rule_kind=GEOSITE`` triggers the meta-rules-dat fetch and the
    DNS-FWD script run. Other kinds (DOMAIN/SUFFIX/KEYWORD/GEOIP) record the
    env and cycle the container, but neither fetch nor script_add fires."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()

    wf = AddGroupWorkflow(
        m, g, name="t", rule_kind=kind, rule_value="example.com",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True

    fetch_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "fetch_geosite_list"
        and e["step"]["status"] == "ok"
    )
    assert "skipped" in fetch_step["step"]["message"]
    assert kind in fetch_step["step"]["message"]

    g.fetch_geosite_list.assert_not_awaited()
    m.script_add.assert_not_called()
    m.script_run.assert_not_called()
    # Container still cycles so the new env takes effect.
    m.stop_container.assert_awaited_once()
    m.start_container.assert_awaited_once()
    m.flush_dns_cache.assert_awaited_once()


async def test_add_group_uses_rule_value_for_meta_rules_lookup() -> None:
    """``fetch_geosite_list`` must be called with ``rule_value`` (the upstream
    category name), not the local group ``name`` — operators routinely give a
    group a friendly name distinct from the meta-rules-dat category."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.reddit.com\n"

    wf = AddGroupWorkflow(
        m, g, name="my-group", rule_kind="GEOSITE", rule_value="reddit",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    g.fetch_geosite_list.assert_awaited_once_with("reddit")


# -------------------------------------------------- TASK 6: wait_mihomo_ready


async def test_add_group_init_includes_wait_mihomo_ready_when_configured() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"
    mh = _mock_mihomo()

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        mihomo=mh, mihomo_ready_timeout=5.0,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
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
    mh.wait_providers_ready.assert_awaited_once()
    # flush_dns runs after wait_mihomo_ready.
    m.flush_dns_cache.assert_awaited_once()


async def test_add_group_init_omits_wait_mihomo_ready_when_disabled() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    init_ids = [s["id"] for s in events[0]["steps"]]
    assert "wait_mihomo_ready" not in init_ids
    assert len(init_ids) == 9


async def test_add_group_wait_mihomo_ready_timeout_surfaces_error() -> None:
    """If mihomo never becomes ready, the workflow ends in error on
    wait_mihomo_ready — but the container is already running, so no recovery
    start is issued (that path only triggers when start_container is still
    pending)."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"
    mh = _mock_mihomo()
    mh.wait_providers_ready.side_effect = MihomoTimeout("not ready")

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        mihomo=mh, mihomo_ready_timeout=1.0,
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["ok"] is False
    assert done["failed_step"] == "wait_mihomo_ready"
    err_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "wait_mihomo_ready"
        and e["step"]["status"] == "error"
    )
    assert "timeout" in err_step["step"]["message"]
    # Container was already running before the failure; flush_dns must NOT
    # have run since the workflow stopped at wait_mihomo_ready.
    m.flush_dns_cache.assert_not_called()
    # start_container ran in the normal flow (status ok), so the recovery
    # branch should NOT have called it again.
    assert m.start_container.await_count == 1


async def test_remove_group_init_includes_wait_mihomo_ready_when_configured() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.list_envs.return_value = []
    mh = _mock_mihomo()

    wf = RemoveGroupWorkflow(
        m, name="t",
        envs_list="L", container_comment="C",
        mihomo=mh, mihomo_ready_timeout=5.0,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    init_ids = [s["id"] for s in events[0]["steps"]]
    assert init_ids == [
        "update_group_env",
        "remove_rule_envs",
        "stop_container",
        "wait_stopped",
        "start_container",
        "wait_running",
        "wait_mihomo_ready",
        "flush_dns",
    ]
    mh.wait_providers_ready.assert_awaited_once()


async def test_add_group_wait_running_uses_mihomo_root_when_configured() -> None:
    """When MIHOMO_API_URL is set, ``wait_running`` polls mihomo's root
    endpoint via ``wait_started`` instead of the RouterOS container status
    field — RouterOS REST may keep ``status=<empty>`` for the entire wait
    window on a cold start, which makes a healthy container indistinguishable
    from a stuck one through that lens."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    # Only one wait_container_status call expected: the ``stopped`` poll.
    m.wait_container_status.side_effect = [
        {".id": "*c", "status": "stopped"},
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"
    mh = _mock_mihomo()

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        mihomo=mh,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    mh.wait_started.assert_awaited_once()
    # The mihomo-configured path no longer touches the RouterOS status field
    # for the start-side wait — only the ``stopped`` poll remains.
    statuses = [c.args[1] for c in m.wait_container_status.await_args_list]
    assert statuses == ["stopped"]
    # The wait_running step's ok message uses the fixed ``mihomo / ok`` form.
    wait_running_ok = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "wait_running"
        and e["step"]["status"] == "ok"
    )
    assert wait_running_ok["step"]["message"] == "mihomo / ok"


async def test_add_group_wait_running_mihomo_timeout_surfaces_error() -> None:
    """If mihomo's root endpoint never answers, the wait_running step ends in
    error. start_container has already run in the normal flow — the recovery
    branch in ``run`` only fires when the start_container step is still
    ``pending``, so it does not double-start. flush_dns must not run."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.wait_container_status.side_effect = [
        {".id": "*c", "status": "stopped"},
    ]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"
    mh = _mock_mihomo()
    mh.wait_started.side_effect = MihomoTimeout(
        "mihomo / not ready within 60.0s; last error: ConnectError"
    )

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        mihomo=mh,
        wait_running_timeout=1.0,
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["ok"] is False
    assert done["failed_step"] == "wait_running"
    err_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "wait_running"
        and e["step"]["status"] == "error"
    )
    assert "timeout" in err_step["step"]["message"]
    # Container was already started in the normal flow; the recovery branch
    # must not call start_container again because that step is already ok.
    assert m.start_container.await_count == 1
    # flush_dns must not run — the workflow stopped at wait_running.
    m.flush_dns_cache.assert_not_called()
    # wait_mihomo_ready must not have been reached either.
    mh.wait_providers_ready.assert_not_called()


async def test_add_group_wait_running_uses_strict_path_when_mihomo_disabled() -> None:
    """No mihomo ⇒ keep the strict ``status==running`` poll. Without a
    follow-up readiness probe, treating ``starting`` as success would let
    flush_dns run before mihomo is actually serving."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    statuses = [c.args[1] for c in m.wait_container_status.await_args_list]
    assert statuses == ["stopped", "running"]


async def test_add_group_separate_timeouts_for_stop_and_start() -> None:
    """The plan splits CONTAINER_WAIT_TIMEOUT into a stop-side window and a
    larger start-side window; each call must consume its own timeout."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        wait_stopped_timeout=11.0,
        wait_running_timeout=222.0,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True

    calls = m.wait_container_status.await_args_list
    assert calls[0].args[1] == "stopped"
    assert calls[0].kwargs["timeout"] == 11.0
    assert calls[1].args[1] == "running"
    assert calls[1].kwargs["timeout"] == 222.0


async def test_add_group_fast_path_uses_wait_running_timeout() -> None:
    """The mihomo ``wait_started`` call must receive ``wait_running_timeout``
    (the start-side budget), not the stop-side one."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.wait_container_status.side_effect = [{".id": "*c", "status": "stopped"}]
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"
    mh = _mock_mihomo()

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        mihomo=mh,
        wait_stopped_timeout=11.0,
        wait_running_timeout=222.0,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True

    assert m.wait_container_status.await_args_list[0].kwargs["timeout"] == 11.0
    mh.wait_started.assert_awaited_once()
    assert mh.wait_started.await_args.kwargs["timeout"] == 222.0


async def test_add_group_recovery_with_separate_timeouts() -> None:
    """Recovery (best-effort start_container after wait_stopped failure) still
    fires regardless of how the two timeouts are sized."""

    m = _mock_mikrotik()
    _wire_happy_path(m)
    m.wait_container_status.side_effect = MikrotikTimeout("did not stop in time")
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        wait_stopped_timeout=3.0,
        wait_running_timeout=300.0,
    )
    events = await _collect(wf)
    done = events[-1]
    assert done["ok"] is False
    assert done["failed_step"] == "wait_stopped"
    m.start_container.assert_awaited_once()


async def test_remove_group_wait_running_uses_mihomo_root_when_configured() -> None:
    """Symmetric to the add-flow mihomo-root test."""

    m = _mock_mikrotik()
    _wire_happy_path(m, group_value="t")
    m.list_envs.return_value = []
    m.wait_container_status.side_effect = [{".id": "*c", "status": "stopped"}]
    mh = _mock_mihomo()

    wf = RemoveGroupWorkflow(
        m, name="t",
        envs_list="L", container_comment="C",
        mihomo=mh,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    mh.wait_started.assert_awaited_once()


# ----------------------------------------- TASK 1: geosite .list parser/render


def test_parse_geosite_list_pure_subdomain_form() -> None:
    """Reddit-style ``+.X`` lines collapse to bare hosts in source order."""

    text = "+.reddit.com\n+.redditstatic.com\n+.redditmedia.com\n"
    assert _parse_geosite_list(text) == [
        "reddit.com",
        "redditstatic.com",
        "redditmedia.com",
    ]


def test_parse_geosite_list_mixed_forms_skips_unsupported_and_comments() -> None:
    """Plain hosts join ``+.X`` hosts; ``regexp:`` / ``keyword:`` / ``include:``
    prefixes, blank lines and ``#`` comments are dropped (none of those map to a
    single ``/ip dns static add ... type=FWD`` entry)."""

    text = (
        "# header comment\n"
        "\n"
        "+.example.com\n"
        "plain.example.org\n"
        "regexp:.*\\.example\\.net\n"
        "keyword:tracker\n"
        "include:other-list\n"
        "   \n"
        "  # indented comment\n"
        "+.another.example\n"
    )
    assert _parse_geosite_list(text) == [
        "example.com",
        "plain.example.org",
        "another.example",
    ]


def test_parse_geosite_list_dedup_preserves_first_occurrence() -> None:
    """Duplicates (including ``+.X``/plain-``X`` collisions) drop on first sight
    while preserving the order of first appearance."""

    text = (
        "+.example.com\n"
        "other.example\n"
        "example.com\n"          # plain dup of +.example.com
        "+.other.example\n"      # +.X dup of plain
        "+.third.example\n"
        "+.example.com\n"        # exact dup
    )
    assert _parse_geosite_list(text) == [
        "example.com",
        "other.example",
        "third.example",
    ]


def test_build_dns_fwd_rsc_empty_domains_returns_empty_string() -> None:
    assert _build_dns_fwd_rsc([], comment="anything") == ""


def test_build_dns_fwd_rsc_starts_with_single_ip_dns_static_header() -> None:
    out = _build_dns_fwd_rsc(["a.example", "b.example"], comment="grp")
    assert out.startswith("/ip dns static\n")
    # Only one header line, even with multiple domains.
    assert out.count("/ip dns static") == 1


def test_build_dns_fwd_rsc_one_if_per_domain_in_order() -> None:
    """Each domain becomes one ``:if … add … type=FWD name="X"`` line, in the
    order supplied."""

    out = _build_dns_fwd_rsc(["a.example", "b.example", "c.example"], comment="g")
    lines = [l for l in out.splitlines() if l.startswith(":if ")]
    assert len(lines) == 3
    # name= must appear in source order
    names = [l.split('name="')[-1].rstrip('" }') for l in lines]
    assert names == ["a.example", "b.example", "c.example"]
    # Each line carries the expected RouterOS shape verbatim.
    for line, domain in zip(lines, ["a.example", "b.example", "c.example"], strict=True):
        assert line == (
            f':if ([:len [find name="{domain}"]] = 0) do={{ '
            f"add address-list=$AddressList forward-to=$ForwardTo "
            f'comment="g" match-subdomain=yes type=FWD '
            f'name="{domain}" }}'
        )


def test_build_dns_fwd_rsc_escapes_quote_in_comment() -> None:
    """A quote in the comment must not break out of the RouterOS string
    literal — escape with a backslash."""

    out = _build_dns_fwd_rsc(["x.example"], comment='evil"name')
    assert 'comment="evil\\"name"' in out
    # No raw unescaped quote sequence inside the comment field.
    assert 'comment="evil"name"' not in out


def test_build_dns_fwd_rsc_escapes_backslash_in_comment() -> None:
    """A literal backslash in the comment must double-up so RouterOS doesn't
    interpret the next character as an escape sequence."""

    out = _build_dns_fwd_rsc(["x.example"], comment="back\\slash")
    assert 'comment="back\\\\slash"' in out


def test_build_dns_fwd_rsc_escapes_quote_in_domain() -> None:
    """Defense-in-depth: even though the parser only emits hostname-shaped
    domains, a stray ``"`` from a malformed upstream ``.list`` must not close
    the RouterOS string literal mid-line."""

    out = _build_dns_fwd_rsc(['evil"host.com'], comment="g")
    assert 'name="evil\\"host.com"' in out
    assert 'find name="evil\\"host.com"' in out


def test_build_dns_fwd_rsc_escapes_dollar_in_domain() -> None:
    """RouterOS interprets ``$name`` inside double-quoted strings as variable
    substitution. A malformed ``.list`` entry containing ``$`` must be passed
    through as a literal so it can never resolve to a global like
    ``$AddressList``."""

    out = _build_dns_fwd_rsc(["bad$AddressList.com"], comment="g")
    assert 'name="bad\\$AddressList.com"' in out
    assert "$AddressList.com" not in out.replace("\\$AddressList.com", "")


def test_build_dns_fwd_rsc_escapes_bracket_in_domain() -> None:
    """RouterOS interprets ``[cmd]`` inside double-quoted strings as command
    substitution — ``[/system reboot]`` would actually execute. A malformed
    ``.list`` entry containing ``[`` must be passed through as a literal."""

    out = _build_dns_fwd_rsc(["bad[/system reboot].com"], comment="g")
    assert 'name="bad\\[/system reboot].com"' in out
    # The bare ``[`` (without a leading backslash) must not appear inside the
    # name="..." literal at all.
    assert 'name="bad[/system' not in out


def test_build_dns_fwd_rsc_escapes_dollar_and_bracket_in_comment() -> None:
    """The comment is operator-controlled (``MIKROTIK_CONTAINER_COMMENT``)
    but applies the same escape policy for consistency."""

    out = _build_dns_fwd_rsc(
        ["x.example"], comment="MihomoProxyRoS$evil[/system]"
    )
    assert 'comment="MihomoProxyRoS\\$evil\\[/system]"' in out


def test_parse_geosite_list_strips_utf8_bom_on_first_line() -> None:
    """Some hosts (and a stray editor) prepend a UTF-8 BOM. The parser must
    not treat ``\\ufeff+.example.com`` as a literal junk hostname."""

    domains = _parse_geosite_list("﻿+.example.com\n+.other.com\n")
    assert domains == ["example.com", "other.com"]


async def test_add_group_wait_mihomo_ready_message_reports_provider_count() -> None:
    m = _mock_mikrotik()
    _wire_happy_path(m)
    g = _mock_github()
    g.fetch_geosite_list.return_value = "+.x\n"
    mh = _mock_mihomo()
    mh.wait_providers_ready.return_value = {
        "geosite-youtube": {
            "vehicleType": "HTTP",
            "updatedAt": "2026-05-08T00:00:00Z",
        },
        "geoip-ru": {
            "vehicleType": "HTTP",
            "updatedAt": "2026-05-08T00:00:00Z",
        },
        "inline-rules": {
            "vehicleType": "Inline",
            "updatedAt": "",
        },
    }

    wf = AddGroupWorkflow(
        m, g, name="t",
        envs_list="L", container_comment="C",
        mihomo=mh,
    )
    events = await _collect(wf)
    assert events[-1]["ok"] is True
    ok_step = next(
        e for e in events
        if e["type"] == "step"
        and e["step"]["id"] == "wait_mihomo_ready"
        and e["step"]["status"] == "ok"
    )
    # Inline providers are counted as exempt (matches
    # MihomoClient.wait_providers_ready semantics) — the message reports only
    # the providers we actually waited on.
    assert "2" in ok_step["step"]["message"]
