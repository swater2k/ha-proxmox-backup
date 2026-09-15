"""Actions for Proxmox Backup Server.

Everything that takes parameters lives here rather than as an entity. Prune and
forget in particular must not be one tap away in a dashboard, so they require
an explicit target, an explicit confirmation, and the destructive option to be
switched on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .api import PbsError
from .const import (
    DOMAIN,
    EVENT_DESTRUCTIVE_ACTION,
    KEEP_WINDOWS,
    SERVICE_FORGET_GROUP,
    SERVICE_FORGET_SNAPSHOT,
    SERVICE_PRUNE,
    SERVICE_RUN_GC,
    SERVICE_SET_PROTECTED,
    SERVICE_VERIFY,
)
from .coordinator import GroupStats, PbsConfigEntry
from .entity import datastore_device_id, group_device_id

ATTR_CONFIRM = "confirm"
ATTR_DRY_RUN = "dry_run"
ATTR_BACKUP_TIME = "backup_time"
ATTR_PROTECTED = "protected"
ATTR_IGNORE_VERIFIED = "ignore_verified"
ATTR_OUTDATED_AFTER = "outdated_after"

# The device arrives as a plain field, not through a target block: Home
# Assistant removed device filters from target selectors, and an unfiltered
# target picker would offer every device in the house.
TARGET_SCHEMA = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string])},
    extra=vol.ALLOW_EXTRA,
)

VERIFY_SCHEMA = TARGET_SCHEMA.extend(
    {
        vol.Optional(ATTR_IGNORE_VERIFIED): cv.boolean,
        vol.Optional(ATTR_OUTDATED_AFTER): vol.All(vol.Coerce(int), vol.Range(min=0)),
    }
)

PRUNE_SCHEMA = TARGET_SCHEMA.extend(
    {
        vol.Optional(ATTR_DRY_RUN, default=True): cv.boolean,
        **{
            vol.Optional(f"keep_{window}"): vol.All(vol.Coerce(int), vol.Range(min=0))
            for window in KEEP_WINDOWS
        },
    }
)

FORGET_GROUP_SCHEMA = TARGET_SCHEMA.extend({vol.Required(ATTR_CONFIRM): cv.boolean})

FORGET_SNAPSHOT_SCHEMA = TARGET_SCHEMA.extend(
    {
        vol.Required(ATTR_BACKUP_TIME): vol.Any(cv.datetime, vol.Coerce(int)),
        vol.Required(ATTR_CONFIRM): cv.boolean,
    }
)

PROTECT_SCHEMA = TARGET_SCHEMA.extend(
    {
        vol.Required(ATTR_BACKUP_TIME): vol.Any(cv.datetime, vol.Coerce(int)),
        vol.Required(ATTR_PROTECTED): cv.boolean,
    }
)


@dataclass(slots=True)
class Target:
    """One resolved action target: a datastore, optionally a group inside it."""

    entry: PbsConfigEntry
    store: str
    stats: GroupStats | None

    @property
    def label(self) -> str:
        """Return a human readable name for messages and events."""
        return self.stats.display_name if self.stats else f"datastore {self.store}"


def _resolve(hass: HomeAssistant, call: ServiceCall, *, group: bool) -> list[Target]:
    """Turn the device targets of a call into datastores and backup groups."""
    registry = dr.async_get(hass)
    targets: list[Target] = []

    for device_id in call.data[ATTR_DEVICE_ID]:
        device = registry.async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"Unknown device: {device_id}")

        identifiers = {
            ident for domain, ident in device.identifiers if domain == DOMAIN
        }
        if not identifiers:
            raise ServiceValidationError(
                f"{device.name} does not belong to Proxmox Backup Server"
            )

        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN:
                continue
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(f"{entry.title} is not loaded right now")
            targets.extend(_match(entry, identifiers))

    if not targets:
        raise ServiceValidationError("No Proxmox Backup Server device was targeted")

    if group and any(target.stats is None for target in targets):
        raise ServiceValidationError(
            "This action needs a backup group device, not a datastore"
        )
    return targets


def _match(entry: PbsConfigEntry, identifiers: set[str]) -> list[Target]:
    """Find which datastore or group the given device identifiers refer to."""
    runtime = entry.runtime_data
    medium = runtime.medium.data
    found: list[Target] = []
    if medium is None:
        return found

    for store, data in medium.datastores.items():
        if datastore_device_id(runtime, store) in identifiers:
            found.append(Target(entry, store, None))
        for stats in data.stats.values():
            if group_device_id(runtime, store, stats.key) in identifiers:
                found.append(Target(entry, store, stats))
    return found


def _require_write(target: Target) -> None:
    """Refuse when write actions are switched off in the options."""
    if not target.entry.runtime_data.allow_write:
        raise ServiceValidationError(
            "Write actions are disabled. Enable them in the integration options."
        )


def _require_destructive(target: Target) -> None:
    """Refuse when destructive actions are switched off in the options."""
    _require_write(target)
    if not target.entry.runtime_data.allow_destructive:
        raise ServiceValidationError(
            "Destructive actions are disabled. Enable them in the integration options."
        )


def _require_idle(target: Target) -> None:
    """Refuse to delete while the datastore is being written to.

    Pruning during a running backup can remove a snapshot the client is still
    referencing, so a busy datastore is a hard stop rather than a warning.
    """
    fast = target.entry.runtime_data.fast.data
    operations = fast.active.get(target.store) if fast else None
    if operations is not None and operations.write:
        raise ServiceValidationError(
            f"{target.store} has {operations.write} write operations running. "
            "Try again once the backups have finished."
        )


def _as_unix(value: Any) -> int:
    """Return a datetime or number as a UNIX timestamp."""
    if isinstance(value, int):
        return value
    stamp = dt_util.as_utc(value)
    return int(stamp.timestamp())


def _fire_destructive(
    hass: HomeAssistant, call: ServiceCall, target: Target, detail: dict[str, Any]
) -> None:
    """Announce an irreversible change so an automation can record it."""
    hass.bus.async_fire(
        EVENT_DESTRUCTIVE_ACTION,
        {
            "action": call.service,
            "server": target.entry.title,
            "datastore": target.store,
            "target": target.label,
            "context_user_id": call.context.user_id,
            **detail,
        },
    )


async def _async_run_gc(call: ServiceCall) -> None:
    """Start garbage collection on every targeted datastore."""
    for target in _resolve(call.hass, call, group=False):
        _require_write(target)
        runtime = target.entry.runtime_data
        try:
            upid = await runtime.client.start_garbage_collection(target.store)
        except PbsError as err:
            raise HomeAssistantError(
                f"Garbage collection on {target.store} failed to start: {err}"
            ) from err
        runtime.tasks.async_track(
            upid, "garbage_collection", target.store, target.store
        )


async def _async_verify(call: ServiceCall) -> None:
    """Verify a datastore or a single backup group."""
    for target in _resolve(call.hass, call, group=False):
        _require_write(target)
        runtime = target.entry.runtime_data
        stats = target.stats
        try:
            upid = await runtime.client.start_verify(
                target.store,
                namespace=stats.namespace if stats else "",
                backup_type=stats.backup_type if stats else None,
                backup_id=stats.backup_id if stats else None,
                ignore_verified=call.data.get(ATTR_IGNORE_VERIFIED),
                outdated_after=call.data.get(ATTR_OUTDATED_AFTER),
            )
        except PbsError as err:
            raise HomeAssistantError(
                f"Verification of {target.label} failed to start: {err}"
            ) from err
        runtime.tasks.async_track(upid, "verify", target.label, target.store)


async def _async_prune(call: ServiceCall) -> ServiceResponse:
    """Prune backup groups, dry run by default.

    Returns what was or would be removed, so an automation can decide whether
    to run it for real.
    """
    keep = {
        window: call.data[f"keep_{window}"]
        for window in KEEP_WINDOWS
        if f"keep_{window}" in call.data
    }
    if not keep:
        raise ServiceValidationError(
            "Set at least one keep-* value, otherwise prune would remove everything"
        )

    dry_run = call.data[ATTR_DRY_RUN]
    results: dict[str, Any] = {"dry_run": dry_run, "groups": []}

    for target in _resolve(call.hass, call, group=True):
        _require_destructive(target)
        if not dry_run:
            _require_idle(target)
        stats = target.stats
        assert stats is not None
        runtime = target.entry.runtime_data

        try:
            snapshots = await runtime.client.prune_group(
                target.store,
                stats.backup_type,
                stats.backup_id,
                namespace=stats.namespace,
                dry_run=dry_run,
                keep=keep,
            )
        except PbsError as err:
            raise HomeAssistantError(f"Prune of {target.label} failed: {err}") from err

        removed = [
            dt_util.utc_from_timestamp(int(entry["backup-time"])).isoformat()
            for entry in snapshots
            if not entry.get("keep") and entry.get("backup-time") is not None
        ]
        results["groups"].append(
            {
                "group": stats.key,
                "name": stats.display_name,
                "datastore": target.store,
                "removed": removed,
                "removed_count": len(removed),
                "kept_count": len(snapshots) - len(removed),
            }
        )

        if not dry_run:
            runtime.tasks.async_record("prune", target.label, target.store, "ok")
            _fire_destructive(
                call.hass, call, target, {"snapshots": removed, "dry_run": False}
            )
            await runtime.medium.async_request_refresh()

    return results


async def _async_forget_group(call: ServiceCall) -> None:
    """Delete whole backup groups including every snapshot."""
    if not call.data[ATTR_CONFIRM]:
        raise ServiceValidationError(
            "Set confirm: true — this deletes all backups of the group irreversibly"
        )

    for target in _resolve(call.hass, call, group=True):
        _require_destructive(target)
        _require_idle(target)
        stats = target.stats
        assert stats is not None
        if stats.protected:
            raise ServiceValidationError(
                f"{target.label} has {stats.protected} protected snapshots. "
                "Remove the protection first."
            )
        runtime = target.entry.runtime_data
        try:
            await runtime.client.forget_group(
                target.store,
                stats.backup_type,
                stats.backup_id,
                namespace=stats.namespace,
            )
        except PbsError as err:
            raise HomeAssistantError(f"Could not delete {target.label}: {err}") from err

        runtime.tasks.async_record("forget_group", target.label, target.store, "ok")
        _fire_destructive(
            call.hass, call, target, {"snapshots_deleted": stats.snapshot_count}
        )
        await runtime.medium.async_request_refresh()


async def _async_forget_snapshot(call: ServiceCall) -> None:
    """Delete one snapshot of a backup group."""
    if not call.data[ATTR_CONFIRM]:
        raise ServiceValidationError(
            "Set confirm: true — this deletes the snapshot irreversibly"
        )
    backup_time = _as_unix(call.data[ATTR_BACKUP_TIME])

    for target in _resolve(call.hass, call, group=True):
        _require_destructive(target)
        _require_idle(target)
        stats = target.stats
        assert stats is not None
        runtime = target.entry.runtime_data
        try:
            await runtime.client.forget_snapshot(
                target.store,
                stats.backup_type,
                stats.backup_id,
                backup_time,
                namespace=stats.namespace,
            )
        except PbsError as err:
            raise HomeAssistantError(
                f"Could not delete the snapshot of {target.label}: {err}"
            ) from err

        runtime.tasks.async_record("forget_snapshot", target.label, target.store, "ok")
        _fire_destructive(call.hass, call, target, {"backup_time": backup_time})
        await runtime.medium.async_request_refresh()


async def _async_set_protected(call: ServiceCall) -> None:
    """Protect or unprotect one snapshot against pruning."""
    backup_time = _as_unix(call.data[ATTR_BACKUP_TIME])
    protected = call.data[ATTR_PROTECTED]

    for target in _resolve(call.hass, call, group=True):
        _require_write(target)
        stats = target.stats
        assert stats is not None
        runtime = target.entry.runtime_data
        try:
            await runtime.client.set_protected(
                target.store,
                stats.backup_type,
                stats.backup_id,
                backup_time,
                protected,
                namespace=stats.namespace,
            )
        except PbsError as err:
            raise HomeAssistantError(
                f"Could not change the protection of {target.label}: {err}"
            ) from err
        await runtime.medium.async_request_refresh()


SERVICES: tuple[tuple[str, Any, Any, SupportsResponse], ...] = (
    (SERVICE_RUN_GC, _async_run_gc, TARGET_SCHEMA, SupportsResponse.NONE),
    (SERVICE_VERIFY, _async_verify, VERIFY_SCHEMA, SupportsResponse.NONE),
    (SERVICE_PRUNE, _async_prune, PRUNE_SCHEMA, SupportsResponse.OPTIONAL),
    (
        SERVICE_FORGET_GROUP,
        _async_forget_group,
        FORGET_GROUP_SCHEMA,
        SupportsResponse.NONE,
    ),
    (
        SERVICE_FORGET_SNAPSHOT,
        _async_forget_snapshot,
        FORGET_SNAPSHOT_SCHEMA,
        SupportsResponse.NONE,
    ),
    (
        SERVICE_SET_PROTECTED,
        _async_set_protected,
        PROTECT_SCHEMA,
        SupportsResponse.NONE,
    ),
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register every action once, independently of any config entry."""
    for name, handler, schema, response in SERVICES:
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=response
        )
