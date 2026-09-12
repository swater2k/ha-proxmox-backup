"""Binary sensor platform for Proxmox Backup Server."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CAP_DISKS,
    CAP_SERVICES,
    CONF_GC_WARNING_DAYS,
    CONF_USAGE_CRITICAL,
    DEFAULT_GC_WARNING_DAYS,
    DEFAULT_USAGE_CRITICAL,
)
from .coordinator import DatastoreData, PbsConfigEntry
from .entity import PbsDatastoreEntity, PbsInstanceEntity

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
    ]

    for store in runtime.medium.stores:
        entities.append(PbsGcRunningSensor(entry, runtime.medium, store))
        entities.append(PbsMaintenanceSensor(entry, runtime.medium, store))
        entities.append(PbsDatastoreProblemSensor(entry, runtime.fast, store))

    async_add_entities(entities)


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


class PbsGcRunningSensor(PbsDatastoreEntity, BinarySensorEntity):
    """Whether a garbage collection run is in progress."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "gc_running"

    def __init__(self, entry: PbsConfigEntry, coordinator, store: str) -> None:
        """Set up the GC running sensor."""
        super().__init__(entry, coordinator, store, "gc_running")

    @property
    def is_on(self) -> bool:
        """Return True while GC runs."""
        data = self.coordinator.data.datastores.get(self.store)
        return bool(data and data.gc and data.gc.is_running)


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
    quickly; the slower GC and verify facts are read from the medium
    coordinator on the side. The list of reasons is exposed as an attribute so a
    notification can say what is actually wrong.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "datastore_problem"

    def __init__(self, entry: PbsConfigEntry, coordinator, store: str) -> None:
        """Set up the datastore problem sensor."""
        super().__init__(entry, coordinator, store, "problem")

    def _reasons(self) -> list[str]:
        """Collect every reason this datastore is considered unhealthy."""
        options = self.entry.options
        reasons: list[str] = []

        usage = self.coordinator.data.usage.get(self.store)
        critical = options.get(CONF_USAGE_CRITICAL, DEFAULT_USAGE_CRITICAL)
        if usage:
            if usage.error:
                reasons.append(f"datastore error: {usage.error}")
            if usage.used_percent is not None and usage.used_percent >= critical:
                reasons.append(f"usage {usage.used_percent:.1f}% >= {critical}%")

        medium = self.entry.runtime_data.medium.data
        data: DatastoreData | None = (
            medium.datastores.get(self.store) if medium else None
        )
        if data is None:
            return reasons

        gc_days = options.get(CONF_GC_WARNING_DAYS, DEFAULT_GC_WARNING_DAYS)
        if data.gc:
            if data.gc.state == "error":
                reasons.append("last garbage collection failed")
            if data.gc.still_bad:
                reasons.append(f"{data.gc.still_bad} corrupt chunks left behind")
            last_run = data.gc.last_run_endtime
            if last_run is not None:
                age = (dt_util.utcnow() - last_run).days
                if age > gc_days:
                    reasons.append(f"no garbage collection for {age} days")

        if data.verify_failed:
            reasons.append(f"{data.verify_failed} snapshots failed verification")

        return reasons

    @property
    def is_on(self) -> bool:
        """Return True when at least one reason applies."""
        return bool(self._reasons())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the reasons so notifications can quote them."""
        return {"reasons": self._reasons()}
