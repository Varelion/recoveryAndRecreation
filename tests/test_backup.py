import sqlite3
import stat
from datetime import datetime

import pytest

from fakes import GENERAL, GUILD, FakeLive
from vault.config import Media
from vault.services.backup import snapshot
from vault.services.exporter import ExportOptions, Exporter
from vault.store import Store

MORNING = datetime(2026, 11, 4, 9, 30)
EVENING = datetime(2026, 11, 4, 18, 5)
WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "t.db")
    await Exporter(FakeLive(), s, GUILD, ExportOptions(media=Media.SKIP)).run()
    yield s
    await s.close()


def _channels(path) -> set[int]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return {row[0] for row in db.execute("SELECT channel_id FROM channels WHERE guild_id = ?", (GUILD,))}


async def test_snapshot_is_dated_readonly_copy(store, tmp_path):
    path = await snapshot(store, GUILD, tmp_path / "out", MORNING)

    assert path == tmp_path / "out" / str(GUILD) / "2026-11-04-0930.db"
    assert GENERAL in _channels(path)
    assert path.stat().st_mode & WRITE_BITS == 0
    # A read-only file must open without WAL sidecars.
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


async def test_second_snapshot_is_new_file(store, tmp_path):
    first = await snapshot(store, GUILD, tmp_path, MORNING)
    second = await snapshot(store, GUILD, tmp_path, EVENING)
    same_minute = await snapshot(store, GUILD, tmp_path, EVENING)

    assert len({first, second, same_minute}) == 3
    assert all(GENERAL in _channels(p) for p in (first, second, same_minute))
