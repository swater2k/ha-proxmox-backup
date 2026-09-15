"""Diagnostics for the Proxmox Backup Server integration."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN_ID, CONF_TOKEN_SECRET
from .coordinator import PbsConfigEntry

TO_REDACT = {
    CONF_TOKEN_SECRET,
    CONF_TOKEN_ID,
    "fingerprint",
    "serial",
    "subject",
    "issuer",
    "owner",
    "user",
}


def _dump(value: Any) -> Any:
    """Turn coordinator payloads into plain JSON friendly structures."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _dump(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_dump(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PbsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "capabilities": {
            "missing": sorted(runtime.capabilities.missing),
        },
        "coordinators": {
            "fast": {
                "last_update_success": runtime.fast.last_update_success,
                "data": async_redact_data(_dump(runtime.fast.data), TO_REDACT),
            },
            "medium": {
                "last_update_success": runtime.medium.last_update_success,
                "snapshots_throttled": runtime.medium.snapshots_throttled,
                "data": async_redact_data(_dump(runtime.medium.data), TO_REDACT),
            },
            "slow": {
                "last_update_success": runtime.slow.last_update_success,
                "data": async_redact_data(_dump(runtime.slow.data), TO_REDACT),
            },
        },
    }
