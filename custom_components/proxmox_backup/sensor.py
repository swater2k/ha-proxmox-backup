"""Sensor platform for Proxmox Backup Server.

Entities are declared as descriptions with a ``source`` naming the coordinator
they belong to, so each one only wakes up when its own data actually changed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import FastData, MediumData, PbsConfigEntry, SlowData
from .entity import PbsDatastoreEntity, PbsInstanceEntity

PARALLEL_UPDATES = 0

SUBSCRIPTION_STATES = [
    "active",
    "expired",
    "invalid",
    "notfound",
    "suspended",
    "unknown",
]
GC_STATES = ["ok", "error", "running", "unknown"]

# Boot time is derived from an uptime counter, so it jitters by a second on
# every poll. Only publish a new value when the drift is larger than this.
BOOT_TIME_TOLERANCE = timedelta(seconds=90)


@dataclass(frozen=True, kw_only=True)
class PbsSensorDescription(SensorEntityDescription):
    """Describes an instance level sensor."""

    source: str
    value_fn: Callable[[Any], StateType | datetime]
    attributes_fn: Callable[[Any], dict[str, Any]] | None = None
    available_fn: Callable[[Any], bool] = lambda _data: True


@dataclass(frozen=True, kw_only=True)
class PbsDatastoreSensorDescription(SensorEntityDescription):
    """Describes a datastore level sensor."""

    source: str
    value_fn: Callable[[Any, str], StateType | datetime]
    attributes_fn: Callable[[Any, str], dict[str, Any]] | None = None
    available_fn: Callable[[Any, str], bool] = lambda _data, _store: True


def _node(data: FastData) -> bool:
    """Return True when the host status could be read."""
    return data.node is not None


def _usage(data: FastData, store: str) -> bool:
    """Return True when usage for this datastore is present."""
    return store in data.usage


def _store(data: MediumData, store: str) -> bool:
    """Return True when medium data for this datastore is present."""
    return store in data.datastores


def _gc(data: MediumData, store: str) -> Any:
    """Return the GC status of a datastore, or None."""
    entry = data.datastores.get(store)
    return entry.gc if entry else None


def _config(data: MediumData, store: str) -> Any:
    """Return the configuration of a datastore, or None."""
    entry = data.datastores.get(store)
    return entry.config if entry else None


def _subscription_state(data: SlowData) -> str:
    """Map the PBS subscription status onto the declared enum options."""
    status = (data.subscription.status if data.subscription else None) or "unknown"
    status = status.lower()
    return status if status in SUBSCRIPTION_STATES else "unknown"


INSTANCE_SENSORS: tuple[PbsSensorDescription, ...] = (
    PbsSensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        source="fast",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        available_fn=_node,
        value_fn=lambda data: data.node.cpu_percent,
    ),
    PbsSensorDescription(
        key="io_wait",
        translation_key="io_wait",
        source="fast",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        available_fn=_node,
        value_fn=lambda data: data.node.wait_percent,
    ),
    PbsSensorDescription(
        key="load_1m",
        translation_key="load_1m",
        source="fast",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        available_fn=_node,
        value_fn=lambda data: data.node.load_at(0),
    ),
    PbsSensorDescription(
        key="load_5m",
        translation_key="load_5m",
        source="fast",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        available_fn=_node,
        value_fn=lambda data: data.node.load_at(1),
    ),
    PbsSensorDescription(
        key="load_15m",
        translation_key="load_15m",
        source="fast",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        available_fn=_node,
        value_fn=lambda data: data.node.load_at(2),
    ),
    PbsSensorDescription(
        key="memory_usage",
        translation_key="memory_usage",
        source="fast",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        available_fn=_node,
        value_fn=lambda data: data.node.memory_percent,
    ),
    PbsSensorDescription(
        key="memory_used",
        translation_key="memory_used",
        source="fast",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_node,
        value_fn=lambda data: data.node.memory_used,
    ),
    PbsSensorDescription(
        key="memory_total",
        translation_key="memory_total",
        source="fast",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        available_fn=_node,
        value_fn=lambda data: data.node.memory_total,
    ),
    PbsSensorDescription(
        key="swap_usage",
        translation_key="swap_usage",
        source="fast",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        available_fn=_node,
        value_fn=lambda data: data.node.swap_percent,
    ),
    PbsSensorDescription(
        key="root_usage",
        translation_key="root_usage",
        source="fast",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        available_fn=_node,
        value_fn=lambda data: data.node.root_percent,
    ),
    PbsSensorDescription(
        key="root_free",
        translation_key="root_free",
        source="fast",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_node,
        value_fn=lambda data: data.node.root_available,
    ),
    PbsSensorDescription(
        key="running_tasks",
        translation_key="running_tasks",
        source="fast",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.running_tasks),
        attributes_fn=lambda data: {
            "tasks": [task.description for task in data.running_tasks[:20]]
        },
    ),
    PbsSensorDescription(
        key="failed_tasks_24h",
        translation_key="failed_tasks_24h",
        source="medium",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.failed_within(1),
    ),
    PbsSensorDescription(
        key="failed_tasks_7d",
        translation_key="failed_tasks_7d",
        source="medium",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.failed_within(7),
    ),
    PbsSensorDescription(
        key="last_failed_task",
        translation_key="last_failed_task",
        source="medium",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: (
            data.last_failed_task.start_time if data.last_failed_task else None
        ),
        attributes_fn=lambda data: (
            {
                "task": data.last_failed_task.description,
                "status": data.last_failed_task.status,
                "user": data.last_failed_task.user,
            }
            if data.last_failed_task
            else {}
        ),
    ),
    PbsSensorDescription(
        key="version",
        translation_key="version",
        source="slow",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.version.full if data.version else None,
    ),
    PbsSensorDescription(
        key="updates_available",
        translation_key="updates_available",
        source="slow",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: len(data.updates),
        attributes_fn=lambda data: {
            "packages": [update.package for update in data.updates[:50]]
        },
    ),
    PbsSensorDescription(
        key="certificate_expiry",
        translation_key="certificate_expiry",
        source="slow",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=lambda data: data.expiring_certificate is not None,
        value_fn=lambda data: (
            data.expiring_certificate.not_after if data.expiring_certificate else None
        ),
    ),
    PbsSensorDescription(
        key="subscription_status",
        translation_key="subscription_status",
        source="slow",
        device_class=SensorDeviceClass.ENUM,
        options=SUBSCRIPTION_STATES,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=lambda data: data.subscription is not None,
        value_fn=_subscription_state,
    ),
)


DATASTORE_SENSORS: tuple[PbsDatastoreSensorDescription, ...] = (
    PbsDatastoreSensorDescription(
        key="used_space",
        translation_key="used_space",
        source="fast",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_usage,
        value_fn=lambda data, store: data.usage[store].used,
    ),
    PbsDatastoreSensorDescription(
        key="free_space",
        translation_key="free_space",
        source="fast",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_usage,
        value_fn=lambda data, store: data.usage[store].available,
    ),
    PbsDatastoreSensorDescription(
        key="total_space",
        translation_key="total_space",
        source="fast",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=_usage,
        value_fn=lambda data, store: data.usage[store].total,
    ),
    PbsDatastoreSensorDescription(
        key="usage",
        translation_key="usage",
        source="fast",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        available_fn=_usage,
        value_fn=lambda data, store: data.usage[store].used_percent,
    ),
    PbsDatastoreSensorDescription(
        key="estimated_full",
        translation_key="estimated_full",
        source="fast",
        device_class=SensorDeviceClass.TIMESTAMP,
        available_fn=_usage,
        value_fn=lambda data, store: data.usage[store].estimated_full,
    ),
    PbsDatastoreSensorDescription(
        key="active_reads",
        translation_key="active_reads",
        source="fast",
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=lambda data, store: store in data.active,
        value_fn=lambda data, store: data.active[store].read,
    ),
    PbsDatastoreSensorDescription(
        key="active_writes",
        translation_key="active_writes",
        source="fast",
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=lambda data, store: store in data.active,
        value_fn=lambda data, store: data.active[store].write,
    ),
    PbsDatastoreSensorDescription(
        key="group_count",
        translation_key="group_count",
        source="medium",
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_store,
        value_fn=lambda data, store: data.datastores[store].group_count,
    ),
    PbsDatastoreSensorDescription(
        key="snapshot_count",
        translation_key="snapshot_count",
        source="medium",
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_store,
        value_fn=lambda data, store: data.datastores[store].snapshot_count,
    ),
    PbsDatastoreSensorDescription(
        key="verified_snapshots",
        translation_key="verified_snapshots",
        source="medium",
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_store,
        value_fn=lambda data, store: data.datastores[store].verify_ok,
    ),
    PbsDatastoreSensorDescription(
        key="failed_verifications",
        translation_key="failed_verifications",
        source="medium",
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_store,
        value_fn=lambda data, store: data.datastores[store].verify_failed,
    ),
    PbsDatastoreSensorDescription(
        key="unverified_snapshots",
        translation_key="unverified_snapshots",
        source="medium",
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_store,
        value_fn=lambda data, store: data.datastores[store].verify_none,
    ),
    PbsDatastoreSensorDescription(
        key="oldest_snapshot",
        translation_key="oldest_snapshot",
        source="medium",
        device_class=SensorDeviceClass.TIMESTAMP,
        available_fn=_store,
        value_fn=lambda data, store: data.datastores[store].oldest_snapshot,
    ),
    PbsDatastoreSensorDescription(
        key="newest_snapshot",
        translation_key="newest_snapshot",
        source="medium",
        device_class=SensorDeviceClass.TIMESTAMP,
        available_fn=_store,
        value_fn=lambda data, store: data.datastores[store].newest_snapshot,
    ),
    PbsDatastoreSensorDescription(
        key="deduplication_factor",
        translation_key="deduplication_factor",
        source="medium",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        available_fn=lambda data, store: _gc(data, store) is not None,
        value_fn=lambda data, store: _gc(data, store).deduplication_factor,
    ),
    PbsDatastoreSensorDescription(
        key="gc_last_run",
        translation_key="gc_last_run",
        source="medium",
        device_class=SensorDeviceClass.TIMESTAMP,
        available_fn=lambda data, store: _gc(data, store) is not None,
        value_fn=lambda data, store: _gc(data, store).last_run_endtime,
    ),
    PbsDatastoreSensorDescription(
        key="gc_state",
        translation_key="gc_state",
        source="medium",
        device_class=SensorDeviceClass.ENUM,
        options=GC_STATES,
        available_fn=lambda data, store: _gc(data, store) is not None,
        value_fn=lambda data, store: _gc(data, store).state,
        attributes_fn=lambda data, store: {
            "last_run_state": _gc(data, store).last_run_state,
            "still_bad_chunks": _gc(data, store).still_bad,
        },
    ),
    PbsDatastoreSensorDescription(
        key="gc_duration",
        translation_key="gc_duration",
        source="medium",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=lambda data, store: _gc(data, store) is not None,
        value_fn=lambda data, store: _gc(data, store).duration,
    ),
    PbsDatastoreSensorDescription(
        key="gc_removed",
        translation_key="gc_removed",
        source="medium",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=lambda data, store: _gc(data, store) is not None,
        value_fn=lambda data, store: _gc(data, store).removed_bytes,
    ),
    PbsDatastoreSensorDescription(
        key="gc_pending",
        translation_key="gc_pending",
        source="medium",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        available_fn=lambda data, store: _gc(data, store) is not None,
        value_fn=lambda data, store: _gc(data, store).pending_bytes,
    ),
    PbsDatastoreSensorDescription(
        key="gc_schedule",
        translation_key="gc_schedule",
        source="medium",
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=lambda data, store: _config(data, store) is not None,
        value_fn=lambda data, store: _config(data, store).gc_schedule,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PbsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all sensors of one config entry."""
    runtime = entry.runtime_data
    entities: list[SensorEntity] = []

    for description in INSTANCE_SENSORS:
        coordinator = runtime.coordinator(description.source)
        entities.append(PbsSensor(entry, coordinator, description))

    entities.append(PbsBootTimeSensor(entry, runtime.fast))

    for store in runtime.medium.stores:
        for datastore_description in DATASTORE_SENSORS:
            coordinator = runtime.coordinator(datastore_description.source)
            entities.append(
                PbsDatastoreSensor(entry, coordinator, store, datastore_description)
            )

    async_add_entities(entities)


