"""Live recorder: applies gateway events of one guild to the store.

Captures what polling cannot: edits to old messages, deletions, reaction
timing, joins/leaves, voice activity. Every event except message creation
is also appended raw to gateway_events for time-based analytics.
"""

import logging

from vault.api import Json, Link
from vault.services.progress import Progress
from vault.store import Store
from vault.store.entities import emoji_key

log = logging.getLogger(__name__)

# Stored as entities already; logging them raw would duplicate the data.
_UNLOGGED = {"MESSAGE_CREATE", "GUILD_CREATE"}

# Event -> entity kind for simple upserts.
_PUTS = {
    "CHANNEL_CREATE": "channel", "CHANNEL_UPDATE": "channel",
    "THREAD_CREATE": "channel", "THREAD_UPDATE": "channel",
    "GUILD_UPDATE": "guild",
    "INVITE_CREATE": "invite",
    "GUILD_SCHEDULED_EVENT_CREATE": "event", "GUILD_SCHEDULED_EVENT_UPDATE": "event",
    "AUTO_MODERATION_RULE_CREATE": "automod", "AUTO_MODERATION_RULE_UPDATE": "automod",
}

# Event -> (entity kind, id field) for deletions.
_GONES = {
    "CHANNEL_DELETE": ("channel", "id"), "THREAD_DELETE": ("channel", "id"),
    "GUILD_ROLE_DELETE": ("role", "role_id"),
    "INVITE_DELETE": ("invite", "code"),
    "GUILD_SCHEDULED_EVENT_DELETE": ("event", "id"),
    "AUTO_MODERATION_RULE_DELETE": ("automod", "id"),
}


class Watcher:
    def __init__(self, link: Link, store: Store, guild_id: int, progress: Progress | None = None) -> None:
        self._link = link
        self._store = store
        self._guild = guild_id
        self._progress = progress or Progress("watch")
        self._handlers = {
            "MESSAGE_CREATE": self._message,
            "MESSAGE_UPDATE": self._message_edit,
            "MESSAGE_DELETE": self._message_delete,
            "MESSAGE_DELETE_BULK": self._message_delete,
            "MESSAGE_REACTION_ADD": self._reaction,
            "MESSAGE_REACTION_REMOVE": self._reaction,
            "MESSAGE_REACTION_REMOVE_ALL": self._reaction_clear,
            "MESSAGE_REACTION_REMOVE_EMOJI": self._reaction_clear,
            "GUILD_MEMBER_ADD": self._member,
            "GUILD_MEMBER_UPDATE": self._member,
            "GUILD_MEMBER_REMOVE": self._member_remove,
            "GUILD_ROLE_CREATE": self._role,
            "GUILD_ROLE_UPDATE": self._role,
            "GUILD_BAN_ADD": self._ban,
            "GUILD_BAN_REMOVE": self._unban,
            "GUILD_EMOJIS_UPDATE": self._emojis,
            "GUILD_STICKERS_UPDATE": self._stickers,
            "THREAD_LIST_SYNC": self._thread_sync,
            "GUILD_AUDIT_LOG_ENTRY_CREATE": self._audit,
        }

    async def run(self) -> None:
        """Record until cancelled. The gateway must be connected by the caller."""
        run_id = await self._store.begin_run("watch", self._guild)
        self._progress.phase("listening")
        log.info("Watching guild %s", self._guild)
        try:
            async with self._link.feed() as events:
                while True:
                    kind, data = await events.get()
                    await self._safe(kind, data)
        finally:
            await self._store.end_run(run_id, "ok", dict(self._progress.counters))

    async def _safe(self, kind: str, data: Json) -> None:
        try:
            await self._on_event(kind, data)
        except Exception:
            # One bad event must not stop the recorder.
            log.exception("Event %s failed", kind)
            self._progress.counters["errors"] += 1

    async def _on_event(self, kind: str, data: Json) -> None:
        guild = data.get("id") if kind in ("GUILD_CREATE", "GUILD_UPDATE") else data.get("guild_id")
        if str(guild) != str(self._guild):
            return

        self._progress.counters[kind.lower()] += 1
        self._progress.detail(f"last: {kind}")
        if kind not in _UNLOGGED:
            await self._store.log_event(kind, data)

        await self._apply(kind, data)
        await self._store.commit()

    async def _apply(self, kind: str, data: Json) -> None:
        if kind in _PUTS:
            await self._store.put(_PUTS[kind], data, self._guild)
            return

        if kind in _GONES:
            entity, field = _GONES[kind]
            await self._store.gone(entity, _key(data[field]))
            return

        handler = self._handlers.get(kind)
        if handler:
            await handler(kind, data)

    # ------------------------------------------------------------ messages

    async def _message(self, _: str, data: Json) -> None:
        await self._store.put_messages(self._guild, [data])

    async def _message_edit(self, _: str, data: Json) -> None:
        # Updates may be partial; overlay on the stored copy.
        old = await self._store.message_raw(int(data["id"]))
        if old is None and "author" not in data:
            return

        await self._store.put_messages(self._guild, [(old or {}) | data])

    async def _message_delete(self, _: str, data: Json) -> None:
        await self._store.message_gone(data.get("ids") or [data["id"]])

    async def _reaction(self, kind: str, data: Json) -> None:
        delta = 1 if kind == "MESSAGE_REACTION_ADD" else -1
        await self._store.react(int(data["message_id"]), data["emoji"], int(data["user_id"]),
                                int(bool(data.get("burst"))), delta)

    async def _reaction_clear(self, _: str, data: Json) -> None:
        key = emoji_key(data["emoji"]) if data.get("emoji") else None
        await self._store.clear_reactions(int(data["message_id"]), key)

    # ------------------------------------------------------------ members & guild

    async def _member(self, _: str, data: Json) -> None:
        old = await self._store.raw("member", self._guild, int(data["user"]["id"])) or {}
        member = old | {k: v for k, v in data.items() if k != "guild_id"}
        await self._store.put("member", member, self._guild)

    async def _member_remove(self, _: str, data: Json) -> None:
        await self._store.gone("member", self._guild, int(data["user"]["id"]))

    async def _role(self, _: str, data: Json) -> None:
        await self._store.put("role", data["role"], self._guild)

    async def _ban(self, _: str, data: Json) -> None:
        await self._store.put("ban", {"user": data["user"], "reason": None}, self._guild)

    async def _unban(self, _: str, data: Json) -> None:
        await self._store.gone("ban", self._guild, int(data["user"]["id"]))

    async def _emojis(self, _: str, data: Json) -> None:
        seen = await self._store.put_many("emoji", data["emojis"], self._guild)
        await self._store.mark_gone("emoji", self._guild, seen)

    async def _stickers(self, _: str, data: Json) -> None:
        seen = await self._store.put_many("sticker", data["stickers"], self._guild)
        await self._store.mark_gone("sticker", self._guild, seen)

    async def _thread_sync(self, _: str, data: Json) -> None:
        await self._store.put_many("channel", data.get("threads", []), self._guild)

    async def _audit(self, _: str, data: Json) -> None:
        await self._store.put_audit(self._guild, {"audit_log_entries": [data]})


def _key(value: str | int) -> str | int:
    # Snowflakes are stored as integers; invite codes stay text.
    return int(value) if str(value).isdigit() else value
