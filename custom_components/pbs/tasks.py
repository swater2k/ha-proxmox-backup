"""Tracking of the worker tasks this integration starts.

Garbage collection, verification and job runs are asynchronous: PBS returns a
UPID immediately and does the work in the background. Waiting for them inside a
service call would block Home Assistant for minutes, so the UPID goes on a watch
list instead and is polled until the task finishes. The result is published as
an event and through the last-action entities.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import PbsClient, PbsError
from .const import EVENT_TASK_FINISHED, LOGGER, TASK_POLL_INTERVAL


class PbsTaskTracker:
    """Follows every task this integration started until it finishes."""

    def __init__(self, hass: HomeAssistant, client: PbsClient, title: str) -> None:
        """Set up an idle tracker. Polling starts with the first task."""
        self.hass = hass
        self.client = client
        self.title = title
        self.last: dict[str, Any] | None = None
        self._watching: dict[str, dict[str, Any]] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def running(self) -> bool:
        """Return True while at least one started task is still in progress."""
        return bool(self._watching)

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Register a callback that fires whenever the tracked state changes."""
        self._listeners.append(update)

        def _remove() -> None:
            self._listeners.remove(update)

        return _remove

    @callback
    def _notify(self) -> None:
        """Tell the entities that something changed."""
        for update in list(self._listeners):
            update()

    @callback
    def async_track(self, upid: str, action: str, target: str, store: str) -> None:
        """Put a freshly started task on the watch list."""
        started = dt_util.utcnow()
        self._watching[upid] = {
            "upid": upid,
            "action": action,
            "target": target,
            "store": store,
            "started": started,
        }
        self.last = {**self._watching[upid], "state": "running", "finished": None}
        LOGGER.debug("Tracking %s task %s on %s", action, upid, target)

        if self._unsub is None:
            self._unsub = async_track_time_interval(
                self.hass, self._async_poll, TASK_POLL_INTERVAL
            )
        self._notify()

    @callback
    def async_record(self, action: str, target: str, store: str, state: str) -> None:
        """Record a synchronous action that has no UPID, such as prune."""
        now = dt_util.utcnow()
        self.last = {
            "upid": None,
            "action": action,
            "target": target,
            "store": store,
            "started": now,
            "finished": now,
            "state": state,
        }
        self._notify()

    async def _async_poll(self, _now: datetime) -> None:
        """Check every watched task and publish the ones that finished."""
        for upid, context in list(self._watching.items()):
            try:
                task = await self.client.get_task_status(upid)
            except PbsError as err:
                # Keep watching: a brief API hiccup is not a finished task.
                LOGGER.debug("Could not read task %s: %s", upid, err)
                continue

            if task.is_running:
                continue

            del self._watching[upid]
            state = "ok" if not task.is_failed else "error"
            finished = task.end_time or dt_util.utcnow()
            self.last = {
                **context,
                "state": state,
                "finished": finished,
                "status": task.status,
            }
            LOGGER.debug("Task %s finished: %s", upid, task.status)

            self.hass.bus.async_fire(
                EVENT_TASK_FINISHED,
                {
                    "server": self.title,
                    "upid": upid,
                    "action": context["action"],
                    "target": context["target"],
                    "datastore": context["store"],
                    "state": state,
                    "status": task.status,
                },
            )

        if not self._watching and self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._notify()

    @callback
    def async_shutdown(self) -> None:
        """Stop polling when the config entry is unloaded."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._watching.clear()
        self._listeners.clear()
