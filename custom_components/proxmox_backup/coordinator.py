"""Data update coordinators for the Proxmox Backup Server integration.

Three coordinators instead of one, because the data has very different rates of
change: host load moves every minute, backup groups every few hours, and the
certificate expiry once a year. Polling everything at the fastest rate would be
wasteful and is explicitly discouraged by the integration quality scale.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PbsAuthError, PbsClient, PbsError, PbsNotFoundError, PbsPermissionError
from .api.models import (
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
)
from .const import (
    CAP_ACTIVE_OPERATIONS,
    CAP_CERTIFICATES,
    CAP_DATASTORE_CONFIG,
    CAP_DISKS,
    CAP_JOBS,
    CAP_NAMESPACES,
    CAP_NODE_STATUS,
    CAP_SERVICES,
    CAP_SUBSCRIPTION,
    CAP_TASKS,
    CAP_UPDATES,
    CONF_DATASTORES,
    LOGGER,
    SCAN_INTERVAL_FAST,
    SCAN_INTERVAL_MEDIUM,
    SCAN_INTERVAL_SLOW,
    SNAPSHOT_THROTTLE_EVERY,
    SNAPSHOT_THROTTLE_LIMIT,
    TASK_HISTORY_LIMIT,
)


class Capabilities:
    """Remembers which endpoints the token is not allowed to read.

    PBS intersects user and token ACLs, so a partially privileged token is
    common. Losing SMART data must not take the whole integration down, it must
    only hide the affected entities.
    """

    def __init__(self) -> None:
        """Start out assuming everything works."""
        self.missing: set[str] = set()

    def mark_missing(self, capability: str, error: Exception) -> None:
        """Record a denied endpoint and log it exactly once."""
        if capability not in self.missing:
            LOGGER.warning(
                "PBS denied access to '%s' (%s). The matching entities stay "
                "unavailable. Remember that PBS needs the ACL on the user *and* "
                "on the token",
                capability,
                error,
            )
            self.missing.add(capability)

    def mark_available(self, capability: str) -> None:
        """Clear a previously denied endpoint after permissions were fixed."""
        if capability in self.missing:
            LOGGER.info("PBS access to '%s' works again", capability)
            self.missing.discard(capability)

    def has(self, capability: str) -> bool:
        """Return True when the endpoint is readable."""
        return capability not in self.missing


# --------------------------------------------------------------------------
# coordinator payloads
# --------------------------------------------------------------------------


@dataclass(slots=True)
class GroupStats:
    """Per group aggregation, computed from the snapshot list.

    Built here rather than per entity so the expensive snapshot call happens
    once for the whole datastore instead of once per guest.
    """

    key: str
    backup_type: str
    backup_id: str
    namespace: str
    snapshot_count: int = 0
    total_size: int | None = None
    verify_ok: int = 0
    verify_failed: int = 0
    verify_none: int = 0
    protected: int = 0
    oldest: datetime | None = None
    newest: datetime | None = None
    label: str | None = None

    @property
    def verify_state(self) -> str:
        """Return the worst verification state found in the group."""
        if self.verify_failed:
            return "failed"
        if self.verify_ok and not self.verify_none:
            return "ok"
        if self.verify_ok:
            return "partial"
        return "none"


@dataclass(slots=True)
class DatastoreData:
    """Everything known about one datastore at the medium interval."""

    store: str
    config: DatastoreConfig | None = None
    gc: GcStatus | None = None
    groups: dict[str, BackupGroup] = field(default_factory=dict)
    stats: dict[str, GroupStats] = field(default_factory=dict)
    namespaces: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    snapshot_details_available: bool = False

    @property
    def group_count(self) -> int:
        """Return the number of backup groups."""
        return len(self.groups) or self.counts.get("groups", 0)

    @property
    def snapshot_count(self) -> int:
        """Return the number of snapshots."""
        if self.snapshot_details_available:
            return sum(stat.snapshot_count for stat in self.stats.values())
        return self.counts.get("snapshots", 0)

    @property
    def verify_ok(self) -> int:
        """Return the number of successfully verified snapshots."""
        return sum(stat.verify_ok for stat in self.stats.values())

    @property
    def verify_failed(self) -> int:
        """Return the number of snapshots that failed verification."""
        return sum(stat.verify_failed for stat in self.stats.values())

    @property
    def verify_none(self) -> int:
        """Return the number of snapshots that were never verified."""
        return sum(stat.verify_none for stat in self.stats.values())

    @property
    def oldest_snapshot(self) -> datetime | None:
        """Return the oldest snapshot timestamp across all groups."""
        stamps = [stat.oldest for stat in self.stats.values() if stat.oldest]
        return min(stamps) if stamps else None

    @property
    def newest_snapshot(self) -> datetime | None:
        """Return the newest snapshot timestamp across all groups."""
        stamps = [stat.newest for stat in self.stats.values() if stat.newest]
        return max(stamps) if stamps else None


@dataclass(slots=True)
class FastData:
    """Payload of the fast coordinator."""

    node: NodeStatus | None = None
    usage: dict[str, DatastoreUsage] = field(default_factory=dict)
    active: dict[str, ActiveOperations] = field(default_factory=dict)
    running_tasks: list[TaskInfo] = field(default_factory=list)


@dataclass(slots=True)
class MediumData:
    """Payload of the medium coordinator."""

    datastores: dict[str, DatastoreData] = field(default_factory=dict)
    jobs: list[JobStatus] = field(default_factory=list)
    failed_tasks: list[TaskInfo] = field(default_factory=list)

    def failed_within(self, days: int) -> int:
        """Return how many tasks failed within the last ``days`` days."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        return sum(
            1
            for task in self.failed_tasks
            if task.start_time is not None and task.start_time >= cutoff
        )

    @property
    def last_failed_task(self) -> TaskInfo | None:
        """Return the most recent failed task."""
        dated = [task for task in self.failed_tasks if task.start_time]
        if not dated:
            return None
        return max(dated, key=lambda task: task.start_time)


