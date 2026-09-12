"""Typed models for the Proxmox Backup Server API.

Every model is parsed defensively: PBS adds and removes fields between minor
releases, and a missing key must never take the whole integration down. Unknown
keys are kept in ``raw`` so diagnostics can show what the server really sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# --------------------------------------------------------------------------
# converters
# --------------------------------------------------------------------------


def as_int(value: Any, default: int | None = None) -> int | None:
    """Return ``value`` as int, or ``default`` when it is not numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float | None = None) -> float | None:
    """Return ``value`` as float, or ``default`` when it is not numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> bool:
    """Return ``value`` as bool. PBS uses 0/1 as well as true/false."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def as_timestamp(value: Any) -> datetime | None:
    """Return a UNIX timestamp as aware datetime.

    PBS uses 0 for "never" in several places (for example an unknown
    estimated-full-date), which must become ``None`` and not 1970.
    """
    seconds = as_int(value)
    if not seconds or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def percentage(used: int | None, total: int | None) -> float | None:
    """Return used/total as percentage, guarding against division by zero."""
    if used is None or not total:
        return None
    return round(used / total * 100, 2)


# --------------------------------------------------------------------------
# node level
# --------------------------------------------------------------------------


@dataclass(slots=True)
class VersionInfo:
    """Answer of ``/version``."""

    version: str | None
    release: str | None
    repoid: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> VersionInfo:
        """Build from the raw payload."""
        return cls(
            version=raw.get("version"),
            release=raw.get("release"),
            repoid=raw.get("repoid"),
            raw=raw,
        )

    @property
    def full(self) -> str | None:
        """Return version and release as one display string."""
        if self.version and self.release:
            return f"{self.version}-{self.release}"
        return self.version


@dataclass(slots=True)
class NodeStatus:
    """Answer of ``/nodes/{node}/status``."""

    uptime: int | None
    cpu: float | None
    wait: float | None
    load: list[float]
    memory_total: int | None
    memory_used: int | None
    swap_total: int | None
    swap_used: int | None
    root_total: int | None
    root_used: int | None
    root_available: int | None
    kernel_version: str | None
    fingerprint: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> NodeStatus:
        """Build from the raw payload."""
        memory = raw.get("memory") or {}
        swap = raw.get("swap") or {}
        root = raw.get("root") or {}
        info = raw.get("info") or {}
        load = [
            value
            for value in (as_float(entry) for entry in raw.get("loadavg") or [])
            if value is not None
        ]
        root_total = as_int(root.get("total"))
        root_used = as_int(root.get("used"))
        root_avail = as_int(root.get("avail"))
        if root_used is None and root_total is not None and root_avail is not None:
            root_used = root_total - root_avail
        return cls(
            uptime=as_int(raw.get("uptime")),
            cpu=as_float(raw.get("cpu")),
            wait=as_float(raw.get("wait")),
            load=load,
            memory_total=as_int(memory.get("total")),
            memory_used=as_int(memory.get("used")),
            swap_total=as_int(swap.get("total")),
            swap_used=as_int(swap.get("used")),
            root_total=root_total,
            root_used=root_used,
            root_available=root_avail,
            kernel_version=raw.get("kversion"),
            fingerprint=info.get("fingerprint"),
            raw=raw,
        )

    @property
    def cpu_percent(self) -> float | None:
        """Return CPU load as percentage. PBS reports a 0..1 fraction."""
        return None if self.cpu is None else round(self.cpu * 100, 2)

    @property
    def wait_percent(self) -> float | None:
        """Return IO wait as percentage."""
        return None if self.wait is None else round(self.wait * 100, 2)

    @property
    def memory_percent(self) -> float | None:
        """Return used memory as percentage."""
        return percentage(self.memory_used, self.memory_total)

    @property
    def swap_percent(self) -> float | None:
        """Return used swap as percentage."""
        return percentage(self.swap_used, self.swap_total)

    @property
    def root_percent(self) -> float | None:
        """Return used root filesystem as percentage."""
        return percentage(self.root_used, self.root_total)

    def load_at(self, index: int) -> float | None:
        """Return the 1/5/15 minute load average, if present."""
        return self.load[index] if len(self.load) > index else None


