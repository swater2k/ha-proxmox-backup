"""Binary sensor platform for Proxmox Backup Server."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import health
from .const import CAP_DISKS, CAP_SERVICES, CONF_STALE_DAYS, DEFAULT_STALE_DAYS
from .coordinator import GroupStats, PbsConfigEntry
from .entity import PbsDatastoreEntity, PbsGroupEntity, PbsInstanceEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PbsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all binary sensors of one config entry."""
    runtime = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        PbsConnectivitySensor(entry, runtime.fast),
        PbsServiceProblemSensor(entry, runtime.slow),
        PbsDiskProblemSensor(entry, runtime.slow),
        PbsOverallProblemSensor(entry, runtime.medium),
        PbsActionRunningSensor(entry, runtime.fast),
    ]

    for store in runtime.medium.stores:
        entities.append(PbsGcRunningSensor(entry, runtime.fast, store))
        entities.append(PbsMaintenanceSensor(entry, runtime.medium, store))
        entities.append(PbsDatastoreProblemSensor(entry, runtime.fast, store))

    async_add_entities(entities)

    known: set[tuple[str, str]] = set()

    @callback
    def _async_add_dynamic() -> None:
        """Add a staleness sensor for every backup group PBS reports."""
        medium = runtime.medium.data
        if medium is None:
            return
        new: list[BinarySensorEntity] = []
        for store, data in medium.datastores.items():
            for stats in data.stats.values():
                ident = (store, stats.key)
                if ident in known:
                    continue
                known.add(ident)
                new.append(PbsBackupStaleSensor(entry, runtime.medium, store, stats))
        if new:
            async_add_entities(new)

    _async_add_dynamic()
    entry.async_on_unload(runtime.medium.async_add_listener(_async_add_dynamic))


class PbsConnectivitySensor(PbsInstanceEntity, BinarySensorEntity):
    """Whether the PBS API answers.

    Deliberately never unavailable: an entity that disappears exactly when the
    server goes down is useless for alerting on the server going down.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connectivity"

    def __init__(self, entry: PbsConfigEntry, coordinator) -> None:
        """Set up the connectivity sensor."""
        super().__init__(entry, coordinator, "connectivity")

    @property
    def available(self) -> bool:
        """Always available, by design."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while the last poll succeeded."""
        return self.coordinator.last_update_success


class PbsServiceProblemSensor(PbsInstanceEntity, BinarySensorEntity):
    """Whether an enabled PBS systemd unit is not running."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "service_problem"

    def __init__(self, entry: PbsConfigEntry, coordinator) -> None:
        """Set up the service problem sensor."""
        super().__init__(entry, coordinator, "service_problem")

    @property
    def available(self) -> bool:
        """Return False when the token may not read the service list."""
        return (
            super().available
            and self.entry.runtime_data.capabilities.has(CAP_SERVICES)
            and bool(self.coordinator.data.services)
        )

    @property
    def is_on(self) -> bool:
        """Return True when at least one enabled unit is down."""
        return bool(self.coordinator.data.failed_services)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """List the affected units."""
        return {
            "failed_services": [
                service.service for service in self.coordinator.data.failed_services
            ]
        }


class PbsDiskProblemSensor(PbsInstanceEntity, BinarySensorEntity):
    """Whether a disk reports a failed SMART verdict."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "disk_problem"

    def __init__(self, entry: PbsConfigEntry, coordinator) -> None:
        """Set up the disk problem sensor."""
        super().__init__(entry, coordinator, "disk_problem")

    @property
    def available(self) -> bool:
        """Return False when the token lacks Audit on /system."""
        return (
            super().available
            and self.entry.runtime_data.capabilities.has(CAP_DISKS)
            and bool(self.coordinator.data.disks)
        )

    @property
    def is_on(self) -> bool:
        """Return True when any disk failed SMART."""
        return bool(self.coordinator.data.unhealthy_disks)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose per disk detail, including wearout for SSDs."""
        return {
            "disks": [
                {
                    "name": disk.name,
                    "model": disk.model,
                    "status": disk.status,
                    "wearout": disk.wearout,
                    "used": disk.used,
                }
                for disk in self.coordinator.data.disks
            ]
        }


class PbsOverallProblemSensor(PbsInstanceEntity, BinarySensorEntity):
    """Whether anything at all is wrong with this PBS.

    The counterpart to the overall status sensor, shaped for automations: it
    turns on for warnings as well as critical findings, and names them in an
    attribute.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "overall_problem"

    def __init__(self, entry: PbsConfigEntry, coordinator) -> None:
        """Set up the overall problem sensor."""
        super().__init__(entry, coordinator, "overall_problem")

    def _findings(self) -> list[health.Finding]:
        """Collect every current finding across all three coordinators."""
        runtime = self.entry.runtime_data
        return health.instance_findings(
            runtime.fast.data,
            runtime.medium.data,
            runtime.slow.data,
            dict(self.entry.options),
        )

    @property
    def is_on(self) -> bool:
        """Return True when at least one finding applies."""
        return bool(self._findings())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the findings and the worst severity among them."""
        findings = self._findings()
        return {
            "status": health.worst(findings),
            "findings": [finding.message for finding in findings],
        }


class PbsGcRunningSensor(PbsDatastoreEntity, BinarySensorEntity):
    """Whether a garbage collection run is in progress.

    Bound to the fast coordinator and answered from the running task list. The
    GC status endpoint keeps a UPID of the last finished run, which would make
    this sensor permanently on.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "gc_running"

    def __init__(self, entry: PbsConfigEntry, coordinator, store: str) -> None:
        """Set up the GC running sensor."""
        super().__init__(entry, coordinator, store, "gc_running")

    @property
    def is_on(self) -> bool:
        """Return True while GC runs on this datastore."""
        return self.coordinator.data.task_running("garbage_collection", self.store)


