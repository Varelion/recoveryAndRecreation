"""Service facade used by every UI (CLI, Discord bot).

Starts actions as tracked jobs; UIs never build services themselves.
"""

from vault.api import Json, Link
from vault.services import exporter, importer
from vault.services.exporter import ExportOptions, Exporter
from vault.services.importer import ImportOptions, Importer
from vault.services.jobs import Job, Jobs
from vault.services.progress import Progress
from vault.services.watcher import Watcher
from vault.store import Store

EXPORT, IMPORT, WATCH = "export", "import", "watch"
WATCH_FLAG = "watch"  # cursor scope remembering active watches across restarts


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

    async def watch(self, guild_id: int, by: int | None = None) -> Job:
        """Record live events; remembered so a restarted bot resumes it."""
        progress = Progress(WATCH)
        job = self.jobs.start(WATCH, guild_id, by, progress, Watcher(self._link, self._store, guild_id, progress).run())
        await self._store.set_cursor(WATCH_FLAG, guild_id, 1)
        await self._store.commit()
        return job

    async def unwatch(self, guild_id: int) -> bool:
        await self._store.set_cursor(WATCH_FLAG, guild_id, 0)
        await self._store.commit()
        job = self.jobs.running(WATCH, guild_id)
        return bool(job) and self.jobs.cancel(job.id)

    async def resume_watches(self) -> list[Job]:
        return [await self.watch(g) for g in await self._store.flagged(WATCH_FLAG)]

    async def known(self, guild_id: int) -> bool:
        return await self._store.raw("guild", guild_id) is not None

    async def stats(self, guild_id: int | None) -> Json:
        return await self._store.counts(guild_id)
