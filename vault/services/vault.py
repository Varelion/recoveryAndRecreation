"""Service facade used by every UI (CLI, Discord bot).

Starts actions as tracked jobs; UIs never build services themselves.
"""

from pathlib import Path

from vault.api import Json, Link
from vault.services import backup, builder, exporter, importer, restore
from vault.services.blueprint import Blueprint
from vault.services.builder import BuildOptions, Builder
from vault.services.exporter import ExportOptions, Exporter
from vault.services.importer import ImportOptions, Importer
from vault.services.jobs import Job, Jobs
from vault.services.progress import Progress
from vault.services.restore import RestoreOptions
from vault.services.watcher import Watcher
from vault.store import Store

EXPORT, IMPORT, WATCH, BUILD = "export", "import", "watch", "build"
RESTORE = "restore"


class Vault:
    def __init__(self, link: Link, store: Store) -> None:
        self._link = link
        self._store = store
        self.jobs = Jobs()

    def export(self, guild_id: int, options: ExportOptions, by: int | None = None) -> Job:
        progress = Progress(EXPORT, exporter.PHASES)
        work = Exporter(self._link.rest, self._store, guild_id, options, progress).run()
        return self.jobs.start(EXPORT, guild_id, by, progress, work)

    def import_guild(self, source: int, target: int, options: ImportOptions, by: int | None = None) -> Job:
        progress = Progress(IMPORT, importer.PHASES)
        work = Importer(self._link.rest, self._store, source, target, options, progress).run()
        return self.jobs.start(IMPORT, target, by, progress, work)

    def build(self, guild_id: int, blueprint: Blueprint, options: BuildOptions, by: int | None = None) -> Job:
        progress = Progress(BUILD, builder.PHASES)
        work = Builder(self._link.rest, self._store, guild_id, blueprint, options, progress).run()
        return self.jobs.start(BUILD, guild_id, by, progress, work)

    def restore(self, backup_db: Path, guild_id: int, options: RestoreOptions, by: int | None = None) -> Job:
        progress = Progress(RESTORE, importer.PHASES)
        work = restore.restore(self._link.rest, backup_db, guild_id, options, progress)
        return self.jobs.start(RESTORE, guild_id, by, progress, work)

    async def backup(self, guild_id: int, out: Path) -> Path:
        """Dated read-only copy of the database; run after an export."""
        return await backup.snapshot(self._store, guild_id, out)

    async def watch(self, guild_id: int, by: int | None = None) -> Job:
        """Record live events."""
        progress = Progress(WATCH)
        return self.jobs.start(WATCH, guild_id, by, progress, Watcher(self._link, self._store, guild_id, progress).run())

    async def known(self, guild_id: int) -> bool:
        return await self._store.raw("guild", guild_id) is not None

    async def stats(self, guild_id: int | None) -> Json:
        return await self._store.counts(guild_id)