@dataclass(slots=True)
class SlowData:
    """Payload of the slow coordinator."""

    version: VersionInfo | None = None
    services: list[ServiceInfo] = field(default_factory=list)
    subscription: SubscriptionInfo | None = None
    updates: list[UpdateInfo] = field(default_factory=list)
    certificates: list[CertificateInfo] = field(default_factory=list)
    disks: list[DiskInfo] = field(default_factory=list)

    @property
    def expiring_certificate(self) -> CertificateInfo | None:
        """Return the certificate that expires first."""
        dated = [cert for cert in self.certificates if cert.not_after]
        if not dated:
            return None
        return min(dated, key=lambda cert: cert.not_after)

    @property
    def failed_services(self) -> list[ServiceInfo]:
        """Return enabled units that are not running."""
        return [
            service
            for service in self.services
            if service.is_enabled and not service.is_running
        ]

    @property
    def unhealthy_disks(self) -> list[DiskInfo]:
        """Return disks whose SMART verdict is a failure."""
        return [disk for disk in self.disks if disk.is_healthy is False]


# --------------------------------------------------------------------------
# coordinators
# --------------------------------------------------------------------------


class PbsBaseCoordinator[DataT](DataUpdateCoordinator[DataT]):
    """Shared error translation and permission handling."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PbsClient,
        capabilities: Capabilities,
        *,
        name: str,
        interval: timedelta,
    ) -> None:
        """Set up the coordinator with its own polling interval."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{entry.title} ({name})",
            update_interval=interval,
        )
        self.client = client
        self.capabilities = capabilities
        self.entry = entry

    @property
    def stores(self) -> list[str]:
        """Return the datastores the user selected for monitoring."""
        return list(self.entry.options.get(CONF_DATASTORES) or [])

    async def _async_update_data(self) -> DataT:
        """Fetch data and translate client errors into coordinator errors."""
        try:
            return await self._async_fetch()
        except PbsAuthError as err:
            raise ConfigEntryAuthFailed(
                "The API token was rejected by Proxmox Backup Server"
            ) from err
        except PbsError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_fetch(self) -> DataT:
        """Fetch the actual payload. Implemented by the subclasses."""
        raise NotImplementedError

    async def _guard[T](
        self, capability: str, awaitable: Awaitable[T], default: T
    ) -> T:
        """Run a call that may be denied, returning ``default`` if it is."""
        try:
            result = await awaitable
        except (PbsPermissionError, PbsNotFoundError) as err:
            self.capabilities.mark_missing(capability, err)
            return default
        self.capabilities.mark_available(capability)
        return result


