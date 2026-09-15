"""Shared entity base classes and device definitions.

Device layout, one level per PBS concept:

    PBS instance  ->  Datastore  ->  Backup group

The instance is the ``via_device`` of every datastore, and each datastore the
``via_device`` of its groups, so Home Assistant draws the hierarchy and removing
the entry cleans all of them up.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL_DATASTORE, MODEL_GROUP, MODEL_SERVER
from .coordinator import GroupStats, PbsBaseCoordinator, PbsConfigEntry, PbsRuntimeData


def instance_device_info(entry: PbsConfigEntry) -> DeviceInfo:
    """Return the device representing the PBS host itself."""
    runtime = entry.runtime_data
    version = runtime.slow.data.version if runtime.slow.data else None
    return DeviceInfo(
        identifiers={(DOMAIN, runtime.root_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=MODEL_SERVER,
        sw_version=version.full if version else None,
        configuration_url=runtime.client.base_url,
    )


def datastore_device_id(runtime: PbsRuntimeData, store: str) -> str:
    """Return the registry identifier of a datastore device."""
    return f"{runtime.root_id}_datastore_{store}"


def datastore_device_info(entry: PbsConfigEntry, store: str) -> DeviceInfo:
    """Return the device representing one datastore."""
    runtime = entry.runtime_data
    return DeviceInfo(
        identifiers={(DOMAIN, datastore_device_id(runtime, store))},
        name=f"Datastore {store}",
        manufacturer=MANUFACTURER,
        model=MODEL_DATASTORE,
        via_device=(DOMAIN, runtime.root_id),
    )


def group_device_id(runtime: PbsRuntimeData, store: str, key: str) -> str:
    """Return the registry identifier of a backup group device."""
    return f"{runtime.root_id}_group_{store}_{key}"


def group_device_info(
    entry: PbsConfigEntry, store: str, stats: GroupStats
) -> DeviceInfo:
    """Return the device representing one backup group."""
    runtime = entry.runtime_data
    return DeviceInfo(
        identifiers={(DOMAIN, group_device_id(runtime, store, stats.key))},
        name=stats.display_name,
        manufacturer=MANUFACTURER,
        model=MODEL_GROUP,
        via_device=(DOMAIN, datastore_device_id(runtime, store)),
    )


class PbsInstanceEntity(CoordinatorEntity[PbsBaseCoordinator[Any]]):
    """Base class for entities that describe the PBS host."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: PbsConfigEntry,
        coordinator: PbsBaseCoordinator[Any],
        key: str,
    ) -> None:
        """Bind the entity to the instance device."""
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.runtime_data.root_id}_{key}"
        self._attr_device_info = instance_device_info(entry)


class PbsDatastoreEntity(CoordinatorEntity[PbsBaseCoordinator[Any]]):
    """Base class for entities that describe one datastore."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: PbsConfigEntry,
        coordinator: PbsBaseCoordinator[Any],
        store: str,
        key: str,
    ) -> None:
        """Bind the entity to the datastore device."""
        super().__init__(coordinator)
        self.entry = entry
        self.store = store
        self._attr_unique_id = f"{entry.runtime_data.root_id}_datastore_{store}_{key}"
        self._attr_device_info = datastore_device_info(entry, store)


class PbsGroupEntity(CoordinatorEntity[PbsBaseCoordinator[Any]]):
    """Base class for entities that describe one backup group.

    The group key carries the namespace, so a guest with the same VMID in two
    namespaces stays two distinct devices.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: PbsConfigEntry,
        coordinator: PbsBaseCoordinator[Any],
        store: str,
        stats: GroupStats,
        key: str,
    ) -> None:
        """Bind the entity to the backup group device."""
        super().__init__(coordinator)
        self.entry = entry
        self.store = store
        self.group_key = stats.key
        self._attr_unique_id = (
            f"{entry.runtime_data.root_id}_group_{store}_{stats.key}_{key}"
        )
        self._attr_device_info = group_device_info(entry, store, stats)

    @property
    def stats(self) -> GroupStats | None:
        """Return the current aggregation for this group, if it still exists."""
        data = self.coordinator.data.datastores.get(self.store)
        return data.stats.get(self.group_key) if data else None

    @property
    def available(self) -> bool:
        """Return False once the group is gone from PBS."""
        return super().available and self.stats is not None
