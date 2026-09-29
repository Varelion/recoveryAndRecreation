"""Recreate a stored guild inside another guild.

    roles ─► categories ─► channels (overwrites remapped) ─► emojis
          ─► messages per channel via webhook (original name + avatar)
             threads / forum posts recreated as their first message arrives

Every created object is recorded in import_map, so re-running resumes and
never duplicates. Messages keep order per channel; original time is shown
as a subtext line because webhooks cannot backdate.
"""

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from vault import config
from vault.api import Denied, Json, Missing, Rejected, Rest
from vault.config import ChannelType, Messages, Mode
from vault.services.progress import Progress
from vault.store import Store

log = logging.getLogger(__name__)

WEBHOOK_NAME = "vault-import"
ROLE, CHANNEL, EMOJI, MESSAGE, WEBHOOK = "role", "channel", "emoji", "message", "webhook"
MESSAGE_TYPES = {0, 19, 20, 23}  # DEFAULT, REPLY, CHAT_INPUT_COMMAND, CONTEXT_MENU_COMMAND
SUBTEXT = "-# "

# Share of total work per phase.
PHASES = {"roles": 5, "channels": 10, "emojis": 5, "messages": 80}

# Community-only types and what a plain guild gets instead.
_FALLBACK = {
    ChannelType.ANNOUNCEMENT: ChannelType.TEXT,
    ChannelType.STAGE: ChannelType.VOICE,
    ChannelType.FORUM: ChannelType.TEXT,
    ChannelType.MEDIA: ChannelType.TEXT,
}

# Channel fields safe to copy on creation.
_CHANNEL_FIELDS = ("name", "topic", "nsfw", "rate_limit_per_user", "bitrate", "user_limit", "position",
                   "default_auto_archive_duration", "default_thread_rate_limit_per_user",
                   "available_tags", "default_sort_order", "default_forum_layout", "rtc_region")


@dataclass
class ImportOptions:
    messages: Messages = Messages.INCLUDE
    mode: Mode = Mode.LIVE
    channels: set[int] = field(default_factory=set)  # empty = all


