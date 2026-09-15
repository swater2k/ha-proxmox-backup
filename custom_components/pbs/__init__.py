"""The Proxmox Backup Server integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import PbsClient
from .const import (
    CONF_TOKEN_ID,
    CONF_TOKEN_SECRET,
    DEFAULT_PORT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .coordinator import (
    Capabilities,
    PbsConfigEntry,
    PbsFastCoordinator,
    PbsMediumCoordinator,
    PbsRuntimeData,
    PbsSlowCoordinator,
)
from .services import async_setup_services
from .tasks import PbsTaskTracker

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once, independently of any config entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PbsConfigEntry) -> bool:
    """Set up Proxmox Backup Server from a config entry."""
    session = async_get_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    )
    client = PbsClient(
        session,
        entry.data[CONF_HOST],
        int(entry.data.get(CONF_PORT, DEFAULT_PORT)),
        entry.data[CONF_TOKEN_ID],
        entry.data[CONF_TOKEN_SECRET],
    )
    capabilities = Capabilities()
    tasks = PbsTaskTracker(hass, client, entry.title)

    fast = PbsFastCoordinator(hass, entry, client, capabilities)
    medium = PbsMediumCoordinator(hass, entry, client, capabilities)
    slow = PbsSlowCoordinator(hass, entry, client, capabilities)

    entry.runtime_data = PbsRuntimeData(
        client=client,
        capabilities=capabilities,
        fast=fast,
        medium=medium,
        slow=slow,
        tasks=tasks,
        root_id=entry.unique_id or entry.entry_id,
    )
    entry.async_on_unload(tasks.async_shutdown)

    # The slow coordinator runs first: its version information becomes the
    # sw_version of the device, which is read while the platforms are set up.
    await slow.async_config_entry_first_refresh()
    await fast.async_config_entry_first_refresh()
    await medium.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PbsConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: PbsConfigEntry) -> None:
    """Reload the entry after the options changed.

    Thresholds and the datastore selection decide which entities exist, so a
    full reload is the only way to apply them consistently.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: PbsConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting devices for datastores or backup groups that are gone.

    A guest that is decommissioned leaves its group behind in Home Assistant
    until the user removes it. Deletion is only permitted once the device no
    longer appears in the current data, so a device is never removed while PBS
    still knows about it.
    """
    runtime = entry.runtime_data
    medium = runtime.medium.data
    alive = {runtime.root_id}
    if medium is not None:
        for store, data in medium.datastores.items():
            alive.add(f"{runtime.root_id}_datastore_{store}")
            alive.update(f"{runtime.root_id}_group_{store}_{key}" for key in data.stats)

    return not any(
        identifier in alive
        for domain, identifier in device.identifiers
        if domain == DOMAIN
    )
