"""Point-in-time, read-only copies of the database.

    <out>/<guild>/2026-11-04-0930.db      first backup that minute
    <out>/<guild>/2026-11-04-0930-2.db    another one the same minute
"""

import stat
from datetime import datetime
from pathlib import Path

from vault.store import Store

STAMP = "%Y-%m-%d-%H%M"
SUFFIX = ".db"
READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


async def snapshot(store: Store, guild_id: int, out: Path, at: datetime | None = None) -> Path:
    """Copy the database to a dated file under `out/<guild>` and make it read-only."""
    folder = out / str(guild_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = _free(folder, (at or datetime.now()).strftime(STAMP))

    await store.snapshot(path)
    path.chmod(READ_ONLY)
    return path


def _free(folder: Path, stamp: str) -> Path:
    # Never overwrite an earlier backup: number later ones of the same minute.
    path = folder / f"{stamp}{SUFFIX}"
    copy = 1
    while path.exists():
        copy += 1
        path = folder / f"{stamp}-{copy}{SUFFIX}"

    return path
