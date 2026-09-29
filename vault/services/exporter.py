"""Guild export: full on first run, incremental afterwards.

    guild ─ roles ─ channels ─ emojis/stickers/events/automod/invites/webhooks/bans
          ─ members ─ audit log (cursor) ─ threads (active + archived)
          ─ messages per channel (cursor = newest stored id)
          ─ reaction users (optional) ─ media (optional)

Message cursors advance after each committed page, so an interrupted export
resumes where it stopped. --rescan-days re-reads a recent window to pick up
edits, reaction changes and deletions that happened while nothing listened.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vault import config
from vault.api import Denied, Json, Missing, Rest
from vault.config import Media, ReactionUsers
from vault.services.media import Downloader
from vault.services.progress import Progress
from vault.store import Store
from vault.store.entities import DISCORD_EPOCH_MS, SNOWFLAKE_TIME_SHIFT

log = logging.getLogger(__name__)

MESSAGES = "messages"
AUDIT = "audit"
NOT_THREAD = f"type NOT IN ({', '.join(str(int(t)) for t in config.THREAD_TYPES)})"

# Share of total work per phase; messages dominate.
PHASES = {"structure": 5, "members": 10, "audit": 5, "threads": 5, "messages": 70, "media": 5}


@dataclass
class ExportOptions:
    media: Media = Media.SKIP
    reaction_users: ReactionUsers = ReactionUsers.SKIP
    rescan_days: int = 0
    channels: set[int] = field(default_factory=set)  # empty = all
    workers: int = 4
    media_root: Path = Path(config.MEDIA_DIR)


def snowflake_at(moment: datetime) -> int:
    """Smallest snowflake at a time; usable as an `after` cursor."""
    ms = int(moment.timestamp() * 1000) - DISCORD_EPOCH_MS
    return max(ms, 0) << SNOWFLAKE_TIME_SHIFT


class Exporter:
    def __init__(self, rest: Rest, store: Store, guild_id: int, options: ExportOptions,
                 progress: Progress | None = None) -> None:
        self._rest = rest
        self._store = store
        self._guild = guild_id
        self._opts = options
        self._progress = progress or Progress("export", PHASES)
        self._stats = self._progress.counters
        self._member_estimate = 0

    async def run(self) -> Json:
        run_id = await self._store.begin_run("export", self._guild)
        try:
            await self._structure()
            await self._members()
            await self._audit()
            channels = await self._threads(await self._channels())
            await self._all_messages(channels)
            await self._media()
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            await self._store.end_run(run_id, status, dict(self._stats))
            raise

        await self._store.end_run(run_id, "ok", dict(self._stats))
        return dict(self._stats)

    # ------------------------------------------------------------ structure

    async def _structure(self) -> None:
        self._progress.phase("structure")
        guild = await self._rest.guild(self._guild)
        self._member_estimate = guild.get("approximate_member_count") or 0
        await self._store.put("guild", guild, self._guild)
        await self._store.guild_stats(guild)

        # Roles and emojis/stickers ship inside the guild payload but have own endpoints.
        await self._listing("role", self._rest.roles)
        await self._listing("emoji", self._rest.emojis)
        await self._listing("sticker", self._rest.stickers)
        await self._listing("event", self._rest.events)
        await self._listing("automod", self._rest.automod)
        await self._listing("invite", self._rest.invites)
        await self._listing("webhook", self._rest.webhooks)

        try:
            seen = await self._store.put_many("ban", [b async for b in self._rest.bans(self._guild)], self._guild)
            await self._store.mark_gone("ban", self._guild, seen)
            self._stats["bans"] = len(seen)
        except Denied:
            self._skip("bans")

        await self._store.commit()

    async def _listing(self, kind: str, fetch) -> None:
        """Store a complete listing and flag what vanished since the last export."""
        try:
            items = await fetch(self._guild)
        except Denied:
            self._skip(kind)
            return

        seen = await self._store.put_many(kind, items, self._guild)
        self._stats[kind] = len(seen)
        self._stats[f"{kind}_gone"] += await self._store.mark_gone(kind, self._guild, seen)

    async def _members(self) -> None:
        self._progress.phase("members", self._member_estimate)
        try:
            seen = set()
            async for member in self._rest.members(self._guild):
                await self._store.put("member", member, self._guild)
                seen.add((self._guild, int(member["user"]["id"])))
                self._progress.advance()
        except Denied:
            # Needs the Server Members privileged intent in the developer portal.
            self._skip("members")
            return

        self._stats["members"] = len(seen)
        self._stats["members_left"] = await self._store.mark_gone("member", self._guild, seen)
        await self._store.commit()

    async def _audit(self) -> None:
        self._progress.phase("audit")
        after = await self._store.cursor(AUDIT, self._guild)
        newest = after
        try:
            async for page in self._rest.audit(self._guild, after):
                self._stats["audit_entries"] += await self._store.put_audit(self._guild, page)
                ids = [int(e["id"]) for e in page["audit_log_entries"]]
                newest = max([newest, *ids])
        except Denied:
            self._skip("audit_log")
            return

        # Cursor moves only after the whole window is stored (pages walk backwards).
        await self._store.set_cursor(AUDIT, self._guild, newest)
        await self._store.commit()

    # ------------------------------------------------------------ channels & threads

    async def _channels(self) -> list[Json]:
        channels = await self._rest.channels(self._guild)
        seen = await self._store.put_many("channel", channels, self._guild)
        self._stats["channels"] = len(seen)
        self._stats["channels_gone"] = await self._store.mark_gone("channel", self._guild, seen, NOT_THREAD)
        await self._store.commit()
        return channels

    async def _threads(self, channels: list[Json]) -> list[Json]:
        """Adds active and archived threads (incl. forum posts) to the channel list."""
        parents = [c for c in channels if c["type"] in config.THREAD_PARENTS]
        self._progress.phase("threads", len(parents))
        threads = {}
        active = await self._rest.active_threads(self._guild)
        for thread in active["threads"]:
            threads[thread["id"]] = thread

        for channel in parents:
            self._progress.advance()
            for scope in ("public", "private"):
                try:
                    async for thread in self._rest.archived_threads(int(channel["id"]), scope):
                        threads[thread["id"]] = thread
                except (Denied, Missing):
                    # Private archives need Manage Threads; skipping keeps public data.
                    self._skip(f"{scope}_threads:{channel['id']}")

        await self._store.put_many("channel", threads.values(), self._guild)
        self._stats["threads"] = len(threads)
        await self._store.commit()
        return channels + list(threads.values())

    # ------------------------------------------------------------ messages

    def _wanted(self, channel: Json) -> bool:
        if channel["type"] not in config.MESSAGE_HOLDERS:
            return False

        if not self._opts.channels:
            return True

        ids = {int(channel["id"]), int(channel.get("parent_id") or 0)}
        return bool(ids & self._opts.channels)

    async def _all_messages(self, channels: list[Json]) -> None:
        # Progress unit = snowflake distance still to walk: start cursor -> last message id.
        work = []
        for channel in filter(self._wanted, channels):
            cursor = await self._store.cursor(MESSAGES, int(channel["id"]))
            start = self._start(cursor)
            span = max(int(channel.get("last_message_id") or 0) - start, 0)
            work.append((channel, cursor, start, span))

        self._progress.phase("messages", sum(w[3] for w in work))
        self._stats["channels_to_sync"] = len(work)
        gate = asyncio.Semaphore(self._opts.workers)

        async def one(channel: Json, cursor: int, start: int, span: int) -> None:
            async with gate:
                await self._messages(channel, cursor, start, span)

            self._stats["channels_synced"] += 1

        await asyncio.gather(*(one(*w) for w in work))

    async def _messages(self, channel: Json, cursor: int, start: int, span: int) -> None:
        cid = int(channel["id"])

        # Nothing new and no rescan: skip the request entirely.
        last = int(channel.get("last_message_id") or 0)
        if start == cursor and last and last <= cursor:
            return

        seen: set[int] = set()
        newest = cursor
        walked = 0
        self._progress.detail(f"#{channel.get('name')}")
        try:
            async for page in self._rest.messages(cid, start):
                self._stats["messages_changed"] += await self._store.put_messages(self._guild, page)
                seen.update(int(m["id"]) for m in page)
                newest = max(newest, int(page[-1]["id"]))
                await self._store.set_cursor(MESSAGES, cid, newest)
                await self._store.commit()

                step = min(newest - start, span) - walked
                self._progress.advance(step)
                walked += step
                self._stats["messages_seen"] += len(page)
        except (Denied, Missing):
            self._skip(f"messages:{cid}")
            return
        finally:
            self._progress.advance(span - walked)

        log.info("#%s: %d messages", channel.get("name"), len(seen))

        # Rescanned window fully walked: stored ids not returned were deleted.
        if start < cursor:
            gone = await self._store.live_ids(cid, start, cursor) - seen
            await self._store.message_gone(gone)
            self._stats["messages_deleted"] += len(gone)

        await self._reaction_users(cid, start)
        await self._store.commit()

    def _start(self, cursor: int) -> int:
        if not cursor or not self._opts.rescan_days:
            return cursor

        window = snowflake_at(datetime.now(timezone.utc) - timedelta(days=self._opts.rescan_days))
        return min(cursor, window)

    async def _reaction_users(self, channel_id: int, after: int) -> None:
        if self._opts.reaction_users is ReactionUsers.SKIP:
            return

        for message_id, key, burst_count in await self._store.reacted(channel_id, after):
            kinds = (0, 1) if burst_count else (0,)
            for burst in kinds:
                try:
                    users = [int(u["id"]) async for u in self._rest.reaction_users(channel_id, message_id, key, burst)]
                except (Denied, Missing):
                    continue

                await self._store.put_reaction_users(message_id, key, burst, users)
                self._stats["reaction_users"] += len(users)

    # ------------------------------------------------------------ media

    async def _media(self) -> None:
        if self._opts.media is Media.SKIP:
            return

        self._progress.phase("media")
        downloader = Downloader(self._rest, self._store, self._opts.media_root, self._opts.workers)
        self._stats["media_saved"] = await downloader.run(self._guild, self._progress)

    def _skip(self, what: str) -> None:
        log.warning("Skipped %s: missing permission or access", what)
        self._stats["skipped"] += 1

