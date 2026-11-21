"""Config flow for the Mega Backup integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    MegaAuthError,
    MegaBackupClient,
    MegaConnectionError,
    MegaError,
    MegaInvalidFolderError,
    MegaMfaRequiredError,
    validate_folder_name,
)
from .const import CONF_EMAIL, CONF_FOLDER, CONF_PASSWORD, DEFAULT_FOLDER, DOMAIN

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_EMAIL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL))


async def _async_validate(
    hass: HomeAssistant, email: str, password: str, folder: str
) -> str | None:
    """Try to log in and prepare the folder. Return an error key or None."""
    try:
        client = MegaBackupClient(hass, email, password, folder)
    except MegaInvalidFolderError:
        return "invalid_folder"

    try:
        await client.async_connect()
    except MegaMfaRequiredError:
        return "mfa_required"
    except MegaAuthError:
        return "invalid_auth"
    except MegaConnectionError:
        return "cannot_connect"
    except MegaError as err:
        _LOGGER.warning("Mega returned an error during setup: %s", err)
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error while validating Mega credentials")
        return "unknown"
    finally:
        await client.async_close()
    return None


class MegaBackupConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Mega Backup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            password = user_input[CONF_PASSWORD]
            try:
                folder = validate_folder_name(user_input[CONF_FOLDER])
            except MegaInvalidFolderError:
                errors[CONF_FOLDER] = "invalid_folder"
            else:
                await self.async_set_unique_id(email)
                self._abort_if_unique_id_configured()

                if error := await _async_validate(self.hass, email, password, folder):
                    errors["base"] = error
                else:
                    return self.async_create_entry(
                        title=email,
                        data={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: password,
                            CONF_FOLDER: folder,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_EMAIL): _EMAIL_SELECTOR,
                        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                        vol.Required(CONF_FOLDER, default=DEFAULT_FOLDER): str,
                    }
                ),
                user_input,
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start the re-authentication flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            error = await _async_validate(
                self.hass,
                entry.data[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                entry.data.get(CONF_FOLDER, DEFAULT_FOLDER),
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}),
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
            errors=errors,
        )