@dataclass(slots=True)
class ServiceInfo:
    """One entry of ``/nodes/{node}/services``."""

    service: str
    name: str | None
    state: str | None
    unit_state: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ServiceInfo:
        """Build from the raw payload."""
        return cls(
            service=raw.get("service") or raw.get("name") or "unknown",
            name=raw.get("name") or raw.get("desc"),
            state=raw.get("state"),
            unit_state=raw.get("unit-state"),
        )

    @property
    def is_running(self) -> bool:
        """Return True when systemd reports the unit as running."""
        return (self.state or "").lower() == "running"

    @property
    def is_enabled(self) -> bool:
        """Return True when the unit is enabled, so it is expected to run."""
        return (self.unit_state or "").lower() in {"enabled", "enabled-runtime"}


@dataclass(slots=True)
class CertificateInfo:
    """One entry of ``/nodes/{node}/certificates/info``."""

    filename: str | None
    not_after: datetime | None
    not_before: datetime | None
    subject: str | None
    issuer: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> CertificateInfo:
        """Build from the raw payload."""
        return cls(
            filename=raw.get("filename"),
            not_after=as_timestamp(raw.get("notafter")),
            not_before=as_timestamp(raw.get("notbefore")),
            subject=raw.get("subject"),
            issuer=raw.get("issuer"),
        )


@dataclass(slots=True)
class DiskInfo:
    """One entry of ``/nodes/{node}/disks/list``."""

    name: str
    devpath: str | None
    model: str | None
    size: int | None
    status: str | None
    wearout: float | None
    used: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> DiskInfo:
        """Build from the raw payload."""
        devpath = raw.get("devpath")
        name = raw.get("name") or (devpath.rsplit("/", 1)[-1] if devpath else "disk")
        wearout = as_float(raw.get("wearout"))
        return cls(
            name=name,
            devpath=devpath,
            model=raw.get("model"),
            size=as_int(raw.get("size")),
            status=raw.get("status"),
            wearout=wearout,
            used=raw.get("used"),
        )

    @property
    def is_healthy(self) -> bool | None:
        """Return SMART health. ``None`` when the disk reports no verdict."""
        status = (self.status or "").lower()
        if status in {"passed", "ok"}:
            return True
        if status in {"failed", "bad"}:
            return False
        return None


@dataclass(slots=True)
class SubscriptionInfo:
    """Answer of ``/nodes/{node}/subscription``."""

    status: str | None
    product_name: str | None
    message: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SubscriptionInfo:
        """Build from the raw payload."""
        return cls(
            status=(raw.get("status") or None),
            product_name=raw.get("productname"),
            message=raw.get("message"),
        )


@dataclass(slots=True)
class TaskInfo:
    """One entry of ``/nodes/{node}/tasks``."""

    upid: str
    worker_type: str | None
    worker_id: str | None
    user: str | None
    start_time: datetime | None
    end_time: datetime | None
    status: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> TaskInfo:
        """Build from the raw payload."""
        return cls(
            upid=raw.get("upid") or "",
            worker_type=raw.get("type") or raw.get("worker_type"),
            worker_id=raw.get("id") or raw.get("worker_id"),
            user=raw.get("user"),
            start_time=as_timestamp(raw.get("starttime")),
            end_time=as_timestamp(raw.get("endtime")),
            status=raw.get("status"),
        )

    @property
    def is_running(self) -> bool:
        """Return True while the task has no exit status yet."""
        return self.status is None and self.end_time is None

    @property
    def is_failed(self) -> bool:
        """Return True for a finished task that did not end with OK.

        PBS reports warnings as ``WARNINGS: 3`` and success as ``OK``; anything
        else is an error string.
        """
        if self.status is None:
            return False
        return not self.status.startswith("OK")

    @property
    def description(self) -> str:
        """Return a short human readable label for dashboards."""
        if self.worker_id:
            return f"{self.worker_type}: {self.worker_id}"
        return self.worker_type or self.upid


