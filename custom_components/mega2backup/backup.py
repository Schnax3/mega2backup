"""Backup platform for the Mega Backup integration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
import functools
import json
import logging
import time
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
    async_free_space,
    async_make_temp_dir,
    async_remove_dir,
)
from .const import (
    DATA_BACKUP_AGENT_LISTENERS,
    DOMAIN,
    DOWNLOAD_CHUNK_SIZE,
    METADATA_SUFFIX,
    STAGING_BUFFER_SIZE,
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


def _log_unexpected_errors[**P, R](
    action: str,
) -> Callable[
    [Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]
]:
    """Log unexpected errors with a traceback and report them as BackupAgentError."""

    def decorator(
        func: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await func(*args, **kwargs)
            except BackupAgentError:
                raise  # already logged where it was raised
            except asyncio.CancelledError:
                _LOGGER.error(
                    "Cancelled while %s: Home Assistant, the browser or a "
                    "timeout aborted the operation",
                    action,
                )
                raise
            except Exception as err:
                _LOGGER.exception("Unexpected error while %s: %s", action, err)
                raise BackupAgentError(
                    f"Unexpected error while {action}: {err}"
                ) from err

        return wrapper

    return decorator


def _format_duration(seconds: float) -> str:
    """Return a short human readable duration."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


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

    @_log_unexpected_errors("downloading a backup from Mega")
    async def async_download_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        """Download a backup file."""
        _LOGGER.info("Home Assistant requested the download of backup %s", backup_id)
        try:
            files = await self._client.async_list_files(force=True)
        except MegaError as err:
            _LOGGER.error("Listing backups in Mega failed: %s", err)
            raise BackupAgentError(f"Failed to list Mega backups: {err}") from err

        node = files.get(_tar_name(backup_id))
        if node is None:
            _LOGGER.warning("Backup %s not found in Mega folder", backup_id)
            raise BackupNotFound(f"Backup {backup_id} not found")

        _LOGGER.info(
            "Downloading backup %s from Mega (%.1f MiB), staging it on disk first",
            backup_id,
            (getattr(node, "size", None) or 0) / 2**20,
        )
        started = time.monotonic()

        # async-mega-py downloads to a file, so the backup is staged on disk
        # first and then streamed to Home Assistant.
        tmp_dir = await async_make_temp_dir(self._hass)
        try:
            path = await self._client.async_download(node, tmp_dir)
        except (MegaError, OSError) as err:
            await async_remove_dir(self._hass, tmp_dir)
            _LOGGER.error("Download of backup %s from Mega failed: %s", backup_id, err)
            raise BackupAgentError(f"Failed to download backup: {err}") from err
        except BaseException:
            await async_remove_dir(self._hass, tmp_dir)
            raise

        _LOGGER.info(
            "Backup %s downloaded from Mega in %s, handing it to Home Assistant",
            backup_id,
            _format_duration(time.monotonic() - started),
        )

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
            except OSError as err:
                _LOGGER.error("Reading downloaded backup %s failed: %s", backup_id, err)
                raise
            finally:
                await async_remove_dir(hass, tmp_dir)

        return _stream()

    @_log_unexpected_errors("uploading a backup to Mega")
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
        try:
            free = await async_free_space(hass)
        except OSError as err:
            raise BackupAgentError(f"Cannot access staging directory: {err}") from err
        if free < backup.size * 1.05:
            _LOGGER.error(
                "Not enough free disk space to stage backup %s: %d MiB free, "
                "%d MiB needed",
                backup.backup_id,
                free // 2**20,
                backup.size // 2**20,
            )
            raise BackupAgentError(
                f"Not enough free disk space to stage the backup: "
                f"{free // 2**20} MiB free, {backup.size // 2**20} MiB needed"
            )

        tmp_dir = await async_make_temp_dir(hass)
        tar_path = tmp_dir / _tar_name(backup.backup_id)
        metadata_path = tmp_dir / _metadata_name(backup.backup_id)
        uploaded_nodes = []
        started = time.monotonic()
        next_log_percent = 10

        _LOGGER.info(
            "Starting upload of backup '%s' (%s, %.1f MiB) to Mega folder '%s'",
            backup.name,
            backup.backup_id,
            backup.size / 2**20,
            self._client.folder,
        )

        def _report(bytes_uploaded: int) -> None:
            nonlocal next_log_percent
            if on_progress is not None:
                on_progress(bytes_uploaded=bytes_uploaded)
            if backup.size > 0:
                percent = bytes_uploaded * 100 // backup.size
                while percent >= next_log_percent <= 90:
                    _LOGGER.info(
                        "Uploading backup %s to Mega: %d%%",
                        backup.backup_id,
                        next_log_percent,
                    )
                    next_log_percent += 10

        try:
            # Stage the stream on disk, async-mega-py uploads from files.
            stream = await open_stream()
            handle = await hass.async_add_executor_job(tar_path.open, "wb")
            staged = 0
            next_stage_percent = 10
            buffer = bytearray()
            try:
                async for chunk in stream:
                    buffer += chunk
                    if len(buffer) < STAGING_BUFFER_SIZE:
                        continue
                    data = bytes(buffer)
                    buffer.clear()
                    await hass.async_add_executor_job(handle.write, data)
                    staged += len(data)
                    if backup.size > 0:
                        while staged * 100 // backup.size >= next_stage_percent:
                            _LOGGER.info(
                                "Staging backup %s on disk: %d%% (%d of %d MiB)",
                                backup.backup_id,
                                min(next_stage_percent, 100),
                                staged // 2**20,
                                backup.size // 2**20,
                            )
                            next_stage_percent += 10
                if buffer:
                    await hass.async_add_executor_job(handle.write, bytes(buffer))
                    staged += len(buffer)
            finally:
                await hass.async_add_executor_job(handle.close)

            _LOGGER.info(
                "Backup %s staged on disk in %s, uploading to Mega",
                backup.backup_id,
                _format_duration(time.monotonic() - started),
            )

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
                _LOGGER.info(
                    "Replacing existing backup %s in Mega folder", backup.backup_id
                )
                await self._client.async_delete(*existing)

            uploaded_nodes.append(await self._client.async_upload(tar_path, _report))
            uploaded_nodes.append(await self._client.async_upload(metadata_path))
            _LOGGER.info(
                "Backup %s uploaded to Mega in %s",
                backup.backup_id,
                _format_duration(time.monotonic() - started),
            )
        except MegaError as err:
            _LOGGER.error(
                "Upload of backup %s to Mega failed: %s", backup.backup_id, err
            )
            if uploaded_nodes:
                try:
                    await self._client.async_delete(*uploaded_nodes)
                except MegaError:
                    _LOGGER.warning("Could not clean up incomplete upload")
            raise BackupAgentError(f"Failed to upload backup: {err}") from err
        except OSError as err:
            _LOGGER.error(
                "Could not stage backup %s on disk (disk full?): %s",
                backup.backup_id,
                err,
            )
            raise BackupAgentError(f"Failed to stage backup on disk: {err}") from err
        finally:
            await async_remove_dir(hass, tmp_dir)

    @_log_unexpected_errors("deleting a backup from Mega")
    async def async_delete_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> None:
        """Delete a backup file."""
        _LOGGER.debug("Home Assistant asked to delete backup %s", backup_id)
        try:
            files = await self._client.async_list_files(force=True)
            nodes = [
                node
                for name in (_tar_name(backup_id), _metadata_name(backup_id))
                if (node := files.get(name)) is not None
            ]
            if not nodes:
                # Home Assistant asks every agent when it deletes a backup, so
                # this is normal for backups that are not stored in Mega.
                _LOGGER.debug("Backup %s is not in Mega, nothing to delete", backup_id)
                raise BackupNotFound(f"Backup {backup_id} not found")
            await self._client.async_delete(*nodes)
            _LOGGER.info("Deleted backup %s from Mega", backup_id)
        except MegaError as err:
            _LOGGER.error("Deleting backup %s from Mega failed: %s", backup_id, err)
            raise BackupAgentError(f"Failed to delete backup: {err}") from err

    @_log_unexpected_errors("listing backups in Mega")
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
            _LOGGER.error("Listing backups in Mega failed: %s", err)
            raise BackupAgentError(f"Failed to list Mega backups: {err}") from err
        _LOGGER.debug("Found %d backup(s) in Mega folder", len(backups))
        return backups

    @_log_unexpected_errors("reading a backup from Mega")
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
                _LOGGER.debug("Backup %s is not in Mega", backup_id)
                raise BackupNotFound(f"Backup {backup_id} not found")
            data = await self._client.async_read_json(node)
            return AgentBackup.from_dict(data)
        except MegaError as err:
            _LOGGER.error("Reading backup %s from Mega failed: %s", backup_id, err)
            raise BackupAgentError(f"Failed to get backup: {err}") from err
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.error("Invalid metadata file for backup %s: %r", backup_id, err)
            raise BackupAgentError(f"Invalid metadata for {backup_id}: {err}") from err
