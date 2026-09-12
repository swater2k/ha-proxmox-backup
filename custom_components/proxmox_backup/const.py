"""Constants for the Proxmox Backup Server integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

DOMAIN: Final = "proxmox_backup"
LOGGER: Final = logging.getLogger(__package__)

MANUFACTURER: Final = "Proxmox"
MODEL_SERVER: Final = "Proxmox Backup Server"
MODEL_DATASTORE: Final = "PBS Datastore"

# configuration keys
CONF_TOKEN_ID: Final = "token_id"
CONF_TOKEN_SECRET: Final = "token_secret"
CONF_DATASTORES: Final = "datastores"

# option keys
CONF_STALE_DAYS: Final = "stale_days"
CONF_USAGE_WARNING: Final = "usage_warning"
CONF_USAGE_CRITICAL: Final = "usage_critical"
CONF_GC_WARNING_DAYS: Final = "gc_warning_days"
CONF_ALLOW_WRITE: Final = "allow_write"
CONF_ALLOW_DESTRUCTIVE: Final = "allow_destructive"

DEFAULT_PORT: Final = 8007
DEFAULT_VERIFY_SSL: Final = False
DEFAULT_STALE_DAYS: Final = 2
DEFAULT_USAGE_WARNING: Final = 80
DEFAULT_USAGE_CRITICAL: Final = 90
DEFAULT_GC_WARNING_DAYS: Final = 3

# polling
SCAN_INTERVAL_FAST: Final = timedelta(seconds=60)
SCAN_INTERVAL_MEDIUM: Final = timedelta(minutes=5)
SCAN_INTERVAL_SLOW: Final = timedelta(hours=6)

# Above this number of snapshots the per-snapshot fetch stops running on every
# medium cycle and is throttled to roughly once an hour. Keeps large datastores
# from hammering the API while small ones stay fully up to date.
SNAPSHOT_THROTTLE_LIMIT: Final = 1500
SNAPSHOT_THROTTLE_EVERY: Final = 12

# how far back failed tasks are counted
FAILED_TASK_WINDOWS: Final = {"24h": 1, "7d": 7}
TASK_HISTORY_LIMIT: Final = 500

# capability names used to remember which endpoints the token may not read
CAP_NODE_STATUS: Final = "node_status"
CAP_SERVICES: Final = "services"
CAP_SUBSCRIPTION: Final = "subscription"
CAP_UPDATES: Final = "updates"
CAP_CERTIFICATES: Final = "certificates"
CAP_DISKS: Final = "disks"
CAP_TASKS: Final = "tasks"
CAP_JOBS: Final = "jobs"
CAP_ACTIVE_OPERATIONS: Final = "active_operations"
CAP_DATASTORE_CONFIG: Final = "datastore_config"
CAP_NAMESPACES: Final = "namespaces"
