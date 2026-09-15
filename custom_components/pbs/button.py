"""Button platform: the parameterless actions.

Only created when write actions are enabled in the options. Deliberately no
button for prune or forget — a single mis-tap in a dashboard would delete
backups without a confirmation dialog, so those live in actions with required
parameters instead.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PbsError
from .coordinator import GroupStats, PbsConfigEntry
from .entity import PbsDatastoreEntity, PbsGroupEntity
from .sensor import PbsJobEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PbsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the action buttons, if the options allow write actions."""
    runtime = entry.runtime_data
    if not runtime.allow_write:
        return

    entities: list[ButtonEntity] = []
    for store in runtime.medium.stores:
        entities.append(PbsGarbageCollectionButton(entry, runtime.fast, store))
        entities.append(PbsVerifyDatastoreButton(entry, runtime.fast, store))
    async_add_entities(entities)

    known_groups: set[tuple[str, str]] = set()
    known_jobs: set[tuple[str, str]] = set()

    @callback
    def _async_add_dynamic() -> None:
        medium = runtime.medium.data
        if medium is None:
            return
        new: list[ButtonEntity] = []

        for store, data in medium.datastores.items():
            for stats in data.stats.values():
                ident = (store, stats.key)
                if ident in known_groups:
                    continue
                known_groups.add(ident)
                new.append(PbsVerifyGroupButton(entry, runtime.medium, store, stats))

        monitored = set(runtime.medium.stores)
        for job in medium.jobs:
            ident = (job.kind, job.job_id)
            if ident in known_jobs or job.store not in monitored:
                continue
            known_jobs.add(ident)
            new.append(PbsRunJobButton(entry, runtime.medium, job))

        if new:
            async_add_entities(new)

    _async_add_dynamic()
    entry.async_on_unload(runtime.medium.async_add_listener(_async_add_dynamic))


class PbsGarbageCollectionButton(PbsDatastoreEntity, ButtonEntity):
    """Start a garbage collection run on one datastore."""

    _attr_translation_key = "start_gc"

    def __init__(self, entry: PbsConfigEntry, coordinator, store: str) -> None:
        """Set up the garbage collection button."""
        super().__init__(entry, coordinator, store, "start_gc")

    async def async_press(self) -> None:
        """Start the run and follow it through the task tracker."""
        runtime = self.entry.runtime_data
        try:
            upid = await runtime.client.start_garbage_collection(self.store)
        except PbsError as err:
            raise HomeAssistantError(
                f"Could not start garbage collection on {self.store}: {err}"
            ) from err
        runtime.tasks.async_track(upid, "garbage_collection", self.store, self.store)
        await runtime.fast.async_request_refresh()


class PbsVerifyDatastoreButton(PbsDatastoreEntity, ButtonEntity):
    """Verify every snapshot of one datastore."""

    _attr_translation_key = "verify_datastore"

    def __init__(self, entry: PbsConfigEntry, coordinator, store: str) -> None:
        """Set up the datastore verify button."""
        super().__init__(entry, coordinator, store, "verify_datastore")

    async def async_press(self) -> None:
        """Start the verification and follow it through the task tracker."""
        runtime = self.entry.runtime_data
        try:
            upid = await runtime.client.start_verify(self.store)
        except PbsError as err:
            raise HomeAssistantError(
                f"Could not start verification on {self.store}: {err}"
            ) from err
        runtime.tasks.async_track(upid, "verify", self.store, self.store)
        await runtime.fast.async_request_refresh()


class PbsVerifyGroupButton(PbsGroupEntity, ButtonEntity):
    """Verify the snapshots of one backup group."""

    _attr_translation_key = "verify_group"

    def __init__(
        self, entry: PbsConfigEntry, coordinator, store: str, stats: GroupStats
    ) -> None:
        """Set up the group verify button."""
        super().__init__(entry, coordinator, store, stats, "verify_group")
        self._backup_type = stats.backup_type
        self._backup_id = stats.backup_id
        self._namespace = stats.namespace

    async def async_press(self) -> None:
        """Start the verification for this group only."""
        runtime = self.entry.runtime_data
        stats = self.stats
        target = stats.display_name if stats else self.group_key
        try:
            upid = await runtime.client.start_verify(
                self.store,
                namespace=self._namespace,
                backup_type=self._backup_type,
                backup_id=self._backup_id,
            )
        except PbsError as err:
            raise HomeAssistantError(
                f"Could not start verification for {target}: {err}"
            ) from err
        runtime.tasks.async_track(upid, "verify", target, self.store)
        await runtime.fast.async_request_refresh()


class PbsRunJobButton(PbsJobEntity, ButtonEntity):
    """Run one configured prune, verify or sync job now."""

    _attr_translation_key = "run_job"

    def __init__(self, entry: PbsConfigEntry, coordinator, job) -> None:
        """Set up the job run button."""
        super().__init__(entry, coordinator, job, "run")

    async def async_press(self) -> None:
        """Start the job outside its schedule."""
        runtime = self.entry.runtime_data
        target = f"{self._kind} job {self._job_id}"
        try:
            upid = await runtime.client.run_job(self._kind, self._job_id)
        except PbsError as err:
            raise HomeAssistantError(f"Could not start {target}: {err}") from err
        runtime.tasks.async_track(upid, self._kind, target, self.store)
        await runtime.fast.async_request_refresh()
