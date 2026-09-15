"""Select platform: the maintenance mode of a datastore.

A select rather than a switch because PBS knows more than on and off: read-only
keeps backups readable while blocking writes, which is what you want during a
long verification.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PbsError
from .const import MAINTENANCE_MODES, MAINTENANCE_OFF
from .coordinator import PbsConfigEntry
from .entity import PbsDatastoreEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PbsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the maintenance selects, if the options allow write actions."""
    runtime = entry.runtime_data
    if not runtime.allow_write:
        return
    async_add_entities(
        PbsMaintenanceSelect(entry, runtime.medium, store)
        for store in runtime.medium.stores
    )


class PbsMaintenanceSelect(PbsDatastoreEntity, SelectEntity):
    """Read and set the maintenance mode of one datastore."""

    _attr_options = MAINTENANCE_MODES
    _attr_translation_key = "maintenance_mode"

    def __init__(self, entry: PbsConfigEntry, coordinator, store: str) -> None:
        """Set up the maintenance mode select."""
        super().__init__(entry, coordinator, store, "maintenance_mode")

    @property
    def current_option(self) -> str:
        """Return the configured mode, normalised onto the known options.

        PBS may store a mode together with a message, as in
        ``offline,message=...``; only the mode itself is of interest here.
        """
        data = self.coordinator.data.datastores.get(self.store)
        raw = data.config.maintenance_mode if data and data.config else None
        if not raw:
            return MAINTENANCE_OFF
        mode = raw.split(",", 1)[0].strip().lower()
        return mode if mode in MAINTENANCE_MODES else MAINTENANCE_OFF

    async def async_select_option(self, option: str) -> None:
        """Set or clear the maintenance mode."""
        runtime = self.entry.runtime_data
        mode = None if option == MAINTENANCE_OFF else option
        try:
            await runtime.client.set_maintenance(self.store, mode)
        except PbsError as err:
            raise HomeAssistantError(
                f"Could not set maintenance mode on {self.store}: {err}"
            ) from err
        runtime.tasks.async_record("maintenance", self.store, self.store, "ok")
        await runtime.medium.async_request_refresh()
