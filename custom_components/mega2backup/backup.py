"""Backup platform for the Mega Backup integration."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
import json
import logging
from typing import Any

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
)
from homeassistant.core import HomeAssistant, callback

from . import MegaConfigEntry
from .api import (
    MegaError,
    async_make_temp_dir,
    async_remove_dir,
)
from .const import (
    DATA_BACKUP_AGENT_LISTENERS,
    DOMAIN,
    DOWNLOAD_CHUNK_SIZE,
    METADATA_SUFFIX,
    TAR_SUFFIX,
)

try:  # Added to Home Assistant in 2026.3
    from homeassistant.components.backup import OnProgressCallback
except ImportError:  # pragma: no cover
    OnProgressCallback = Callable[..., None]  # type: ignore[misc,assignment]

_LOGGER = logging.getLogger(__name__)


async def async_get_backup_agents(
    hass: HomeAssistant,
    **kwargs: Any,
) -> list[BackupAgent]:
    """Return a backup agent for every loaded config entry."""
    entries: list[MegaConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    return [MegaBackupAgent(hass, entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Register a listener that is called when the agents need to be reloaded."""
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        """Remove the listener."""
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


def _tar_name(backup_id: str) -> str:
    return f"{backup_id}{TAR_SUFFIX}"


def _metadata_name(backup_id: str) -> str:
    return f"{backup_id}{METADATA_SUFFIX}"


class MegaBackupAgent(BackupAgent):
    """Backup agent that stores backups in a folder of a Mega account.

    Every backup consists of two files: ``<id>.tar`` (the backup itself) and
    ``<id>.metadata.json`` (the Home Assistant backup metadata). The metadata
    file is uploaded last, so only complete backups are listed.
    """

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: MegaConfigEntry) -> None:
        """Initialize the agent."""
        super().__init__()
        self._hass = hass
        self._client = entry.runtime_data
        self.name = entry.title
        self.unique_id = entry.entry_id

    async def async_download_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        """Download a backup file."""
        try:
            files = await self._client.async_list_files(force=True)
        except MegaError as err:
            raise BackupAgentError(f"Failed to list Mega backups: {err}") from err

        node = files.get(_tar_name(backup_id))
        if node is None:
            raise BackupNotFound(f"Backup {backup_id} not found")

        # async-mega-py downloads to a file, so the backup is staged on disk
        # first and then streamed to Home Assistant.
        tmp_dir = await async_make_temp_dir(self._hass)
        try:
            path = await self._client.async_download(node, tmp_dir)
        except MegaError as err:
            await async_remove_dir(self._hass, tmp_dir)
            raise BackupAgentError(f"Failed to download backup: {err}") from err
        except BaseException:
            await async_remove_dir(self._hass, tmp_dir)
            raise

        hass = self._hass

        async def _stream() -> AsyncIterator[bytes]:
            try:
                handle = await hass.async_add_executor_job(path.open, "rb")
                try:
                    while chunk := await hass.async_add_executor_job(
                        handle.read, DOWNLOAD_CHUNK_SIZE
                    ):
                        yield chunk
                finally:
                    await hass.async_add_executor_job(handle.close)
            finally:
                await async_remove_dir(hass, tmp_dir)

        return _stream()

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback | None = None,
        **kwargs: Any,
    ) -> None:
        """Upload a backup."""
        hass = self._hass
        tmp_dir = await async_make_temp_dir(hass)
        tar_path = tmp_dir / _tar_name(backup.backup_id)
        metadata_path = tmp_dir / _metadata_name(backup.backup_id)
        uploaded_nodes = []

        def _report(bytes_uploaded: int) -> None:
            if on_progress is not None:
                on_progress(bytes_uploaded=bytes_uploaded)

        try:
            # Stage the stream on disk, async-mega-py uploads from files.
            stream = await open_stream()
            handle = await hass.async_add_executor_job(tar_path.open, "wb")
            try:
                async for chunk in stream:
                    await hass.async_add_executor_job(handle.write, chunk)
            finally:
                await hass.async_add_executor_job(handle.close)

            await hass.async_add_executor_job(
                metadata_path.write_text,
                json.dumps(backup.as_dict()),
                "utf-8",
            )

            # Replace an existing backup with the same ID.
            files = await self._client.async_list_files(force=True)
            existing = [
                node
                for name in (
                    _tar_name(backup.backup_id),
                    _metadata_name(backup.backup_id),
                )
                if (node := files.get(name)) is not None
            ]
            if existing:
                await self._client.async_delete(*existing)

            uploaded_nodes.append(await self._client.async_upload(tar_path, _report))
            uploaded_nodes.append(await self._client.async_upload(metadata_path))
        except MegaError as err:
            if uploaded_nodes:
                try:
                    await self._client.async_delete(*uploaded_nodes)
                except MegaError:
                    _LOGGER.warning("Could not clean up incomplete upload")
            raise BackupAgentError(f"Failed to upload backup: {err}") from err
        finally:
            await async_remove_dir(hass, tmp_dir)

    async def async_delete_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> None:
        """Delete a backup file."""
        try:
            files = await self._client.async_list_files(force=True)
            nodes = [
                node
                for name in (_tar_name(backup_id), _metadata_name(backup_id))
                if (node := files.get(name)) is not None
            ]
            if not nodes:
                raise BackupNotFound(f"Backup {backup_id} not found")
            await self._client.async_delete(*nodes)
        except MegaError as err:
            raise BackupAgentError(f"Failed to delete backup: {err}") from err

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """List backups."""
        try:
            files = await self._client.async_list_files()
            backups: list[AgentBackup] = []
            for name, node in files.items():
                if not name.endswith(METADATA_SUFFIX):
                    continue
                backup_id = name.removesuffix(METADATA_SUFFIX)
                if _tar_name(backup_id) not in files:
                    continue  # Orphaned metadata
                try:
                    data = await self._client.async_read_json(node)
                    backups.append(AgentBackup.from_dict(data))
                except (KeyError, TypeError, ValueError) as err:
                    _LOGGER.warning("Ignoring invalid metadata file %s: %s", name, err)
        except MegaError as err:
            raise BackupAgentError(f"Failed to list Mega backups: {err}") from err
        return backups

    async def async_get_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AgentBackup:
        """Return a backup."""
        try:
            files = await self._client.async_list_files()
            node = files.get(_metadata_name(backup_id))
            if node is None or _tar_name(backup_id) not in files:
                # The cache may be stale, ask Mega once more.
                files = await self._client.async_list_files(force=True)
                node = files.get(_metadata_name(backup_id))
            if node is None or _tar_name(backup_id) not in files:
                raise BackupNotFound(f"Backup {backup_id} not found")
            data = await self._client.async_read_json(node)
            return AgentBackup.from_dict(data)
        except MegaError as err:
            raise BackupAgentError(f"Failed to get backup: {err}") from err
        except (KeyError, TypeError, ValueError) as err:
            raise BackupAgentError(f"Invalid metadata for {backup_id}: {err}") from err