class PbsFastCoordinator(PbsBaseCoordinator[FastData]):
    """Host load, datastore usage and currently running tasks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PbsClient,
        capabilities: Capabilities,
    ) -> None:
        """Set up the one minute coordinator."""
        super().__init__(
            hass, entry, client, capabilities, name="fast", interval=SCAN_INTERVAL_FAST
        )

    async def _async_fetch(self) -> FastData:
        """Fetch node status, usage, active operations and running tasks."""
        stores = self.stores
        node, usage, tasks, *operations = await asyncio.gather(
            self._guard(CAP_NODE_STATUS, self.client.get_node_status(), None),
            self.client.get_datastore_usage(),
            self._guard(CAP_TASKS, self.client.get_tasks(limit=50, running=True), []),
            *[
                self._guard(
                    CAP_ACTIVE_OPERATIONS,
                    self.client.get_active_operations(store),
                    None,
                )
                for store in stores
            ],
        )
        active = {
            store: operation
            for store, operation in zip(stores, operations, strict=False)
            if operation is not None
        }
        return FastData(
            node=node,
            usage={store: usage[store] for store in stores if store in usage},
            active=active,
            running_tasks=[task for task in tasks if task.is_running],
        )


class PbsMediumCoordinator(PbsBaseCoordinator[MediumData]):
    """Backup groups, snapshots, garbage collection, jobs and failed tasks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PbsClient,
        capabilities: Capabilities,
    ) -> None:
        """Set up the five minute coordinator."""
        super().__init__(
            hass,
            entry,
            client,
            capabilities,
            name="medium",
            interval=SCAN_INTERVAL_MEDIUM,
        )
        self._cycle = 0
        self._snapshots_throttled = False

    @property
    def snapshots_throttled(self) -> bool:
        """Return True when snapshot details are only refreshed hourly."""
        return self._snapshots_throttled

    async def _async_fetch(self) -> MediumData:
        """Fetch everything that changes on the scale of hours."""
        self._cycle += 1
        fetch_snapshots = (
            not self._snapshots_throttled or self._cycle % SNAPSHOT_THROTTLE_EVERY == 1
        )

        since = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
        configs, jobs, failed = await asyncio.gather(
            self._guard(CAP_DATASTORE_CONFIG, self.client.get_datastore_config(), {}),
            self._guard(CAP_JOBS, self.client.get_jobs(), []),
            self._guard(
                CAP_TASKS,
                self.client.get_tasks(
                    limit=TASK_HISTORY_LIMIT, errors=True, since=since
                ),
                [],
            ),
        )

        datastores = {
            store: await self._async_fetch_store(store, configs, fetch_snapshots)
            for store in self.stores
        }

        total_snapshots = sum(data.snapshot_count for data in datastores.values())
        if total_snapshots > SNAPSHOT_THROTTLE_LIMIT and not self._snapshots_throttled:
            self._snapshots_throttled = True
            LOGGER.info(
                "%s snapshots found, throttling the snapshot detail poll to "
                "roughly hourly",
                total_snapshots,
            )

        return MediumData(
            datastores=datastores,
            jobs=jobs,
            failed_tasks=[task for task in failed if task.is_failed],
        )

    async def _async_fetch_store(
        self,
        store: str,
        configs: dict[str, DatastoreConfig],
        fetch_snapshots: bool,
    ) -> DatastoreData:
        """Fetch one datastore including all of its namespaces."""
        namespaces, counts, gc = await asyncio.gather(
            self._guard(CAP_NAMESPACES, self.client.get_namespaces(store), [""]),
            self.client.get_datastore_counts(store),
            self.client.get_gc_status(store),
        )

        groups: dict[str, BackupGroup] = {}
        snapshots: list[Snapshot] = []
        for namespace in namespaces or [""]:
            for group in await self.client.get_groups(store, namespace):
                groups[group.key] = group
            if fetch_snapshots:
                snapshots.extend(await self.client.get_snapshots(store, namespace))

        previous = (self.data.datastores.get(store) if self.data else None) or None
        if fetch_snapshots:
            stats = _aggregate(groups, snapshots)
            details_available = True
        else:
            stats = previous.stats if previous else {}
            details_available = bool(stats)

        return DatastoreData(
            store=store,
            config=configs.get(store),
            gc=gc,
            groups=groups,
            stats=stats,
            namespaces=list(namespaces or [""]),
            counts=counts,
            snapshot_details_available=details_available,
        )