class Importer:
    def __init__(self, rest: Rest, store: Store, source: int, target: int, options: ImportOptions,
                 progress: Progress | None = None) -> None:
        self._rest = rest
        self._store = store
        self._source = source
        self._target = target
        self._opts = options
        self._progress = progress or Progress("import", PHASES)
        self._stats = self._progress.counters

    async def run(self) -> Json:
        run_id = await self._store.begin_run("import", self._target)
        try:
            await self._roles()
            channels = await self._store.guild_rows("channels", self._source, "type = 4 DESC, position")
            await self._channels(channels)
            await self._emojis()
            if self._opts.messages is Messages.INCLUDE:
                await self._all_messages(channels)
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            await self._store.end_run(run_id, status, dict(self._stats))
            raise

        await self._store.end_run(run_id, "ok", dict(self._stats))
        return dict(self._stats)

    # ------------------------------------------------------------ helpers

    @property
    def _dry(self) -> bool:
        return self._opts.mode is Mode.DRY_RUN

    async def _target_id(self, kind: str, source_id: int | str | None) -> int | None:
        if source_id is None:
            return None

        row = await self._store.mapped(self._target, kind, int(source_id))
        return row["target_id"] if row else None

    # ------------------------------------------------------------ roles

    async def _roles(self) -> None:
        roles = await self._store.guild_rows("roles", self._source, "position DESC")
        self._progress.phase("roles", len(roles))
        for role in roles:
            self._progress.advance()
            if role["id"] == str(self._source):
                # @everyone exists already: copy its permissions only.
                await self._everyone(role)
                continue

            # Bot/integration roles are owned by Discord.
            if role.get("managed") or await self._target_id(ROLE, role["id"]):
                continue

            self._stats["roles"] += 1
            if self._dry:
                continue

            created = await self._rest.create_role(self._target, {
                k: role.get(k) for k in ("name", "permissions", "color", "hoist", "mentionable", "unicode_emoji")
            })
            await self._store.map(self._target, ROLE, int(role["id"]), int(created["id"]))

    async def _everyone(self, role: Json) -> None:
        await self._store.map(self._target, ROLE, int(role["id"]), self._target)
        if self._dry:
            return

        await self._rest.edit_role(self._target, self._target, {"permissions": role["permissions"]})

    # ------------------------------------------------------------ channels

    def _wanted(self, channel: Json) -> bool:
        if not self._opts.channels:
            return True

        return bool({int(channel["id"]), int(channel.get("parent_id") or 0)} & self._opts.channels)

    async def _channels(self, channels: list[Json]) -> None:
        # Categories sort first, so parents exist before children.
        self._progress.phase("channels", len(channels))
        for channel in channels:
            self._progress.advance()
            kind = channel["type"]
            if kind in config.THREAD_TYPES or not self._wanted(channel):
                continue

            if await self._target_id(CHANNEL, channel["id"]):
                continue

            self._stats["channels"] += 1
            if self._dry:
                continue

            payload = {k: channel[k] for k in _CHANNEL_FIELDS if channel.get(k) is not None}
            payload["type"] = kind
            payload["parent_id"] = await self._target_id(CHANNEL, channel.get("parent_id"))
            payload["permission_overwrites"] = await self._overwrites(channel)
            payload["available_tags"] = [
                {k: t[k] for k in ("name", "moderated", "emoji_name") if t.get(k) is not None}
                for t in channel.get("available_tags", [])
            ] or None

            try:
                created = await self._rest.create_channel(self._target, _compact(payload))
            except Rejected:
                if kind not in _FALLBACK:
                    raise

                # Target lacks Community; degrade the type.
                log.warning("#%s created as type %s", channel["name"], int(_FALLBACK[kind]))
                payload["type"] = _FALLBACK[kind]
                payload.pop("available_tags", None)
                created = await self._rest.create_channel(self._target, _compact(payload))

            await self._store.map(self._target, CHANNEL, int(channel["id"]), int(created["id"]), str(created["type"]))

    async def _overwrites(self, channel: Json) -> list[Json]:
        out = []
        for ow in channel.get("permission_overwrites", []):
            # Member overwrites keep the id (same user in both guilds); roles are remapped.
            target = int(ow["id"]) if ow["type"] == 1 else await self._target_id(ROLE, ow["id"])
            if target is None:
                continue

            out.append({"id": str(target), "type": ow["type"], "allow": ow["allow"], "deny": ow["deny"]})

        return out

    # ------------------------------------------------------------ emojis

    async def _emojis(self) -> None:
        emojis = await self._store.guild_rows("emojis", self._source, "emoji_id")
        self._progress.phase("emojis", len(emojis))
        for emoji in emojis:
            self._progress.advance()
            if await self._target_id(EMOJI, emoji["id"]):
                continue

            self._stats["emojis"] += 1
            if self._dry:
                continue

            ext = "gif" if emoji.get("animated") else "png"
            try:
                data = await self._rest.cdn(f"{config.CDN}/emojis/{emoji['id']}.{ext}")
                uri = f"data:image/{ext};base64,{base64.b64encode(data).decode()}"
                created = await self._rest.create_emoji(self._target, emoji["name"], uri)
            except (Denied, Missing) as exc:
                log.warning("Emoji %s skipped: %s", emoji["name"], exc)
                continue

            await self._store.map(self._target, EMOJI, int(emoji["id"]), int(created["id"]))

    # ------------------------------------------------------------ messages

    async def _all_messages(self, channels: list[Json]) -> None:
        rows = await self._store.guild_rows("channels", self._source, "channel_id")
        threads: dict[str, list[Json]] = {}
        for row in rows:
            if row["type"] in config.THREAD_TYPES:
                threads.setdefault(row.get("parent_id"), []).append(row)

        wanted = [c for c in channels if c["type"] != ChannelType.CATEGORY and self._wanted(c)]
        total = done = 0
        for channel in wanted:
            total += await self._store.message_count(int(channel["id"]))
            done += await self._store.imported_count(self._target, int(channel["id"]))

        # Already-imported messages count as done, so a resumed job starts at its real percentage.
        self._progress.phase("messages", total)
        self._progress.advance(done)

        for channel in wanted:
            target = await self._store.mapped(self._target, CHANNEL, int(channel["id"]))
            if self._dry:
                count = await self._store.message_count(int(channel["id"]))
                self._stats["messages"] += count
                self._progress.advance(count)
                continue

            if target is None:
                continue

            self._progress.detail(f"#{channel['name']}")
            await self._replay(int(channel["id"]), target["target_id"], None)
            forum = target["extra"] == str(int(ChannelType.FORUM))
            for thread in threads.get(channel["id"], []):
                await self._thread(thread, target["target_id"], forum)

    async def _thread(self, thread: Json, parent: int, forum: bool) -> None:
        """Forum posts are born from their first webhook message; other threads are opened first."""
        source = int(thread["id"])
        self._progress.detail(f"#{thread['name']}")
        target = await self._target_id(CHANNEL, source)
        if target is None and not forum:
            created = await self._rest.create_thread(parent, thread["name"])
            target = int(created["id"])
            await self._store.map(self._target, CHANNEL, source, target)

        await self._replay(source, parent, target, thread["name"])

    async def _replay(self, source: int, channel: int, thread: int | None, thread_name: str | None = None) -> None:
        """Send stored messages of `source` through a webhook of `channel`, resuming after the last sent."""
        hook = await self._webhook(channel)
        resume = await self._store.last_mapped(self._target, source)

        async for message in self._store.channel_messages(source, resume):
            self._progress.advance()
            if message.get("type", 0) not in MESSAGE_TYPES:
                continue

            # No thread yet: this message opens the forum post.
            opener = thread_name if thread is None else None
            created = await self._send(hook, message, thread, opener)
            if created is None:
                continue

            self._stats["messages"] += 1
            await self._store.map(self._target, MESSAGE, int(message["id"]), int(created["id"]))
            if thread is None and opener:
                thread = int(created["channel_id"])
                await self._store.map(self._target, CHANNEL, source, thread)

            if message.get("pinned"):
                await self._pin(int(created["channel_id"]), int(created["id"]))

    async def _webhook(self, channel_id: int) -> tuple[int, str]:
        row = await self._store.mapped(self._target, WEBHOOK, channel_id)
        if row:
            return row["target_id"], row["extra"]

        hook = await self._rest.create_webhook(channel_id, WEBHOOK_NAME)
        await self._store.map(self._target, WEBHOOK, channel_id, int(hook["id"]), hook["token"])
        return int(hook["id"]), hook["token"]

    async def _send(self, hook: tuple[int, str], message: Json, thread_id: int | None,
                    thread_name: str | None) -> Json | None:
        author = message.get("author") or {}
        name = (author.get("global_name") or author.get("username") or "unknown")[:config.WEBHOOK_NAME_LIMIT]
        files = await self._files(message)
        payload = _compact({
            "username": name,
            "avatar_url": _avatar(author),
            "content": _content(message, len(files)),
            "embeds": [e for e in message.get("embeds", []) if e.get("type") == "rich"][:config.EMBED_LIMIT] or None,
            "allowed_mentions": {"parse": []},  # never ping on replay
            "thread_name": thread_name,
        })

        try:
            return await self._rest.execute_webhook(*hook, payload, files, thread_id)
        except (Denied, Missing) as exc:
            log.warning("Message %s skipped: %s", message["id"], exc)
            self._stats["messages_failed"] += 1
            return None

    async def _files(self, message: Json) -> list[tuple[str, bytes]]:
        """Local copies first; live CDN as fallback (links may have expired)."""
        files = []
        for a in message.get("attachments", []):
            path = await self._store.media_path(int(a["id"]))
            try:
                data = Path(path).read_bytes() if path else await self._rest.cdn(a["url"])
            except (OSError, Denied, Missing):
                self._stats["attachments_lost"] += 1
                continue

            files.append((a["filename"], data))

        return files

    async def _pin(self, channel_id: int, message_id: int) -> None:
        try:
            await self._rest.pin(channel_id, message_id)
        except Denied:
            self._stats["pins_failed"] += 1


def _avatar(author: Json) -> str | None:
    if not author.get("avatar"):
        return None

    return f"{config.CDN}/avatars/{author['id']}/{author['avatar']}.png"


def _content(message: Json, file_count: int) -> str:
    """Original text + reply hint + lost attachment note + timestamp, within Discord's limit.

    e.g. "hello\n-# ↪ reply · 2021-04-02 18:03 UTC"
    """
    stamp = datetime.fromisoformat(message["timestamp"]).strftime("%Y-%m-%d %H:%M UTC")
    notes = [stamp]
    if message.get("message_reference") and message.get("type") == 19:
        notes.insert(0, "↪ reply")

    lost = len(message.get("attachments", [])) - file_count
    if lost:
        notes.append(f"{lost} attachment(s) unavailable")

    footer = SUBTEXT + " · ".join(notes)
    body = message.get("content") or ""
    room = config.CONTENT_LIMIT - len(footer) - 1
    if len(body) > room:
        body = body[: room - 1] + "…"

    return f"{body}\n{footer}" if body else footer


def _compact(payload: Json) -> Json:
    return {k: v for k, v in payload.items() if v is not None}
