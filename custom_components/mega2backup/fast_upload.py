"""Faster uploads to Mega.

async-mega-py uploads a file strictly one chunk after the other and encrypts
every chunk inside the event loop. For multi-gigabyte backups on a small
device that is slow: the CPU work and the network round trips add up, and only
a single connection is used.

This module uploads the same chunks with the same encryption, but

* reads and encrypts the next chunks in a worker thread while earlier chunks
  are still being sent, and
* sends several chunks at the same time over parallel connections (Mega's
  upload servers accept chunks in any order, each chunk carries its offset).

It relies on a few internals of async-mega-py (the version is pinned in
manifest.json). If any of them is missing, or the upload server does not hand
out the completion handle, ``FastUploadUnavailable`` is raised and the caller
falls back to the regular upload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
import re
import time
from typing import Any

import aiohttp

from .const import CHUNK_RETRIES, UPLOAD_CONCURRENCY
from .logs import TransferLogger, memory_summary

_LOGGER = logging.getLogger(__name__)

_ERROR_CODE_RE = re.compile(r"^-\d+$")


class FastUploadUnavailable(Exception):
    """The fast upload cannot be used, use the regular one."""


async def _post_chunk(api: Any, url: str, offset: int, data: bytes) -> str:
    """Send one chunk, retrying transient network errors."""
    for attempt in range(1, CHUNK_RETRIES + 1):
        try:
            started = time.monotonic()
            result = (await api.upload_chunk(url, offset, data)).strip()
            _LOGGER.debug(
                "Chunk at offset %d (%d KiB) sent in %.2f s, response %s",
                offset,
                len(data) // 1024,
                time.monotonic() - started,
                result[:12] or "(empty)",
            )
            return result
        except (aiohttp.ClientError, TimeoutError) as err:
            if attempt == CHUNK_RETRIES:
                raise
            _LOGGER.warning(
                "Uploading the chunk at offset %d failed (%s), retry %d/%d",
                offset,
                err or type(err).__name__,
                attempt,
                CHUNK_RETRIES - 1,
            )
            await asyncio.sleep(2 * attempt)
    raise RuntimeError("unreachable")  # pragma: no cover


async def async_fast_upload(
    client: Any,
    path: Path,
    dest_node_id: str,
    on_progress: Callable[[int], None] | None = None,
    concurrency: int = UPLOAD_CONCURRENCY,
) -> Any:
    """Upload *path* into the folder *dest_node_id* and return the new node."""
    try:
        from mega import upload as mega_upload  # noqa: PLC0415
        from mega.chunker import MegaChunker, get_chunks  # noqa: PLC0415
        from mega.data_structures import Crypto  # noqa: PLC0415
        from mega.errors import InvalidNameError  # noqa: PLC0415
        from mega.utils import random_u32int_array  # noqa: PLC0415

        core = client._core
        api = core.api
        master_key = core.vault.master_key
        clear_cache = core.clear_cache
        deserialize_node = core._deserialize_node
        request_upload_url = mega_upload._request_upload_url
        finish_upload = mega_upload.finish_upload
    except (ImportError, AttributeError) as err:
        raise FastUploadUnavailable(f"internal API changed: {err!r}") from err

    InvalidNameError.check(path.name)
    file_size = await asyncio.to_thread(lambda: path.stat().st_size)
    if file_size == 0:
        raise FastUploadUnavailable("empty file")

    _LOGGER.debug(
        "Fast upload of %s: %d bytes, concurrency %d, %s",
        path.name,
        file_size,
        concurrency,
        memory_summary(),
    )
    xfer = TransferLogger(_LOGGER, f"Mega upload {path.name}", file_size)
    user_progress = on_progress

    def on_progress(done: int) -> None:
        xfer.update(done)
        if user_progress is not None:
            user_progress(done)

    random_array = random_u32int_array(6)
    chunker = MegaChunker(random_array[:4], random_array[4:])
    upload_url = await request_upload_url(api, file_size)
    _LOGGER.debug("Received upload URL (host %s)", upload_url.split("/")[2])

    if concurrency <= 1:
        sent = 0
        responses: list[str] = []
        handle = await asyncio.to_thread(path.open, "rb")

        def read_and_encrypt(size: int) -> bytes:
            return chunker.read(handle.read(size))

        try:
            for offset, size in get_chunks(file_size):
                data = await asyncio.to_thread(read_and_encrypt, size)
                response = await _post_chunk(api, upload_url, offset, data)
                if response:
                    if _ERROR_CODE_RE.match(response):
                        raise RuntimeError(
                            f"Mega rejected the chunk at offset {offset} "
                            f"(error code {response})"
                        )
                    responses.append(response)
                sent += len(data)
                if on_progress is not None:
                    on_progress(sent)
        finally:
            handle.close()
    else:
        sent = 0
        responses = []
        await _upload_parallel(
            path,
            chunker,
            upload_url,
            file_size,
            api,
            concurrency,
            on_progress,
            responses,
            get_chunks,
        )

    if not responses:
        raise FastUploadUnavailable("the upload server returned no completion handle")
    if len(responses) > 1:
        _LOGGER.debug("Upload returned %d non-empty responses", len(responses))
    file_handle = responses[-1]

    _LOGGER.debug("All chunks sent, finishing the upload (%s)", memory_summary())
    crypto = Crypto.compose(chunker.key, chunker.iv, chunker.compute_meta_mac())
    clear_cache()
    response = await finish_upload(
        api,
        master_key,
        file_handle,
        path,
        dest_node_id,
        crypto.full_key,
        crypto.key,
    )
    return deserialize_node(response["f"][0])


async def _upload_parallel(
    path: Path,
    chunker: Any,
    upload_url: str,
    file_size: int,
    api: Any,
    concurrency: int,
    on_progress: Callable[[int], None] | None,
    responses: list[str],
    get_chunks: Callable[[int], Any],
) -> None:
    """Upload with a bounded in-memory queue for configurable concurrency."""
    queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(
        maxsize=concurrency * 2
    )
    sent = 0

    def read_and_encrypt(handle: Any, size: int) -> bytes:
        return chunker.read(handle.read(size))

    async def producer() -> None:
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            for offset, size in get_chunks(file_size):
                data = await asyncio.to_thread(read_and_encrypt, handle, size)
                await queue.put((offset, data))
        finally:
            handle.close()
        for _ in range(concurrency):
            await queue.put(None)

    async def worker() -> None:
        nonlocal sent
        while (item := await queue.get()) is not None:
            offset, data = item
            response = await _post_chunk(api, upload_url, offset, data)
            if response:
                if _ERROR_CODE_RE.match(response):
                    raise RuntimeError(
                        f"Mega rejected the chunk at offset {offset} "
                        f"(error code {response})"
                    )
                responses.append(response)
            sent += len(data)
            if on_progress is not None:
                on_progress(sent)

    _LOGGER.debug(
        "Uploading %s (%d bytes) with %d parallel connections",
        path.name,
        file_size,
        concurrency,
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(producer())
            for _ in range(concurrency):
                group.create_task(worker())
    except BaseExceptionGroup as err:
        # Report the first real error instead of the group wrapper.
        raise err.exceptions[0] from None