class PbsSensor(PbsInstanceEntity, SensorEntity):
    """Sensor describing the PBS host."""

    entity_description: PbsSensorDescription

    def __init__(self, entry, coordinator, description: PbsSensorDescription) -> None:
        """Store the description and build the unique id from its key."""
        super().__init__(entry, coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return False when the underlying data could not be read."""
        return super().available and self.entity_description.available_fn(
            self.coordinator.data
        )

    @property
    def native_value(self) -> StateType | datetime:
        """Return the current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional context for dashboards and automations."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)


class PbsDatastoreSensor(PbsDatastoreEntity, SensorEntity):
    """Sensor describing one datastore."""

    entity_description: PbsDatastoreSensorDescription

    def __init__(
        self, entry, coordinator, store: str, description: PbsDatastoreSensorDescription
    ) -> None:
        """Store the description and build the unique id from its key."""
        super().__init__(entry, coordinator, store, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return False when the underlying data could not be read."""
        return super().available and self.entity_description.available_fn(
            self.coordinator.data, self.store
        )

    @property
    def native_value(self) -> StateType | datetime:
        """Return the current value."""
        return self.entity_description.value_fn(self.coordinator.data, self.store)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional context for dashboards and automations."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data, self.store)


class PbsBootTimeSensor(PbsInstanceEntity, SensorEntity):
    """Boot time of the PBS host.

    Derived from the uptime counter, which drifts by a second on every poll.
    Publishing that would create a new state every minute and pollute the
    recorder, so the value only moves when the drift becomes significant.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "boot_time"

    def __init__(self, entry, coordinator) -> None:
        """Set up the boot time sensor."""
        super().__init__(entry, coordinator, "boot_time")
        self._value: datetime | None = None

    @property
    def available(self) -> bool:
        """Return False when the host status could not be read."""
        return super().available and self.coordinator.data.node is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Recalculate the boot time, ignoring sub minute drift."""
        node = self.coordinator.data.node
        if node is None or node.uptime is None:
            super()._handle_coordinator_update()
            return
        candidate = datetime.now(UTC) - timedelta(seconds=node.uptime)
        if self._value is None or abs(candidate - self._value) > BOOT_TIME_TOLERANCE:
            self._value = candidate
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> datetime | None:
        """Return the boot time."""
        if self._value is None:
            node = self.coordinator.data.node
            if node is not None and node.uptime is not None:
                self._value = datetime.now(UTC) - timedelta(seconds=node.uptime)
        return self._value
