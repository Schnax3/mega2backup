"""Thin async wrapper around async-mega-py used to store Home Assistant backups."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
import contextlib
import json
import logging
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, TypeVar

import aiohttp
from mega.client import MegaNzClient
from mega.data_structures import Node
from mega.errors import MegaNzError, RequestError, ValidationError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import INVALID_FOLDER_CHARS, LISTING_CACHE_TTL

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Mega API error codes (see mega.errors)
_ERR_NOT_FOUND = -9
_ERR_BLOCKED = -16
_ERR_SESSION = -15
_ERR_MFA_REQUIRED = -26

ProgressCallback = Callable[[int], None]


class MegaError(Exception):
    """Base error of the Mega backup client."""


class MegaAuthError(MegaError):
    """Login failed (wrong credentials or blocked account)."""


class MegaMfaRequiredError(MegaAuthError):
    """The account requires multi-factor authentication."""


class MegaSessionExpiredError(MegaError):
    """The Mega session is no longer valid."""


class MegaConnectionError(MegaError):
    """Mega could not be reached."""


class MegaApiError(MegaError):
    """Mega returned an error."""


class MegaInvalidFolderError(MegaError):
    """The configured folder name is not usable."""


def validate_folder_name(folder: str) -> str:
    """Return the cleaned folder name or raise MegaInvalidFolderError."""
    folder = folder.strip()
    if not folder or any(char in folder for char in INVALID_FOLDER_CHARS):
        raise MegaInvalidFolderError(folder)
    return folder


@contextlib.contextmanager
def _translate_errors(*, login: bool = False) -> Generator[None]:
    """Translate library / network errors into MegaError subclasses."""
    try:
        yield
    except RequestError as err:
        code = err.code
        if login and code == _ERR_MFA_REQUIRED:
            raise MegaMfaRequiredError(str(err)) from err
        if login and code in (_ERR_NOT_FOUND, _ERR_BLOCKED):
            raise MegaAuthError(str(err)) from err
        if code == _ERR_SESSION:
            raise MegaSessionExpiredError(str(err)) from err
        raise MegaApiError(str(err)) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise MegaConnectionError(str(err) or type(err).__name__) from err
    except (MegaNzError, ValidationError, LookupError, RuntimeError) as err:
        raise MegaApiError(str(err) or type(err).__name__) from err


@contextlib.contextmanager
def _upload_progress(callback: ProgressCallback | None) -> Generator[None]:
    """Route async-mega-py upload progress to *callback* (best effort).

    The library has no public progress callback, but exposes a context variable
    holding a hook factory. If that internal API ever changes, uploads simply
    continue without progress reporting.
    """
    if callback is None:
        yield
        return

    try:
        from mega import progress as mega_progress  # noqa: PLC0415

        factory_var = mega_progress._PROGRESS_HOOK_FACTORY
    except (ImportError, AttributeError):
        yield
        return

    sent = 0

    @contextlib.contextmanager
    def factory(description: str, total: float, kind: str) -> Generator[Any]:
        def hook(advance: float) -> None:
            nonlocal sent
            if kind == "UP":
                sent += int(advance)
                callback(sent)

        yield hook

    token = factory_var.set(factory)
    try:
        yield
    finally:
        factory_var.reset(token)


class MegaBackupClient:
    """Stores and retrieves backup files in one folder of a Mega account."""

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
        folder: str,
    ) -> None:
        """Initialize the client (no network access yet)."""
        self._hass = hass
        self._email = email
        self._password = password
        self._folder = validate_folder_name(folder)
        # Transfers can take a long time, so no total timeout.
        self._session = async_create_clientsession(
            hass,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120),
        )
        self._client: MegaNzClient | None = None
        self._lock = asyncio.Lock()
        self._files_cache: tuple[float, dict[str, Node]] | None = None
        self._json_cache: dict[str, dict[str, Any]] = {}

    @property
    def email(self) -> str:
        """Return the account e-mail address."""
        return self._email

    @property
    def folder(self) -> str:
        """Return the configured backup folder name."""
        return self._folder

    # ------------------------------------------------------------------ session

    async def async_connect(self) -> None:
        """Log in and make sure the backup folder exists."""
        async with self._lock:
            await self._async_login()
        await self._call(self._async_ensure_folder)

    async def async_close(self) -> None:
        """Close the HTTP session."""
        self._client = None
        await self._session.close()

    async def _async_login(self) -> None:
        client = MegaNzClient(self._session)
        with _translate_errors(login=True):
            await client.login(self._email, self._password)
        self._client = client
        self._files_cache = None

    async def _call(self, func: Callable[[MegaNzClient], Awaitable[_T]]) -> _T:
        """Run *func* with a logged-in client, re-logging in once if needed."""
        for attempt in (1, 2):
            async with self._lock:
                if self._client is None:
                    await self._async_login()
                client = self._client
            assert client is not None
            try:
                with _translate_errors():
                    return await func(client)
            except MegaSessionExpiredError as err:
                if attempt == 2:
                    raise MegaAuthError(str(err)) from err
                _LOGGER.info("Mega session expired, logging in again")
                async with self._lock:
                    self._client = None
        raise MegaApiError("unreachable")  # pragma: no cover

    async def _async_ensure_folder(self, client: MegaNzClient) -> Node:
        fs = await client.get_filesystem(force=True)
        try:
            node = fs.find(self._folder)
        except FileNotFoundError:
            _LOGGER.info("Creating Mega backup folder '%s'", self._folder)
            return await client.create_folder(self._folder)
        if not node.is_folder:
            raise MegaApiError(f"'{self._folder}' exists but is not a folder")
        return node

    # ------------------------------------------------------------------ listing

    async def async_list_files(self, *, force: bool = False) -> dict[str, Node]:
        """Return the files in the backup folder, keyed by file name."""
        cached = self._files_cache
        if (
            not force
            and cached is not None
            and time.monotonic() - cached[0] < LISTING_CACHE_TTL
        ):
            return cached[1]

        async def _list(client: MegaNzClient) -> dict[str, Node]:
            folder = await self._async_ensure_folder(client)
            fs = await client.get_filesystem()
            return {
                node.attributes.name: node
                for node in fs.iterdir(folder.id)
                if node.is_file
            }

        files = await self._call(_list)
        self._files_cache = (time.monotonic(), files)
        return files

    def invalidate_cache(self) -> None:
        """Forget the cached folder listing."""
        self._files_cache = None

    # ---------------------------------------------------------------- transfers

    async def async_download(self, node: Node, target_dir: Path) -> Path:
        """Download *node* into *target_dir* and return the local path."""

        async def _download(client: MegaNzClient) -> Path:
            return await client.download(node, target_dir)

        return await self._call(_download)

    async def async_read_json(self, node: Node) -> dict[str, Any]:
        """Download a small JSON file and return its parsed content."""
        if (cached := self._json_cache.get(node.id)) is not None:
            return cached

        tmp_dir = await async_make_temp_dir(self._hass)
        try:
            path = await self.async_download(node, tmp_dir)
            text = await self._hass.async_add_executor_job(path.read_text, "utf-8")
        finally:
            await async_remove_dir(self._hass, tmp_dir)

        try:
            data = json.loads(text)
        except ValueError as err:
            raise MegaApiError(f"'{node.attributes.name}' is not valid JSON") from err
        if not isinstance(data, dict):
            raise MegaApiError(f"'{node.attributes.name}' has an unexpected format")
        self._json_cache[node.id] = data
        return data

    async def async_upload(
        self,
        path: Path,
        on_progress: ProgressCallback | None = None,
    ) -> Node:
        """Upload the local file *path* into the backup folder."""

        async def _upload(client: MegaNzClient) -> Node:
            folder = await self._async_ensure_folder(client)
            with _upload_progress(on_progress):
                return await client.upload(path, folder.id)

        try:
            return await self._call(_upload)
        finally:
            self.invalidate_cache()

    async def async_delete(self, *nodes: Node) -> None:
        """Permanently delete *nodes* (bypassing the Mega rubbish bin)."""

        async def _delete(client: MegaNzClient) -> None:
            for node in nodes:
                await client.destroy(node.id)

        try:
            await self._call(_delete)
        finally:
            self.invalidate_cache()
            for node in nodes:
                self._json_cache.pop(node.id, None)


def _temp_base(hass: HomeAssistant) -> Path:
    """Return the staging directory.

    It lives inside the Home Assistant config directory (the data partition)
    because /tmp is often a small RAM disk, which is too small for backups.
    """
    return Path(hass.config.path("mega2backup_tmp"))


def _make_temp_dir(base: Path) -> str:
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="job_", dir=base)


def _remove_dir(path: Path | str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _free_space(base: Path) -> int:
    base.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(base).free


async def async_make_temp_dir(hass: HomeAssistant) -> Path:
    """Create a temporary working directory."""
    base = _temp_base(hass)
    return Path(await hass.async_add_executor_job(_make_temp_dir, base))


async def async_remove_dir(hass: HomeAssistant, path: Path) -> None:
    """Remove a temporary working directory."""
    await hass.async_add_executor_job(_remove_dir, path)


async def async_cleanup_temp(hass: HomeAssistant) -> None:
    """Remove leftovers of interrupted transfers (call when no transfer runs)."""
    await hass.async_add_executor_job(_remove_dir, _temp_base(hass))


async def async_free_space(hass: HomeAssistant) -> int:
    """Return the free bytes on the staging partition."""
    return await hass.async_add_executor_job(_free_space, _temp_base(hass))
