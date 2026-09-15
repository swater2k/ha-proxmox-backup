"""Exceptions raised by the Proxmox Backup Server API client."""

from __future__ import annotations


class PbsError(Exception):
    """Base class for every error raised by the client."""


class PbsConnectionError(PbsError):
    """The host could not be reached, or answered too slowly."""


class PbsAuthError(PbsError):
    """The API token was rejected (HTTP 401).

    Always maps to a re-authentication flow: the token was deleted, the secret
    rotated, or the token is expired.
    """


class PbsPermissionError(PbsError):
    """The token is valid but lacks privileges for this path (HTTP 403).

    Never fatal for the integration as a whole. PBS builds the intersection of
    user and token ACLs, so a partially privileged token is a normal state that
    must only disable the affected entities.
    """

    def __init__(self, path: str) -> None:
        """Remember which endpoint was denied."""
        super().__init__(f"Missing permission for {path}")
        self.path = path


class PbsNotFoundError(PbsError):
    """The endpoint or object does not exist (HTTP 404).

    Also raised for endpoints that a given PBS version does not provide yet,
    which is why it is treated like a missing capability rather than an error.
    """

    def __init__(self, path: str) -> None:
        """Remember which endpoint was missing."""
        super().__init__(f"Not found: {path}")
        self.path = path


class PbsApiError(PbsError):
    """PBS answered with an unexpected status or an unparsable body."""
