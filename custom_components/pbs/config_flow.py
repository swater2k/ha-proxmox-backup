"""Config and options flow for Proxmox Backup Server."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    PbsAuthError,
    PbsClient,
    PbsConnectionError,
    PbsError,
    PbsPermissionError,
)
from .const import (
    CONF_ALLOW_DESTRUCTIVE,
    CONF_ALLOW_WRITE,
    CONF_DATASTORES,
    CONF_GC_WARNING_DAYS,
    CONF_STALE_DAYS,
    CONF_TOKEN_ID,
    CONF_TOKEN_SECRET,
    CONF_USAGE_CRITICAL,
    CONF_USAGE_WARNING,
    DEFAULT_GC_WARNING_DAYS,
    DEFAULT_PORT,
    DEFAULT_STALE_DAYS,
    DEFAULT_USAGE_CRITICAL,
    DEFAULT_USAGE_WARNING,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    LOGGER,
)


class NoDatastoresError(Exception):
    """The token authenticated but sees no datastore at all."""


CREDENTIAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN_ID): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
        ),
        vol.Required(CONF_TOKEN_SECRET): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


def _connection_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Return the full connection form, prefilled with ``defaults``."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_TOKEN_ID, default=defaults.get(CONF_TOKEN_ID, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(CONF_TOKEN_SECRET): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_VERIFY_SSL,
                default=defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ): BooleanSelector(),
        }
    )


async def validate_connection(hass, data: Mapping[str, Any]) -> dict[str, Any]:
    """Talk to PBS and return unique id plus the visible datastores.

    Raises the client exceptions unchanged so the calling step can map them
    onto form errors.
    """
    session = async_get_clientsession(hass, verify_ssl=data[CONF_VERIFY_SSL])
    client = PbsClient(
        session,
        data[CONF_HOST],
        int(data[CONF_PORT]),
        data[CONF_TOKEN_ID],
        data[CONF_TOKEN_SECRET],
    )

    await client.ping()
    version = await client.get_version()
    datastores = await client.list_datastores()
    if not datastores:
        raise NoDatastoresError

    # Deliberately host:port and not the node fingerprint: the fingerprint is
    # only readable with Audit on /system, so a token without it would produce
    # a different unique id and the duplicate check would not catch a second
    # setup of the same server. An IP change is handled by the reconfigure flow.
    return {
        "unique_id": f"{data[CONF_HOST]}:{int(data[CONF_PORT])}",
        "datastores": datastores,
        "version": version.full,
    }


class PbsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup, re-authentication and reconfiguration."""

    VERSION = 1

    def __init__(self) -> None:
        """Prepare the intermediate state between the two steps."""
        self._data: dict[str, Any] = {}
        self._datastores: list[str] = []

    @staticmethod
    def async_get_options_flow(config_entry) -> PbsOptionsFlow:
        """Return the options flow."""
        return PbsOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect host and token, then verify them."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = {**user_input, CONF_PORT: int(user_input[CONF_PORT])}
            try:
                info = await validate_connection(self.hass, user_input)
            except PbsAuthError:
                errors["base"] = "invalid_auth"
            except PbsPermissionError:
                errors["base"] = "insufficient_permissions"
            except NoDatastoresError:
                errors["base"] = "no_datastores"
            except PbsConnectionError:
                errors["base"] = "cannot_connect"
            except PbsError:
                LOGGER.exception("Unexpected error while validating PBS")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured()
                self._data = user_input
                self._datastores = info["datastores"]
                return await self.async_step_datastores()

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_datastores(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick which datastores to monitor."""
        if len(self._datastores) == 1:
            return self._create(self._datastores)

        if user_input is not None:
            return self._create(user_input[CONF_DATASTORES])

        return self.async_show_form(
            step_id="datastores",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DATASTORES, default=self._datastores
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=self._datastores,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    def _create(self, datastores: list[str]) -> ConfigFlowResult:
        """Create the entry with sane default options."""
        return self.async_create_entry(
            title=f"PBS {self._data[CONF_HOST]}",
            data=self._data,
            options={
                CONF_DATASTORES: datastores,
                CONF_STALE_DAYS: DEFAULT_STALE_DAYS,
                CONF_USAGE_WARNING: DEFAULT_USAGE_WARNING,
                CONF_USAGE_CRITICAL: DEFAULT_USAGE_CRITICAL,
                CONF_GC_WARNING_DAYS: DEFAULT_GC_WARNING_DAYS,
                CONF_ALLOW_WRITE: False,
                CONF_ALLOW_DESTRUCTIVE: False,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the token was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh token and verify it against the same host."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            candidate = {**entry.data, **user_input}
            try:
                await validate_connection(self.hass, candidate)
            except PbsAuthError:
                errors["base"] = "invalid_auth"
            except PbsPermissionError:
                errors["base"] = "insufficient_permissions"
            except NoDatastoresError:
                errors["base"] = "no_datastores"
            except PbsConnectionError:
                errors["base"] = "cannot_connect"
            except PbsError:
                LOGGER.exception("Unexpected error while validating PBS")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates=dict(user_input)
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=CREDENTIAL_SCHEMA,
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host, port, token or TLS handling of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = {**user_input, CONF_PORT: int(user_input[CONF_PORT])}
            try:
                info = await validate_connection(self.hass, user_input)
            except PbsAuthError:
                errors["base"] = "invalid_auth"
            except PbsPermissionError:
                errors["base"] = "insufficient_permissions"
            except NoDatastoresError:
                errors["base"] = "no_datastores"
            except PbsConnectionError:
                errors["base"] = "cannot_connect"
            except PbsError:
                LOGGER.exception("Unexpected error while validating PBS")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_server")
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(user_input or entry.data),
            errors=errors,
        )


class PbsOptionsFlow(OptionsFlow):
    """Thresholds, datastore selection and the write permission switches."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        current = dict(entry.options)

        if user_input is not None:
            if user_input[CONF_ALLOW_DESTRUCTIVE] and not user_input[CONF_ALLOW_WRITE]:
                errors["base"] = "destructive_needs_write"
            elif user_input[CONF_USAGE_CRITICAL] <= user_input[CONF_USAGE_WARNING]:
                errors["base"] = "thresholds_out_of_order"
            else:
                return self.async_create_entry(
                    data={
                        **current,
                        **user_input,
                        CONF_STALE_DAYS: int(user_input[CONF_STALE_DAYS]),
                        CONF_USAGE_WARNING: int(user_input[CONF_USAGE_WARNING]),
                        CONF_USAGE_CRITICAL: int(user_input[CONF_USAGE_CRITICAL]),
                        CONF_GC_WARNING_DAYS: int(user_input[CONF_GC_WARNING_DAYS]),
                    }
                )
            current = {**current, **user_input}

        available = await self._async_available_datastores()
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DATASTORES,
                    default=current.get(CONF_DATASTORES, available),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=available,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_STALE_DAYS,
                    default=current.get(CONF_STALE_DAYS, DEFAULT_STALE_DAYS),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=365, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_USAGE_WARNING,
                    default=current.get(CONF_USAGE_WARNING, DEFAULT_USAGE_WARNING),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=99, mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(
                    CONF_USAGE_CRITICAL,
                    default=current.get(CONF_USAGE_CRITICAL, DEFAULT_USAGE_CRITICAL),
                ): NumberSelector(
                    NumberSelectorConfig(min=2, max=100, mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(
                    CONF_GC_WARNING_DAYS,
                    default=current.get(CONF_GC_WARNING_DAYS, DEFAULT_GC_WARNING_DAYS),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=90, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_ALLOW_WRITE,
                    default=current.get(CONF_ALLOW_WRITE, False),
                ): BooleanSelector(),
                vol.Required(
                    CONF_ALLOW_DESTRUCTIVE,
                    default=current.get(CONF_ALLOW_DESTRUCTIVE, False),
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def _async_available_datastores(self) -> list[str]:
        """Return the datastores PBS currently offers.

        Falls back to whatever is already selected when the entry is not loaded
        or the server cannot be reached, so the form still opens.
        """
        selected = list(self.config_entry.options.get(CONF_DATASTORES) or [])
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None:
            return selected
        try:
            available = await runtime.client.list_datastores()
        except PbsError as err:
            LOGGER.debug("Could not refresh the datastore list: %s", err)
            return selected
        return sorted(set(available) | set(selected))