@dataclass(slots=True)
class UpdateInfo:
    """One entry of ``/nodes/{node}/apt/update``."""

    package: str
    version: str | None
    old_version: str | None
    title: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> UpdateInfo:
        """Build from the raw payload."""
        return cls(
            package=raw.get("Package") or raw.get("package") or "unknown",
            version=raw.get("Version") or raw.get("version"),
            old_version=raw.get("OldVersion") or raw.get("old_version"),
            title=raw.get("Title") or raw.get("title"),
        )


# --------------------------------------------------------------------------
# datastore level
# --------------------------------------------------------------------------


@dataclass(slots=True)
class DatastoreUsage:
    """One entry of ``/status/datastore-usage``."""

    store: str
    total: int | None
    used: int | None
    available: int | None
    estimated_full: datetime | None
    error: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> DatastoreUsage:
        """Build from the raw payload."""
        return cls(
            store=raw.get("store") or "",
            total=as_int(raw.get("total")),
            used=as_int(raw.get("used")),
            available=as_int(raw.get("avail")),
            estimated_full=as_timestamp(raw.get("estimated-full-date")),
            error=raw.get("error"),
        )

    @property
    def used_percent(self) -> float | None:
        """Return used space as percentage."""
        return percentage(self.used, self.total)


@dataclass(slots=True)
class GcStatus:
    """Answer of ``/admin/datastore/{store}/gc``."""

    upid: str | None
    last_run_upid: str | None
    last_run_state: str | None
    last_run_endtime: datetime | None
    duration: int | None
    index_data_bytes: int | None
    disk_bytes: int | None
    removed_bytes: int | None
    pending_bytes: int | None
    still_bad: int | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GcStatus:
        """Build from the raw payload."""
        return cls(
            upid=raw.get("upid"),
            last_run_upid=raw.get("last-run-upid"),
            last_run_state=raw.get("last-run-state"),
            last_run_endtime=as_timestamp(raw.get("last-run-endtime")),
            duration=as_int(raw.get("duration")),
            index_data_bytes=as_int(raw.get("index-data-bytes")),
            disk_bytes=as_int(raw.get("disk-bytes")),
            removed_bytes=as_int(raw.get("removed-bytes")),
            pending_bytes=as_int(raw.get("pending-bytes")),
            still_bad=as_int(raw.get("still-bad")),
            raw=raw,
        )

    @property
    def is_running(self) -> bool:
        """Return True while a GC run is in progress."""
        return bool(self.upid)

    @property
    def state(self) -> str:
        """Return a normalised state for the enum sensor."""
        if self.is_running:
            return "running"
        if not self.last_run_state:
            return "unknown"
        if self.last_run_state.startswith("OK"):
            return "ok"
        return "error"

    @property
    def deduplication_factor(self) -> float | None:
        """Return the dedup factor the PBS web UI shows.

        Logical data referenced by all indexes divided by the bytes actually
        occupied on disk.
        """
        if not self.index_data_bytes or not self.disk_bytes:
            return None
        return round(self.index_data_bytes / self.disk_bytes, 2)


@dataclass(slots=True)
class DatastoreConfig:
    """One entry of ``/config/datastore``."""

    name: str
    path: str | None
    comment: str | None
    gc_schedule: str | None
    maintenance_mode: str | None
    notify: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> DatastoreConfig:
        """Build from the raw payload."""
        return cls(
            name=raw.get("name") or raw.get("store") or "",
            path=raw.get("path"),
            comment=raw.get("comment"),
            gc_schedule=raw.get("gc-schedule"),
            maintenance_mode=raw.get("maintenance-mode"),
            notify=raw.get("notify"),
            raw=raw,
        )

    @property
    def in_maintenance(self) -> bool:
        """Return True when any maintenance mode is configured."""
        return bool(self.maintenance_mode)


