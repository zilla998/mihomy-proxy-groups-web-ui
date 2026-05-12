"""Workflow engine for add/remove of a proxy group on mihomo-proxy-ros.

Each workflow exposes a single :meth:`run` async generator that yields a
sequence of events the UI consumes via SSE. The first event is an ``init``
snapshot listing every step with ``status=pending``. Subsequent events are
``step`` updates (``running`` → ``ok`` / ``error``) and a final ``done``
event carrying the overall outcome and (on failure) the offending step id.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from backend.github import GithubClient, GithubClientError, GithubError
from backend.mihomo import MihomoClient, MihomoClientError, MihomoTimeout
from backend.mikrotik import MikrotikClient, MikrotikClientError, MikrotikTimeout


StepStatus = Literal["pending", "running", "ok", "error"]


@dataclass
class Step:
    id: str
    title: str
    status: StepStatus = "pending"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "message": self.message,
        }


VALID_KINDS: tuple[str, ...] = ("GEOSITE", "GEOIP", "DOMAIN", "SUFFIX", "KEYWORD")

# Per-group ENV suffixes recognised by entrypoint.sh. Used as an allowlist when
# removing envs so that a group whose name happens to share a prefix with a
# system ENV (e.g. group "log" / "fake" / "dns") cannot wipe out unrelated
# settings like LOG_LEVEL or FAKE_IP_RANGE.
GROUP_ENV_SUFFIXES: frozenset[str] = frozenset(
    {
        "TYPE",
        "USE",
        "FILTER",
        "EXCLUDE",
        "EXCLUDE_TYPE",
        "PROXIES",
        "TOLERANCE",
        "URL",
        "URL_STATUS",
        "INTERVAL",
        "STRATEGY",
        "ICON",
        "HIDDEN",
        "PRIORITY",
        "GEOSITE",
        "GEOIP",
        "AS",
        "DOMAIN",
        "SUFFIX",
        "KEYWORD",
        "IPCIDR",
        "SRCIPCIDR",
        "DNS",
        "DSCP",
    }
)

# Env-name prefixes used by entrypoint.sh's own system envs that share at
# least one suffix with GROUP_ENV_SUFFIXES. A group whose env-name form
# (uppercase, hyphen→underscore) matches one of these would let the remove
# flow delete real system envs. Examples:
#   group "group"      → GROUP_TYPE / GROUP_URL / GROUP_INTERVAL / ... (defaults)
#   group "healthcheck" → HEALTHCHECK_URL / HEALTHCHECK_INTERVAL / ...
#   group "external-ui" → EXTERNAL_UI_URL
#   group "fake-ip"     → FAKE_IP_FILTER
#   group "sub-link"    → SUB_LINK_INTERVAL
#   group "global"     → GLOBAL_TYPE / GLOBAL_URL / ... (entrypoint.sh:1265)
#   group "dns"        → DNS_TYPE / DNS_PROXIES / ... (entrypoint.sh:1317)
RESERVED_ENV_PREFIXES: frozenset[str] = frozenset(
    {
        "GROUP",
        "HEALTHCHECK",
        "EXTERNAL_UI",
        "FAKE_IP",
        "SUB_LINK",
        "LINK",
        "GLOBAL",
        "DNS",
    }
)

# entrypoint.sh dynamically reads ``${name}_INTERVAL`` for each numbered
# SUB_LINK / LINK instance (collect_cmds), so SUB_LINK1, LINK2, etc. are
# also reserved.
_RESERVED_NUMBERED_RE = re.compile(r"^(?:SUB_LINK|LINK)\d+$")


def _is_reserved_env_prefix(env_prefix: str) -> bool:
    return env_prefix in RESERVED_ENV_PREFIXES or bool(
        _RESERVED_NUMBERED_RE.match(env_prefix)
    )


class WorkflowError(Exception):
    """Raised for invalid workflow configuration or internal invariant breaks."""


def _normalize_group_name(name: str) -> str:
    name = name.strip().lower()
    if not name:
        raise WorkflowError("group name is empty")
    env_prefix = _env_name(name)
    if _is_reserved_env_prefix(env_prefix):
        # Both add and remove paths refuse reserved prefixes — even if a name
        # somehow ended up in GROUP via SSH, the web-ui's blanket ``<NAME>_*``
        # remove sweep would clobber system defaults like GROUP_TYPE,
        # HEALTHCHECK_URL, FAKE_IP_FILTER. The operator must edit those by
        # hand if they really want to.
        raise WorkflowError(
            f"group name '{name}' would collide with the system env "
            f"namespace '{env_prefix}_*' (reserved by entrypoint.sh)"
        )
    return name


def _split_group_value(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _join_group_value(parts: list[str]) -> str:
    return ",".join(parts)


def _env_name(name: str) -> str:
    """Translate a group name into the form mihomo's entrypoint looks up.

    entrypoint.sh does ``tr '-' '_' | tr '[:lower:]' '[:upper:]'`` for every
    ``<NAME>_*`` ENV lookup, so a GROUP entry of ``my-group`` is read as
    ``MY_GROUP_GEOSITE``. Stored ENV keys must mirror that translation.
    """

    return name.upper().replace("-", "_")


def _rule_env_key(name: str, kind: str) -> str:
    return f"{_env_name(name)}_{kind.upper()}"


def _parse_geosite_list(text: str) -> list[str]:
    """Extract DNS-FWD-able domain names from a meta-rules-dat ``.list`` file.

    Order is preserved as in the source; duplicates are dropped on first sight.
    Recognised forms: ``+.example.com`` (subdomain match) and bare
    ``example.com``. Both collapse to the bare host. Lines starting with
    ``regexp:``, ``keyword:`` or ``include:``, blank lines and ``#`` comments
    are skipped — none of those map cleanly onto a single RouterOS DNS-FWD
    static entry.
    """

    # Some upstreams prepend a UTF-8 BOM; strip it from the head so the first
    # line's ``+.`` prefix check succeeds instead of treating ``﻿+.X``
    # as a junk literal hostname.
    if text.startswith("﻿"):
        text = text[1:]
    seen: set[str] = set()
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("regexp:", "keyword:", "include:")):
            continue
        domain = line[2:] if line.startswith("+.") else line
        if not domain or domain in seen:
            continue
        seen.add(domain)
        out.append(domain)
    return out


def _parse_inline_dns_fwd_domains(value: str) -> list[str]:
    """Extract DNS-FWD-able domains from inline DOMAIN/SUFFIX env values.

    ``entrypoint.sh`` accepts comma-separated values for rule envs. DNS-FWD can
    only mirror positive domain-like entries, so negated ``!example.com``
    tokens are ignored and duplicate hosts collapse on first sight.
    """

    seen: set[str] = set()
    out: list[str] = []
    for raw in value.split(","):
        token = raw.strip()
        if not token or token.startswith("!"):
            continue
        domain = token[2:] if token.startswith("+.") else token
        if not domain or domain in seen:
            continue
        seen.add(domain)
        out.append(domain)
    return out


def _rsc_escape(value: str) -> str:
    """Escape RouterOS double-quoted string metacharacters.

    Inside ``"..."`` RouterOS interprets ``$name`` as variable substitution
    and ``[cmd]`` as command substitution, so a stray ``$`` or ``[`` in a
    domain name (corrupted upstream ``.list``) would not just break the
    string literal — ``[/system reboot]`` would actually execute. Backslash
    and double-quote are escaped for the same defense-in-depth reason: a
    malformed entry must never close out of the string.
    """

    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("[", "\\[")
    )


def _build_dns_fwd_rsc(domains: list[str], comment: str) -> str:
    """Render a RouterOS ``.rsc`` body that adds DNS-FWD entries for ``domains``.

    The output expects the caller (``_do_run_router_script``) to prepend the
    ``:global AddressList "" / :global ForwardTo "<container>"`` prelude.
    Quotes/backslashes in both ``comment`` and ``domain`` are escaped — even
    though the parser only emits hostname-shaped domains today, a malformed
    upstream ``.list`` should never let a stray ``"`` close the RouterOS
    string literal mid-line.
    """

    if not domains:
        return ""
    safe_comment = _rsc_escape(comment)
    lines = ["/ip dns static"]
    for domain in domains:
        safe_domain = _rsc_escape(domain)
        lines.append(
            f':if ([:len [find name="{safe_domain}"]] = 0) do={{ '
            f"add address-list=$AddressList forward-to=$ForwardTo "
            f'comment="{safe_comment}" match-subdomain=yes type=FWD '
            f'name="{safe_domain}" }}'
        )
    return "\n".join(lines) + "\n"


class _BaseGroupWorkflow:
    """Shared event-emission machinery for add/remove flows."""

    def __init__(
        self,
        mikrotik: MikrotikClient,
        envs_list: str,
        container_comment: str,
        *,
        wait_stopped_timeout: float = 60.0,
        wait_running_timeout: float = 180.0,
        mihomo: "MihomoClient | None" = None,
        mihomo_ready_timeout: float = 90.0,
    ) -> None:
        self.mikrotik = mikrotik
        self.envs_list = envs_list
        self.container_comment = container_comment
        self.wait_stopped_timeout = wait_stopped_timeout
        self.wait_running_timeout = wait_running_timeout
        self.mihomo = mihomo
        self.mihomo_ready_timeout = mihomo_ready_timeout
        self.steps: list[Step] = []
        self._container_id: "str | None" = None
        # Set true the moment the stop request leaves this process — even if
        # the REST call later raises (timeout, 5xx, transport blip), RouterOS
        # may have already accepted it, so the recovery branch must still run.
        self._stop_issued = False

    def _add_step(self, step_id: str, title: str) -> Step:
        s = Step(id=step_id, title=title)
        self.steps.append(s)
        return s

    async def run(self) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "init",
            "steps": [s.to_dict() for s in self.steps],
        }

        failed_step_id: "str | None" = None

        for step in self.steps:
            step.status = "running"
            yield {"type": "step", "step": step.to_dict()}
            try:
                msg = await self._dispatch(step)
                step.status = "ok"
                step.message = msg or ""
                yield {"type": "step", "step": step.to_dict()}
            except Exception as exc:  # noqa: BLE001
                step.status = "error"
                step.message = self._format_exception(exc)
                yield {"type": "step", "step": step.to_dict()}
                failed_step_id = step.id
                break

        if failed_step_id is not None and self._stop_issued:
            # Best-effort restart: once we've sent stop, a subsequent error
            # (e.g. wait_stopped timeout) must not leave the container stopped.
            start_step = next(
                (s for s in self.steps if s.id == "start_container"), None,
            )
            if start_step is not None and start_step.status == "pending":
                start_step.status = "running"
                start_step.message = "recovery: restarting after failure"
                yield {"type": "step", "step": start_step.to_dict()}
                try:
                    await self._do_start_container()
                    start_step.status = "ok"
                    start_step.message = "recovery: container restart issued"
                except Exception as exc:  # noqa: BLE001
                    start_step.status = "error"
                    start_step.message = (
                        f"recovery failed: {self._format_exception(exc)}"
                    )
                yield {"type": "step", "step": start_step.to_dict()}

        if failed_step_id is not None:
            yield {"type": "done", "ok": False, "failed_step": failed_step_id}
            return

        yield {"type": "done", "ok": True, "failed_step": None}

    async def _dispatch(self, step: Step) -> str:
        handler = getattr(self, f"_do_{step.id}", None)
        if handler is None:
            raise WorkflowError(f"no handler for step {step.id!r}")
        result = await handler()
        return result if isinstance(result, str) else ""

    @staticmethod
    def _format_exception(exc: Exception) -> str:
        if isinstance(exc, MikrotikTimeout):
            return f"timeout: {exc}"
        if isinstance(exc, MikrotikClientError):
            return f"mikrotik: {exc}"
        if isinstance(exc, MihomoTimeout):
            return f"timeout: {exc}"
        if isinstance(exc, MihomoClientError):
            return f"mihomo: {exc}"
        if isinstance(exc, GithubClientError):
            return f"github: {exc}"
        if isinstance(exc, WorkflowError):
            return str(exc)
        return f"{type(exc).__name__}: {exc}"

    # ----------------------------------------------- shared container helpers

    async def _resolve_container(self) -> str:
        container = await self.mikrotik.find_container(self.container_comment)
        if container is None:
            raise WorkflowError(
                f"no container found with comment={self.container_comment!r}"
            )
        cid = container.get(".id")
        if not cid:
            raise WorkflowError("container record has no .id field")
        self._container_id = cid
        return cid

    async def _do_stop_container(self) -> str:
        cid = self._container_id or await self._resolve_container()
        # Mark before the await: if the REST call times out *after* RouterOS
        # has accepted the stop, the workflow run-loop sees only the timeout
        # exception and would otherwise skip recovery, leaving the container
        # stopped indefinitely.
        self._stop_issued = True
        await self.mikrotik.stop_container(cid)
        return f"stop sent to {cid}"

    async def _do_wait_stopped(self) -> str:
        if self._container_id is None:
            raise WorkflowError("container id missing")
        result = await self.mikrotik.wait_container_status(
            self._container_id,
            "stopped",
            timeout=self.wait_stopped_timeout,
        )
        return f"status={result.get('status')}"

    async def _do_start_container(self) -> str:
        if self._container_id is None:
            raise WorkflowError("container id missing")
        await self.mikrotik.start_container(self._container_id)
        return f"start sent to {self._container_id}"

    async def _do_wait_running(self) -> str:
        if self._container_id is None:
            raise WorkflowError("container id missing")
        # When MIHOMO_API_URL is configured, poll mihomo's root endpoint
        # directly: RouterOS REST may keep ``status=<empty>`` for the entire
        # wait window on a cold start, so a healthy container looks identical
        # to a stuck one through that lens. ``GET /`` answers as soon as
        # mihomo's HTTP listener binds — including with 401/403 when mihomo
        # has ``secret:`` set and our bearer is missing/wrong — which is the
        # earliest moment we know the container is live.
        if self.mihomo is not None:
            await self.mihomo.wait_started(timeout=self.wait_running_timeout)
            return "mihomo / ok"
        result = await self.mikrotik.wait_container_status(
            self._container_id,
            "running",
            timeout=self.wait_running_timeout,
        )
        return f"status={result.get('status')}"

    async def _do_wait_mihomo_ready(self) -> str:
        if self.mihomo is None:
            raise WorkflowError("mihomo client missing")
        providers = await self.mihomo.wait_providers_ready(
            timeout=self.mihomo_ready_timeout,
        )
        non_inline = sum(
            1
            for entry in providers.values()
            if isinstance(entry, dict) and entry.get("vehicleType") != "Inline"
        )
        return f"providers ready: {non_inline}"

    async def _do_flush_dns(self) -> str:
        await self.mikrotik.flush_dns_cache()
        return "ok"


class AddGroupWorkflow(_BaseGroupWorkflow):
    def __init__(
        self,
        mikrotik: MikrotikClient,
        github: GithubClient,
        *,
        name: str,
        rule_kind: str = "GEOSITE",
        rule_value: "str | None" = None,
        envs_list: str,
        container_comment: str,
        wait_stopped_timeout: float = 60.0,
        wait_running_timeout: float = 180.0,
        mihomo: "MihomoClient | None" = None,
        mihomo_ready_timeout: float = 90.0,
        run_script_timeout: float = 600.0,
    ) -> None:
        super().__init__(
            mikrotik,
            envs_list,
            container_comment,
            wait_stopped_timeout=wait_stopped_timeout,
            wait_running_timeout=wait_running_timeout,
            mihomo=mihomo,
            mihomo_ready_timeout=mihomo_ready_timeout,
        )
        self.github = github
        self.name = _normalize_group_name(name)
        self.run_script_timeout = run_script_timeout
        kind = rule_kind.upper()
        if kind not in VALID_KINDS:
            raise WorkflowError(f"invalid rule_kind: {rule_kind!r}")
        self.rule_kind = kind
        value = (rule_value if rule_value is not None else self.name).strip()
        if not value:
            raise WorkflowError("rule_value is empty")
        self.rule_value = value

        self._rsc_text: "str | None" = None
        self._script_id: "str | None" = None
        self._script_name: "str | None" = None

        self._add_step("update_group_env", f"Add '{self.name}' to GROUP env")
        self._add_step(
            "add_rule_env",
            f"Add {_rule_env_key(self.name, self.rule_kind)}={self.rule_value} env",
        )
        dns_fwd_source = (
            f"Fetch {self.rule_value}.list from meta-rules-dat"
            if self.rule_kind == "GEOSITE"
            else f"Build RouterOS DNS-FWD script from {self.rule_kind} value"
        )
        self._add_step(
            "fetch_geosite_list",
            dns_fwd_source,
        )
        self._add_step("run_router_script", "Run RouterOS DNS-FWD script")
        self._add_step("stop_container", "Stop mihomo-proxy-ros container")
        self._add_step("wait_stopped", "Wait for container to stop")
        self._add_step("start_container", "Start mihomo-proxy-ros container")
        self._add_step("wait_running", "Wait for container to be running")
        # wait_mihomo_ready is the bridge between RouterOS-level ``running`` and
        # mihomo actually serving traffic — only present when the operator has
        # configured MIHOMO_API_URL. If absent, flush_dns runs immediately after
        # wait_running and the upstream resolver may briefly serve fake-ips for
        # destinations whose rule providers haven't downloaded yet.
        if self.mihomo is not None:
            self._add_step(
                "wait_mihomo_ready",
                "Wait for mihomo to load rule providers",
            )
        self._add_step("flush_dns", "Flush MikroTik DNS cache")

    async def _do_update_group_env(self) -> str:
        # Preflight: resolve the container before any persistent state changes.
        # Otherwise a misconfigured container_comment would leave us with the
        # GROUP env updated, the <NAME>_<KIND> env created, and the .rsc
        # forwarders pushed to the router, but no container ever cycled —
        # operator would have to undo all three by hand.
        await self._resolve_container()
        env = await self.mikrotik.find_env(self.envs_list, "GROUP")
        if env is None:
            await self.mikrotik.add_env(self.envs_list, "GROUP", self.name)
            return f"created GROUP={self.name}"
        current = _split_group_value(env.get("value", ""))
        # Compare by env-name form (uppercased + hyphens→underscores) so
        # case differences and "foo-bar" vs "foo_bar" — which share the same
        # <NAME>_* env namespace in entrypoint.sh — collapse to a single group.
        target_env = _env_name(self.name)
        aliases = [p for p in current if _env_name(p) == target_env]
        if aliases:
            # ``self.name`` is already lower-cased by ``_normalize_group_name``,
            # so accept any alias that differs only in case (e.g. existing
            # "Telegram" vs requested "telegram") as a no-op rather than
            # demanding the user re-submit the exact raw spelling — which is
            # impossible since the input is always lower-cased.
            if any(p.lower() == self.name for p in aliases):
                return f"already in GROUP={env.get('value', '')}"
            # A different raw spelling already maps to the same <NAME>_* env
            # namespace. Continuing would let add_rule_env /
            # fetch_geosite_list / run_router_script overwrite the existing
            # group's envs and fetch an unrelated geosite list — refuse so the
            # user picks the canonical spelling already in GROUP.
            raise WorkflowError(
                f"group '{aliases[0]}' is already in GROUP and shares the "
                f"'{target_env}_*' env namespace with '{self.name}'; "
                f"use '{aliases[0]}' instead"
            )
        new_value = _join_group_value([*current, self.name])
        await self.mikrotik.set_env(env[".id"], new_value)
        return f"updated GROUP={new_value}"

    async def _do_add_rule_env(self) -> str:
        key = _rule_env_key(self.name, self.rule_kind)
        existing = await self.mikrotik.find_env(self.envs_list, key)
        if existing is not None:
            if existing.get("value") == self.rule_value:
                return f"already present {key}={self.rule_value}"
            await self.mikrotik.set_env(existing[".id"], self.rule_value)
            return f"updated {key}={self.rule_value}"
        await self.mikrotik.add_env(self.envs_list, key, self.rule_value)
        return f"created {key}={self.rule_value}"

    async def _do_fetch_geosite_list(self) -> str:
        # GEOSITE pulls domain entries from meta-rules-dat. Inline DOMAIN and
        # SUFFIX values already are domain names, so they can be mirrored into
        # RouterOS DNS-FWD directly. GEOIP/KEYWORD do not map to a single
        # ``/ip dns static add ... type=FWD name=...`` row.
        if self.rule_kind in {"DOMAIN", "SUFFIX"}:
            domains = _parse_inline_dns_fwd_domains(self.rule_value)
            if not domains:
                self._rsc_text = None
                return f"no DNS-FWD-able domains in {self.rule_kind} value, skipped"
            self._rsc_text = _build_dns_fwd_rsc(domains, comment=self.name)
            return f"{len(domains)} inline domains"
        if self.rule_kind != "GEOSITE":
            self._rsc_text = None
            return f"skipped (rule_kind={self.rule_kind})"
        # Not every geosite category exists in meta-rules-dat (custom or
        # alpha-only names). A 404 is the normal "no list for this category"
        # signal; downgrade to an ok-skipped step so the operator can still
        # register the env and cycle the container. Other GitHub failures
        # (5xx, rate limit, transport) still raise.
        try:
            text = await self.github.fetch_geosite_list(self.rule_value)
        except GithubError as exc:
            if exc.status_code == 404:
                self._rsc_text = None
                return (
                    f"no geosite list for '{self.rule_value}' in "
                    f"meta-rules-dat, skipped"
                )
            raise
        domains = _parse_geosite_list(text)
        if not domains:
            self._rsc_text = None
            return f"no DNS-FWD-able domains in '{self.rule_value}.list', skipped"
        self._rsc_text = _build_dns_fwd_rsc(domains, comment=self.name)
        return f"{len(domains)} domains"

    async def _do_run_router_script(self) -> str:
        if not self._rsc_text:
            return "skipped (no .rsc generated)"
        # The generated .rsc body references ``:global AddressList`` /
        # ``:global ForwardTo`` but never initialises them — they're designed
        # to run inside FWD_update, the wrapper from script21.rsc that sets
        # both globals first. RouterOS globals are wiped on reboot, so on a
        # freshly-booted router (before the daily 06:30 FWD_update scheduler
        # fires) ``$ForwardTo`` would be empty and every ``add … forward-to=""``
        # would be rejected. Prepend the same initialisation FWD_update uses
        # so this workflow is self-contained.
        prelude = (
            ':global AddressList ""\n'
            f':global ForwardTo "{_rsc_escape(self.container_comment)}"\n'
        )
        wrapped = prelude + self._rsc_text
        # nanosecond suffix avoids name collisions when two adds for the same
        # group are submitted in the same second.
        script_name = f"webui-add-{self.name}-{time.time_ns()}"
        self._script_name = script_name
        added = await self.mikrotik.script_add(script_name, wrapped)
        # RouterOS REST identifies scripts by .id (e.g. "*99"). The PUT
        # response always carries it; treat its absence as a hard failure
        # rather than falling back to the textual name — script_remove
        # accepts only .id-form identifiers, so a name fallback would 404
        # silently and leak the just-created script.
        script_id = added.get(".id")
        if not script_id:
            raise WorkflowError(
                f"script_add response missing .id: {added!r}"
            )
        self._script_id = script_id
        try:
            await self.mikrotik.script_run(
                self._script_id, timeout=self.run_script_timeout
            )
        finally:
            try:
                await self.mikrotik.script_remove(self._script_id)
            except MikrotikClientError:
                # Best-effort cleanup; the run-failure (if any) is still raised.
                pass
        return f"ran {script_name}"


class RemoveGroupWorkflow(_BaseGroupWorkflow):
    def __init__(
        self,
        mikrotik: MikrotikClient,
        *,
        name: str,
        envs_list: str,
        container_comment: str,
        wait_stopped_timeout: float = 60.0,
        wait_running_timeout: float = 180.0,
        mihomo: "MihomoClient | None" = None,
        mihomo_ready_timeout: float = 90.0,
    ) -> None:
        super().__init__(
            mikrotik,
            envs_list,
            container_comment,
            wait_stopped_timeout=wait_stopped_timeout,
            wait_running_timeout=wait_running_timeout,
            mihomo=mihomo,
            mihomo_ready_timeout=mihomo_ready_timeout,
        )
        self.name = _normalize_group_name(name)

        self._add_step("update_group_env", f"Remove '{self.name}' from GROUP env")
        self._add_step("remove_rule_envs", f"Remove {_env_name(self.name)}_* envs")
        self._add_step("stop_container", "Stop mihomo-proxy-ros container")
        self._add_step("wait_stopped", "Wait for container to stop")
        self._add_step("start_container", "Start mihomo-proxy-ros container")
        self._add_step("wait_running", "Wait for container to be running")
        if self.mihomo is not None:
            self._add_step(
                "wait_mihomo_ready",
                "Wait for mihomo to load rule providers",
            )
        self._add_step("flush_dns", "Flush MikroTik DNS cache")

    async def _do_update_group_env(self) -> str:
        # Preflight: resolve the container before any persistent state changes.
        # Otherwise a misconfigured container_comment would leave the GROUP
        # env shrunk and the <NAME>_* envs gone, but the still-running
        # container would keep its previous (now-stale) config until the
        # operator manually restarted it.
        await self._resolve_container()
        env = await self.mikrotik.find_env(self.envs_list, "GROUP")
        if env is None:
            return "GROUP env not present"
        parts = _split_group_value(env.get("value", ""))
        # Match by env-name form so "youtube" / "YouTube" and "foo-bar" /
        # "foo_bar" all resolve to the same group — entrypoint.sh translates
        # all of these to the same <NAME>_* prefix when reading envs.
        target_env = _env_name(self.name)
        if not any(_env_name(p) == target_env for p in parts):
            return f"'{self.name}' not in GROUP={env.get('value', '')}"
        new_value = _join_group_value(
            [p for p in parts if _env_name(p) != target_env]
        )
        await self.mikrotik.set_env(env[".id"], new_value)
        return f"updated GROUP={new_value!r}"

    async def _do_remove_rule_envs(self) -> str:
        # Match <NAME>_<SUFFIX> only when SUFFIX is a known per-group ENV
        # entrypoint.sh actually consults. Otherwise a group named e.g. "log"
        # would have its remove flow wipe LOG_LEVEL — script21.rsc's system
        # envs share prefixes with plausible group names.
        prefix = f"{_env_name(self.name)}_"
        envs = await self.mikrotik.list_envs(self.envs_list)
        removed: list[str] = []
        for env in envs:
            key = env.get("key", "")
            if key == "GROUP" or not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            if suffix not in GROUP_ENV_SUFFIXES:
                continue
            env_id = env.get(".id")
            if env_id:
                await self.mikrotik.remove_env(env_id)
                removed.append(key)
        if not removed:
            return "no rule envs found"
        return f"removed {', '.join(sorted(removed))}"