class PbsSlowCoordinator(PbsBaseCoordinator[SlowData]):
    """Version, services, certificates, updates and disks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PbsClient,
        capabilities: Capabilities,
    ) -> None:
        """Set up the six hour coordinator."""
        super().__init__(
            hass, entry, client, capabilities, name="slow", interval=SCAN_INTERVAL_SLOW
        )

    async def _async_fetch(self) -> SlowData:
        """Fetch the rarely changing parts, all of them optional."""
        (
            version,
            services,
            subscription,
            updates,
            certificates,
            disks,
        ) = await asyncio.gather(
            self.client.get_version(),
            self._guard(CAP_SERVICES, self.client.get_services(), []),
            self._guard(CAP_SUBSCRIPTION, self.client.get_subscription(), None),
            self._guard(CAP_UPDATES, self.client.get_updates(), []),
            self._guard(CAP_CERTIFICATES, self.client.get_certificates(), []),
            self._guard(CAP_DISKS, self.client.get_disks(), []),
        )
        return SlowData(
            version=version,
            services=services,
            subscription=subscription,
            updates=updates,
            certificates=certificates,
            disks=disks,
        )


@dataclass(slots=True)
class PbsRuntimeData:
    """Everything the platforms need, stored on the config entry."""

    client: PbsClient
    capabilities: Capabilities
    fast: PbsFastCoordinator
    medium: PbsMediumCoordinator
    slow: PbsSlowCoordinator
    root_id: str

    def coordinator(self, source: str) -> PbsBaseCoordinator[Any]:
        """Return a coordinator by the name used in entity descriptions."""
        return {"fast": self.fast, "medium": self.medium, "slow": self.slow}[source]


type PbsConfigEntry = ConfigEntry[PbsRuntimeData]


def _aggregate(
    groups: dict[str, BackupGroup], snapshots: list[Snapshot]
) -> dict[str, GroupStats]:
    """Fold the snapshot list into one record per backup group."""
    stats: dict[str, GroupStats] = {
        key: GroupStats(
            key=key,
            backup_type=group.backup_type,
            backup_id=group.backup_id,
            namespace=group.namespace,
            label=group.comment,
        )
        for key, group in groups.items()
    }

    for snapshot in snapshots:
        key = snapshot.group_key
        stat = stats.get(key)
        if stat is None:
            # A group that appeared between the two calls. Track it anyway.
            stat = stats[key] = GroupStats(
                key=key,
                backup_type=snapshot.backup_type,
                backup_id=snapshot.backup_id,
                namespace=snapshot.namespace,
            )

        stat.snapshot_count += 1
        if snapshot.size is not None:
            stat.total_size = (stat.total_size or 0) + snapshot.size
        if snapshot.protected:
            stat.protected += 1

        state = (snapshot.verify_state or "").lower()
        if state == "ok":
            stat.verify_ok += 1
        elif state:
            stat.verify_failed += 1
        else:
            stat.verify_none += 1

        stamp = snapshot.backup_time
        if stamp:
            if stat.oldest is None or stamp < stat.oldest:
                stat.oldest = stamp
            if stat.newest is None or stamp > stat.newest:
                stat.newest = stamp
                # PVE writes the guest name into the snapshot comment via its
                # notes-template, which makes a far better label than the VMID.
                if snapshot.comment:
                    stat.label = snapshot.comment

    return stats
