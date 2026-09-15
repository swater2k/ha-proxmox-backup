"""Health evaluation for the aggregated status sensors.

Kept free of Home Assistant imports so the rules can be unit tested against
captured fixtures without a running instance. Everything in here answers one
question: what is currently wrong, and how bad is it?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .const import (
    CONF_GC_WARNING_DAYS,
    CONF_STALE_DAYS,
    CONF_USAGE_CRITICAL,
    CONF_USAGE_WARNING,
    DEFAULT_GC_WARNING_DAYS,
    DEFAULT_STALE_DAYS,
    DEFAULT_USAGE_CRITICAL,
    DEFAULT_USAGE_WARNING,
)

STATE_OK = "ok"
STATE_WARNING = "warning"
STATE_CRITICAL = "critical"

OVERALL_STATES = [STATE_OK, STATE_WARNING, STATE_CRITICAL]
_RANK = {STATE_OK: 0, STATE_WARNING: 1, STATE_CRITICAL: 2}


@dataclass(frozen=True, slots=True)
class Finding:
    """One concrete thing that is wrong."""

    level: str
    scope: str
    message: str


def worst(findings: list[Finding]) -> str:
    """Return the overall state implied by a list of findings."""
    if not findings:
        return STATE_OK
    return max((finding.level for finding in findings), key=lambda level: _RANK[level])


def _age_days(stamp: datetime | None) -> float | None:
    """Return how many days ago ``stamp`` was."""
    if stamp is None:
        return None
    return (datetime.now(UTC) - stamp).total_seconds() / 86400


def datastore_findings(
    store: str, fast: Any, medium: Any, options: dict[str, Any]
) -> list[Finding]:
    """Evaluate one datastore: space, garbage collection and verification."""
    findings: list[Finding] = []
    warning = options.get(CONF_USAGE_WARNING, DEFAULT_USAGE_WARNING)
    critical = options.get(CONF_USAGE_CRITICAL, DEFAULT_USAGE_CRITICAL)
    gc_days = options.get(CONF_GC_WARNING_DAYS, DEFAULT_GC_WARNING_DAYS)

    usage = fast.usage.get(store) if fast else None
    if usage is not None:
        if usage.error:
            findings.append(
                Finding(STATE_CRITICAL, store, f"datastore error: {usage.error}")
            )
        percent = usage.used_percent
        if percent is not None:
            if percent >= critical:
                findings.append(
                    Finding(
                        STATE_CRITICAL, store, f"{percent:.1f} % used (>= {critical} %)"
                    )
                )
            elif percent >= warning:
                findings.append(
                    Finding(
                        STATE_WARNING, store, f"{percent:.1f} % used (>= {warning} %)"
                    )
                )

    data = medium.datastores.get(store) if medium else None
    if data is None:
        return findings

    if data.config and data.config.in_maintenance:
        findings.append(
            Finding(
                STATE_WARNING,
                store,
                f"maintenance mode active ({data.config.maintenance_mode})",
            )
        )

    if data.gc:
        if data.gc.state == "error":
            findings.append(
                Finding(STATE_WARNING, store, "last garbage collection failed")
            )
        if data.gc.still_bad:
            findings.append(
                Finding(
                    STATE_CRITICAL,
                    store,
                    f"{data.gc.still_bad} corrupt chunks left behind",
                )
            )
        age = _age_days(data.gc.last_run_endtime)
        if age is not None and age > gc_days:
            findings.append(
                Finding(
                    STATE_WARNING, store, f"no garbage collection for {age:.0f} days"
                )
            )

    if data.verify_failed:
        findings.append(
            Finding(
                STATE_CRITICAL,
                store,
                f"{data.verify_failed} snapshots failed verification",
            )
        )

    return findings


def group_findings(medium: Any, options: dict[str, Any]) -> list[Finding]:
    """Evaluate every backup group: is its newest backup recent enough."""
    stale_days = options.get(CONF_STALE_DAYS, DEFAULT_STALE_DAYS)
    findings: list[Finding] = []

    for _store, stats in medium.all_groups() if medium else []:
        age = stats.age_days
        if age is None:
            findings.append(
                Finding(STATE_WARNING, stats.key, f"{stats.display_name}: no backup")
            )
        elif age > stale_days:
            findings.append(
                Finding(
                    STATE_WARNING,
                    stats.key,
                    f"{stats.display_name}: last backup {age:.1f} days ago",
                )
            )
    return findings


def instance_findings(
    fast: Any, medium: Any, slow: Any, options: dict[str, Any]
) -> list[Finding]:
    """Evaluate the whole server: every datastore, group, job, task and disk."""
    findings: list[Finding] = []

    if medium is not None:
        for store in medium.datastores:
            findings.extend(datastore_findings(store, fast, medium, options))
        findings.extend(group_findings(medium, options))

        failed = medium.failed_within(1)
        if failed:
            findings.append(
                Finding(STATE_WARNING, "tasks", f"{failed} tasks failed in 24 h")
            )

        for job in medium.jobs:
            if job.state == "error":
                findings.append(
                    Finding(
                        STATE_WARNING,
                        f"job:{job.job_id}",
                        f"{job.kind} job {job.job_id} failed",
                    )
                )

    if slow is not None:
        for service in slow.failed_services:
            findings.append(
                Finding(
                    STATE_CRITICAL,
                    "services",
                    f"service {service.service} is not running",
                )
            )
        for disk in slow.unhealthy_disks:
            findings.append(
                Finding(STATE_CRITICAL, "disks", f"disk {disk.name} failed SMART")
            )

    # Stable order: worst first, then alphabetical. Without this the attribute
    # would reshuffle on every poll and spam the recorder with state changes.
    return sorted(findings, key=lambda f: (-_RANK[f.level], f.message))
