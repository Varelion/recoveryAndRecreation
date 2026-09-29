"""Attachment downloader. Files are stored once per content hash.

    media/ab/abcdef...0123.png   (first two hex chars shard the folder)
"""

import asyncio
import hashlib
import logging
from pathlib import Path

from vault.api import Denied, Missing, Rest
from vault.services.progress import Progress
from vault.store import Store

log = logging.getLogger(__name__)

_SHARD = 2


class Downloader:
    def __init__(self, rest: Rest, store: Store, root: Path, workers: int) -> None:
        self._rest = rest
        self._store = store
        self._root = root
        self._gate = asyncio.Semaphore(workers)

    async def run(self, guild_id: int, progress: Progress) -> int:
        """Download every attachment of the guild not yet on disk. Returns files saved."""
        pending = await self._store.pending_media(guild_id)
        log.info("Media: %d pending", len(pending))
        progress.plan(len(pending))

        async def one(row) -> int:
            saved = await self._one(row["attachment_id"], row["filename"], row["url"])
            progress.advance()
            return saved

        results = await asyncio.gather(*(one(r) for r in pending))
        await self._store.commit()
        return sum(results)

    async def _one(self, attachment_id: int, filename: str, url: str) -> int:
        async with self._gate:
            try:
                data = await self._rest.cdn(url)
            except (Missing, Denied):
                # Signed CDN links expire; a later export refreshes them.
                log.warning("Media gone: %s", url)
                return 0

        sha = hashlib.sha256(data).hexdigest()
        path = self._root / sha[:_SHARD] / (sha + Path(filename or "").suffix)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await self._store.put_media(attachment_id, sha, str(path), len(data))
        return 1
