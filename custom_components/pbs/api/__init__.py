"""Home Assistant agnostic client for the Proxmox Backup Server API."""

from .client import PbsClient
from .exceptions import (
    PbsApiError,
    PbsAuthError,
    PbsConnectionError,
    PbsError,
    PbsNotFoundError,
    PbsPermissionError,
)

__all__ = [
    "PbsApiError",
    "PbsAuthError",
    "PbsClient",
    "PbsConnectionError",
    "PbsError",
    "PbsNotFoundError",
    "PbsPermissionError",
]
