"""Async client for the Proxmox Backup Server REST API.

Deliberately free of Home Assistant imports: it takes an ``aiohttp`` session
and returns dataclasses, nothing else. That keeps it testable without a running
Home Assistant and leaves the door open to reusing it elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import aiohttp

from .exceptions import (
    PbsApiError,
    PbsAuthError,
    PbsConnectionError,
    PbsNotFoundError,
    PbsPermissionError,
)
from .models import (
    ActiveOperations,
    BackupGroup,
    CertificateInfo,
    DatastoreConfig,
    DatastoreUsage,
    DiskInfo,
    GcStatus,
    JobStatus,
    NodeStatus,
    ServiceInfo,
    Snapshot,
    SubscriptionInfo,
    TaskInfo,
    UpdateInfo,
    VersionInfo,
    as_int,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20
JOB_KINDS = ("prune", "verify", "sync")


class PbsClient:
    """Thin async wrapper around the PBS API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token_id: str,
        token_secret: str,
        *,
        node: str = "localhost",
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Store connection details.

        The session is owned by the caller (Home Assistant hands out a shared
        one), so this class never closes it.
        """
        self._session = session
        self._host = host
        self._port = port
        self._node = node
        self._base = f"https://{host}:{port}/api2/json"
        self._headers = {
            "Authorization": f"PBSAPIToken={token_id}:{token_secret}",
            "Accept": "application/json",
        }
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def base_url(self) -> str:
        """Return the URL of the web interface, used as configuration_url."""
        return f"https://{self._host}:{self._port}"

    @property
    def node(self) -> str:
        """Return the node name. PBS is single node and always localhost."""
        return self._node

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> Any:
        """Perform one API call and return the unwrapped ``data`` field."""
        url = f"{self._base}{path}"
        clean_params = _clean_params(params)
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                params=clean_params,
                json=_clean_body(data) if data else None,
                timeout=self._timeout,
            ) as response:
                return await self._handle(response, path)
        except (TimeoutError, aiohttp.ClientError) as err:
            if retry:
                # One retry only, and only for transport level problems: a PBS
                # restart or a brief network hiccup should not mark every
                # entity unavailable.
                _LOGGER.debug("Retrying %s %s after %s", method, path, err)
                await asyncio.sleep(1)
                return await self._request(
                    method, path, params=params, data=data, retry=False
                )
            raise PbsConnectionError(f"{method} {path} failed: {err}") from err

    @staticmethod
    async def _handle(response: aiohttp.ClientResponse, path: str) -> Any:
        """Translate HTTP status codes into typed exceptions."""
        if response.status == HTTPStatus.UNAUTHORIZED:
            raise PbsAuthError(f"Token rejected for {path}")
        if response.status == HTTPStatus.FORBIDDEN:
            raise PbsPermissionError(path)
        if response.status == HTTPStatus.NOT_FOUND:
            raise PbsNotFoundError(path)
        if response.status >= HTTPStatus.BAD_REQUEST:
            body = (await response.text())[:300]
            raise PbsApiError(f"HTTP {response.status} for {path}: {body}")

        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as err:
            raise PbsApiError(f"Unparsable answer for {path}: {err}") from err

        if not isinstance(payload, dict):
            raise PbsApiError(f"Unexpected answer for {path}: {type(payload)}")
        return payload.get("data")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET one endpoint."""
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        """POST to one endpoint. Used by the action milestone."""
        return await self._request("POST", path, data=data, retry=False)

    async def _put(self, path: str, data: dict[str, Any] | None = None) -> Any:
        """PUT to one endpoint. Used by the action milestone."""
        return await self._request("PUT", path, data=data, retry=False)

    @staticmethod
    def _store_path(store: str) -> str:
        """Return the URL prefix for one datastore."""
        return f"/admin/datastore/{quote(store, safe='')}"

    # ------------------------------------------------------------------
    # node level
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Check that something answers. Requires no privileges at all."""
        await self._get("/ping")
        return True

    async def get_version(self) -> VersionInfo:
        """Return the PBS version."""
        return VersionInfo.from_api(await self._get("/version") or {})

    async def get_node_status(self) -> NodeStatus:
        """Return CPU, memory, swap and root filesystem of the host."""
        return NodeStatus.from_api(await self._get(f"/nodes/{self._node}/status") or {})

    async def get_services(self) -> list[ServiceInfo]:
        """Return the systemd units PBS manages."""
        raw = await self._get(f"/nodes/{self._node}/services") or []
        return [ServiceInfo.from_api(entry) for entry in raw]

    async def get_subscription(self) -> SubscriptionInfo:
        """Return the subscription status."""
        raw = await self._get(f"/nodes/{self._node}/subscription") or {}
        return SubscriptionInfo.from_api(raw)

    async def get_updates(self) -> list[UpdateInfo]:
        """Return pending package updates from the apt cache."""
        raw = await self._get(f"/nodes/{self._node}/apt/update") or []
        return [UpdateInfo.from_api(entry) for entry in raw]

    async def get_certificates(self) -> list[CertificateInfo]:
        """Return the installed TLS certificates."""
        raw = await self._get(f"/nodes/{self._node}/certificates/info") or []
        return [CertificateInfo.from_api(entry) for entry in raw]

    async def get_disks(self) -> list[DiskInfo]:
        """Return the physical disks including their SMART verdict."""
        raw = (
            await self._get(
                f"/nodes/{self._node}/disks/list", {"include-partitions": 0}
            )
            or []
        )
        return [DiskInfo.from_api(entry) for entry in raw]

    async def get_tasks(
        self,
        *,
        limit: int = 100,
        running: bool = False,
        errors: bool = False,
        since: int | None = None,
    ) -> list[TaskInfo]:
        """Return recent tasks, optionally filtered to running or failed ones."""
        params: dict[str, Any] = {"limit": limit}
        if running:
            params["running"] = 1
        if errors:
            params["errors"] = 1
        if since is not None:
            params["since"] = since
        raw = await self._get(f"/nodes/{self._node}/tasks", params) or []
        return [TaskInfo.from_api(entry) for entry in raw]

    async def get_task_status(self, upid: str) -> TaskInfo:
        """Return the status of one task, used to follow triggered actions."""
        raw = (
            await self._get(f"/nodes/{self._node}/tasks/{quote(upid, safe='')}/status")
            or {}
        )
        return TaskInfo.from_api(raw)

    # ------------------------------------------------------------------
    # datastore level
    # ------------------------------------------------------------------

    async def list_datastores(self) -> list[str]:
        """Return the datastores the token may see.

        An empty list is not an error here but is treated as one by the config
        flow: it almost always means the ACL is missing on the user behind the
        token.
        """
        raw = await self._get("/admin/datastore") or []
        return [entry["store"] for entry in raw if isinstance(entry, dict)]

    async def get_datastore_config(self) -> dict[str, DatastoreConfig]:
        """Return the configuration of all datastores, keyed by name."""
        raw = await self._get("/config/datastore") or []
        configs = [DatastoreConfig.from_api(entry) for entry in raw]
        return {config.name: config for config in configs if config.name}

    async def get_datastore_usage(self) -> dict[str, DatastoreUsage]:
        """Return usage and estimated-full date of all datastores."""
        raw = await self._get("/status/datastore-usage") or []
        usages = [DatastoreUsage.from_api(entry) for entry in raw]
        return {usage.store: usage for usage in usages if usage.store}

    async def get_gc_status(self, store: str) -> GcStatus:
        """Return the garbage collection status of one datastore."""
        raw = await self._get(f"{self._store_path(store)}/gc") or {}
        return GcStatus.from_api(raw)

    async def get_active_operations(self, store: str) -> ActiveOperations:
        """Return the number of running reads and writes on a datastore."""
        raw = await self._get(f"{self._store_path(store)}/active-operations") or {}
        return ActiveOperations.from_api(raw)

    async def get_namespaces(self, store: str) -> list[str]:
        """Return all namespaces of a datastore, root included as empty string."""
        raw = await self._get(f"{self._store_path(store)}/namespace") or []
        names = [entry.get("ns", "") for entry in raw if isinstance(entry, dict)]
        return names or [""]

    async def get_groups(self, store: str, namespace: str = "") -> list[BackupGroup]:
        """Return the backup groups of a datastore, one per guest."""
        raw = (
            await self._get(
                f"{self._store_path(store)}/groups", {"ns": namespace or None}
            )
            or []
        )
        return [BackupGroup.from_api(entry, namespace) for entry in raw]

    async def get_snapshots(self, store: str, namespace: str = "") -> list[Snapshot]:
        """Return all snapshots of a datastore in a single call."""
        raw = (
            await self._get(
                f"{self._store_path(store)}/snapshots", {"ns": namespace or None}
            )
            or []
        )
        return [Snapshot.from_api(entry, namespace) for entry in raw]

    async def get_datastore_counts(self, store: str) -> dict[str, int]:
        """Return group and snapshot counts as PBS itself aggregates them."""
        raw = await self._get(f"{self._store_path(store)}/status", {"verbose": 1}) or {}
        counts = raw.get("counts") or {}
        groups = 0
        snapshots = 0
        for value in counts.values():
            if not isinstance(value, dict):
                continue
            groups += as_int(value.get("groups"), 0) or 0
            snapshots += as_int(value.get("snapshots"), 0) or 0
        return {"groups": groups, "snapshots": snapshots}

    # ------------------------------------------------------------------
    # actions
    #
    # Garbage collection, verify and job runs are asynchronous: PBS answers
    # with a UPID and does the work in a worker task. Prune and forget answer
    # synchronously, prune even returning what it would remove, which is what
    # makes a dry run useful.
    # ------------------------------------------------------------------

    async def start_garbage_collection(self, store: str) -> str:
        """Start a garbage collection run and return its UPID."""
        return await self._post(f"{self._store_path(store)}/gc")

    async def start_verify(
        self,
        store: str,
        *,
        namespace: str = "",
        backup_type: str | None = None,
        backup_id: str | None = None,
        backup_time: int | None = None,
        ignore_verified: bool | None = None,
        outdated_after: int | None = None,
    ) -> str:
        """Start a verification run and return its UPID.

        Without backup_type and backup_id the whole datastore is verified.
        """
        return await self._post(
            f"{self._store_path(store)}/verify",
            {
                "ns": namespace or None,
                "backup-type": backup_type,
                "backup-id": backup_id,
                "backup-time": backup_time,
                "ignore-verified": ignore_verified,
                "outdated-after": outdated_after,
            },
        )

    async def run_job(self, kind: str, job_id: str) -> str:
        """Start a configured prune, verify or sync job and return its UPID."""
        if kind not in JOB_KINDS:
            raise PbsApiError(f"Unknown job kind: {kind}")
        return await self._post(f"/admin/{kind}/{quote(job_id, safe='')}/run")

    async def prune_group(
        self,
        store: str,
        backup_type: str,
        backup_id: str,
        *,
        namespace: str = "",
        dry_run: bool = True,
        keep: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        """Prune one backup group.

        Returns one entry per snapshot with a ``keep`` flag, so a dry run shows
        exactly what a real run would delete.
        """
        payload: dict[str, Any] = {
            "backup-type": backup_type,
            "backup-id": backup_id,
            "ns": namespace or None,
            "dry-run": dry_run,
        }
        for window, count in (keep or {}).items():
            payload[f"keep-{window}"] = count
        result = await self._post(f"{self._store_path(store)}/prune", payload)
        return result or []

    async def forget_snapshot(
        self,
        store: str,
        backup_type: str,
        backup_id: str,
        backup_time: int,
        *,
        namespace: str = "",
    ) -> None:
        """Delete a single snapshot. Irreversible."""
        await self._request(
            "DELETE",
            f"{self._store_path(store)}/snapshots",
            params={
                "backup-type": backup_type,
                "backup-id": backup_id,
                "backup-time": backup_time,
                "ns": namespace or None,
            },
            retry=False,
        )

    async def forget_group(
        self,
        store: str,
        backup_type: str,
        backup_id: str,
        *,
        namespace: str = "",
    ) -> None:
        """Delete a whole backup group with all of its snapshots. Irreversible."""
        await self._request(
            "DELETE",
            f"{self._store_path(store)}/groups",
            params={
                "backup-type": backup_type,
                "backup-id": backup_id,
                "ns": namespace or None,
            },
            retry=False,
        )

    async def set_protected(
        self,
        store: str,
        backup_type: str,
        backup_id: str,
        backup_time: int,
        protected: bool,
        *,
        namespace: str = "",
    ) -> None:
        """Protect or unprotect one snapshot against pruning."""
        await self._put(
            f"{self._store_path(store)}/protected",
            {
                "backup-type": backup_type,
                "backup-id": backup_id,
                "backup-time": backup_time,
                "protected": protected,
                "ns": namespace or None,
            },
        )

    async def set_maintenance(self, store: str, mode: str | None) -> None:
        """Set or clear the maintenance mode of a datastore."""
        if mode:
            await self._put(
                f"/config/datastore/{quote(store, safe='')}", {"maintenance-mode": mode}
            )
        else:
            await self._put(
                f"/config/datastore/{quote(store, safe='')}",
                {"delete": ["maintenance-mode"]},
            )

    async def get_jobs(self) -> list[JobStatus]:
        """Return configured prune, verify and sync jobs with their last result."""
        jobs: list[JobStatus] = []
        for kind in JOB_KINDS:
            try:
                raw = await self._get(f"/admin/{kind}") or []
            except (PbsNotFoundError, PbsPermissionError) as err:
                _LOGGER.debug("Skipping %s jobs: %s", kind, err)
                continue
            jobs.extend(JobStatus.from_api(entry, kind) for entry in raw)
        return jobs


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Prepare query parameters: drop None, send bools as 1 and 0.

    A query string carries no types, so a bool has to become a number that PBS
    parses back into one.
    """
    if not params:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        cleaned[key] = int(value) if isinstance(value, bool) else value
    return cleaned or None


def _clean_body(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Prepare a JSON body: drop None, leave every other type alone.

    Unlike a query string, JSON has real types. PBS rejects a numeric 1 for a
    boolean parameter with "Expected boolean value", so bools must stay bools.
    """
    if not data:
        return None
    cleaned = {key: value for key, value in data.items() if value is not None}
    return cleaned or None