class PbsMaintenanceSensor(PbsDatastoreEntity, BinarySensorEntity):
    """Whether the datastore is in maintenance mode."""

    _attr_translation_key = "maintenance"

    def __init__(self, entry: PbsConfigEntry, coordinator, store: str) -> None:
        """Set up the maintenance sensor."""
        super().__init__(entry, coordinator, store, "maintenance")

    @property
    def is_on(self) -> bool:
        """Return True when any maintenance mode is configured."""
        data = self.coordinator.data.datastores.get(self.store)
        return bool(data and data.config and data.config.in_maintenance)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose which maintenance mode is set."""
        data = self.coordinator.data.datastores.get(self.store)
        mode = data.config.maintenance_mode if data and data.config else None
        return {"mode": mode}


class PbsDatastoreProblemSensor(PbsDatastoreEntity, BinarySensorEntity):
    """Aggregated problem state of one datastore.

    Bound to the fast coordinator because the usage thresholds should react
    quickly; the slower garbage collection and verification facts are read from
    the medium coordinator on the side.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "datastore_problem"

    def __init__(self, entry: PbsConfigEntry, coordinator, store: str) -> None:
        """Set up the datastore problem sensor."""
        super().__init__(entry, coordinator, store, "problem")

    def _findings(self) -> list[health.Finding]:
        """Collect the findings that concern this datastore."""
        return health.datastore_findings(
            self.store,
            self.coordinator.data,
            self.entry.runtime_data.medium.data,
            dict(self.entry.options),
        )

    @property
    def is_on(self) -> bool:
        """Return True when at least one finding applies."""
        return bool(self._findings())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the reasons so notifications can quote them."""
        findings = self._findings()
        return {
            "status": health.worst(findings),
            "reasons": [finding.message for finding in findings],
        }


class PbsBackupStaleSensor(PbsGroupEntity, BinarySensorEntity):
    """Whether a backup group's newest backup is older than the threshold."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "backup_stale"

    def __init__(
        self, entry: PbsConfigEntry, coordinator, store: str, stats: GroupStats
    ) -> None:
        """Set up the staleness sensor for one group."""
        super().__init__(entry, coordinator, store, stats, "backup_stale")

    @property
    def is_on(self) -> bool:
        """Return True when the newest backup is too old, or missing."""
        stats = self.stats
        if stats is None:
            return False
        age = stats.age_days
        if age is None:
            return True
        return age > self.entry.options.get(CONF_STALE_DAYS, DEFAULT_STALE_DAYS)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the age and the threshold it was compared against."""
        stats = self.stats
        return {
            "age_days": stats.age_days if stats else None,
            "threshold_days": self.entry.options.get(
                CONF_STALE_DAYS, DEFAULT_STALE_DAYS
            ),
        }


class PbsActionRunningSensor(PbsInstanceEntity, BinarySensorEntity):
    """Whether an action triggered from Home Assistant is still running."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "action_running"

    def __init__(self, entry: PbsConfigEntry, coordinator) -> None:
        """Set up the action running sensor."""
        super().__init__(entry, coordinator, "action_running")

    async def async_added_to_hass(self) -> None:
        """Listen to the task tracker on top of the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.entry.runtime_data.tasks.async_add_listener(self.async_write_ha_state)
        )

    @property
    def is_on(self) -> bool:
        """Return True while a triggered task is in progress."""
        return self.entry.runtime_data.tasks.running