@dataclass(slots=True)
class ActiveOperations:
    """Answer of ``/admin/datastore/{store}/active-operations``."""

    read: int
    write: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ActiveOperations:
        """Build from the raw payload."""
        return cls(
            read=as_int(raw.get("read"), 0) or 0,
            write=as_int(raw.get("write"), 0) or 0,
        )


@dataclass(slots=True)
class BackupGroup:
    """One entry of ``/admin/datastore/{store}/groups``."""

    backup_type: str
    backup_id: str
    namespace: str
    last_backup: datetime | None
    backup_count: int
    owner: str | None
    comment: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any], namespace: str = "") -> BackupGroup:
        """Build from the raw payload."""
        return cls(
            backup_type=raw.get("backup-type") or "host",
            backup_id=str(raw.get("backup-id") or ""),
            namespace=raw.get("ns") or namespace or "",
            last_backup=as_timestamp(raw.get("last-backup")),
            backup_count=as_int(raw.get("backup-count"), 0) or 0,
            owner=raw.get("owner"),
            comment=raw.get("comment"),
        )

    @property
    def key(self) -> str:
        """Return a stable identifier including the namespace."""
        prefix = f"{self.namespace}/" if self.namespace else ""
        return f"{prefix}{self.backup_type}/{self.backup_id}"


@dataclass(slots=True)
class Snapshot:
    """One entry of ``/admin/datastore/{store}/snapshots``."""

    backup_type: str
    backup_id: str
    namespace: str
    backup_time: datetime | None
    size: int | None
    owner: str | None
    protected: bool
    comment: str | None
    verify_state: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any], namespace: str = "") -> Snapshot:
        """Build from the raw payload."""
        verification = raw.get("verification") or {}
        return cls(
            backup_type=raw.get("backup-type") or "host",
            backup_id=str(raw.get("backup-id") or ""),
            namespace=raw.get("ns") or namespace or "",
            backup_time=as_timestamp(raw.get("backup-time")),
            size=as_int(raw.get("size")),
            owner=raw.get("owner"),
            protected=as_bool(raw.get("protected")),
            comment=raw.get("comment"),
            verify_state=verification.get("state"),
        )

    @property
    def group_key(self) -> str:
        """Return the key of the group this snapshot belongs to."""
        prefix = f"{self.namespace}/" if self.namespace else ""
        return f"{prefix}{self.backup_type}/{self.backup_id}"


@dataclass(slots=True)
class JobStatus:
    """One entry of ``/admin/prune``, ``/admin/verify`` or ``/admin/sync``."""

    job_id: str
    kind: str
    store: str | None
    schedule: str | None
    comment: str | None
    disabled: bool
    last_run_state: str | None
    last_run_endtime: datetime | None
    last_run_upid: str | None
    next_run: datetime | None

    @classmethod
    def from_api(cls, raw: dict[str, Any], kind: str) -> JobStatus:
        """Build from the raw payload."""
        return cls(
            job_id=raw.get("id") or "",
            kind=kind,
            store=raw.get("store"),
            schedule=raw.get("schedule"),
            comment=raw.get("comment"),
            disabled=as_bool(raw.get("disable")),
            last_run_state=raw.get("last-run-state"),
            last_run_endtime=as_timestamp(raw.get("last-run-endtime")),
            last_run_upid=raw.get("last-run-upid"),
            next_run=as_timestamp(raw.get("next-run")),
        )

    @property
    def state(self) -> str:
        """Return a normalised state for the enum sensor."""
        if self.disabled:
            return "disabled"
        if not self.last_run_state:
            return "unknown"
        if self.last_run_state.startswith("OK"):
            return "ok"
        return "error"
