"""Converge a guild onto a blueprint: create what is missing, fix what drifted.

    preflight ─► name ─► roles ─► prune ─► order ─► channels ─► prune ─► order ─► posts
        │                  │                            │                            │
   roles to touch     adopt by name               adopt by id;               forum posts opened
   below the bot?                                 type change =              first, then docs cut
                                                  new channel                into ≤2000-char
                                                                             messages, edited by hash;
                                                                             <!-- embed --> blocks become
                                                                             one embed message each

    build_map: 'role:Staff' | 'channel:velaris' | 'post:claims:docs/x.md' | 'message:rules:0'
               | 'retired:<id>' ─► Discord id (+ hash of the posted text)

Roles and channels are compared with their live state, so a re-run writes
nothing unless the blueprint or the guild changed. Roles absent from the
blueprint are deleted only with BuildOptions.roles = DELETE. Channels only
with BuildOptions.channels = DELETE, and only those the build itself made,
adopted or retired: never a channel it does not know.
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass

from vault import config
from vault.api import Json, Missing, Rejected, Rest
from vault.config import ChannelType, Mode, Prune
from vault.services.blueprint import (
    CHANNEL_REF, EVERYONE, GRANT, ROLE_REF, Blueprint, Category, Channel, Role, Thread,
)
from vault.services.progress import Progress
from vault.store import Store

log = logging.getLogger(__name__)

# Share of total work per phase.
PHASES = {"roles": 10, "channels": 40, "posts": 50}

ROLE, CHANNEL, POST, MESSAGE, RETIRED = "role:", "channel:", "post:", "message:", "retired:"
ROLE_OVERWRITE, MEMBER_OVERWRITE = 0, 1  # overwrite types (spec)
PINNED = 1 << 1  # channel flag (spec): pinned forum post
REQUIRE_TAG = 1 << 4  # channel flag (spec): a forum's new posts must carry a tag
NO_PINGS = {"parse": []}
SILENT = 1 << 12  # message flag (spec): SUPPRESS_NOTIFICATIONS
BUMP_TEXT = "·"  # the touch that re-sorts an ordered forum; deleted at once
CHANGES = ("posts_created", "posts_updated", "messages_sent", "messages_edited", "messages_deleted")

# Doc authors force a message break with this line.
SPLIT = "<!-- split -->"
_HEADING = re.compile(r"\n(?=#{1,3} )")

# Doc authors start an embed message with this line; it runs to the next embed, split or the end.
# Its first line is the title, the rest the description, e.g.
#   <!-- embed #C0392B -->
#   **🐒 Saiyan**
#   750–1,500 · Vegeta   ─►   one message: a red-barred embed titled "🐒 Saiyan"
_EMBED_START = re.compile(r"^(?=<!-- embed[ #])", re.M)
_EMBED = re.compile(r"<!-- embed(?: #([0-9A-Fa-f]{6}))? -->")
_TITLE_MARKS = "*#_ "


class Blocked(Exception):
    """Roles to change sit at or above the bot's highest role; Discord would refuse."""


@dataclass
class BuildOptions:
    mode: Mode = Mode.LIVE
    roles: Prune = Prune.KEEP  # roles absent from the blueprint
    channels: Prune = Prune.KEEP  # channels the build made, then dropped or replaced


class Builder:
    def __init__(self, rest: Rest, store: Store, guild_id: int, blueprint: Blueprint, options: BuildOptions,
                 progress: Progress | None = None) -> None:
        self._rest = rest
        self._store = store
        self._guild = guild_id
        self._bp = blueprint
        self._opts = options
        self._progress = progress or Progress("build", PHASES)
        self._stats = self._progress.counters
        self._report: dict[str, list[str]] = {}
        self._role_ids: dict[str, int] = {EVERYONE: guild_id}
        self._channel_ids: dict[str, int] = {}
        self._retired: set[int] = set()  # channels replaced this run (type changed)
        self._opened: set[int] = set()  # forum posts created this run
        self._bot_roles: set[str] = set()
        self._stand_ins = 0  # dry-run ids of objects not created

    async def run(self) -> Json:
        run_id = await self._store.begin_run("build", self._guild)
        try:
            await self._build()
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            await self._store.end_run(run_id, status, dict(self._stats))
            raise

        await self._store.end_run(run_id, "ok", dict(self._stats))
        return dict(self._stats) | self._report

    async def _build(self) -> None:
        live = await self._rest.roles(self._guild)
        await self._preflight(live)
        await self._rename()
        await self._roles(live)
        await self._channels()
        await self._posts()

    # ------------------------------------------------------------ helpers

    @property
    def _dry(self) -> bool:
        return self._opts.mode is Mode.DRY_RUN

    def _stand_in(self) -> int:
        self._stand_ins -= 1
        return self._stand_ins

    def _top(self, live: list[Json]) -> int:
        """Position of the bot's highest role; it moves up as roles are created below it."""
        return max((r["position"] for r in live if r["id"] in self._bot_roles), default=0)

    async def _mark(self, key: str, target_id: int, digest: str | None = None) -> None:
        if self._dry:
            return

        await self._store.mark_built(self._guild, key, target_id, digest)

    def _render(self, text: str) -> str:
        """{#velaris} -> channel link, {@Staff} -> role mention (posts never ping)."""
        text = CHANNEL_REF.sub(lambda m: f"<#{self._channel_ids[m[1]]}>", text)
        return ROLE_REF.sub(lambda m: f"<@&{self._role_ids[m[1]]}>", text)

    # ------------------------------------------------------------ guild

    async def _preflight(self, live: list[Json]) -> None:
        """Discord refuses edits to roles at or above the bot's highest role; check before any write."""
        me = await self._rest.me()
        member = await self._rest.member(self._guild, int(me["id"]))
        self._bot_roles = set(member["roles"])

        top = self._top(live)
        wanted = {r.name for r in self._bp.roles}
        doomed = self._opts.roles is Prune.DELETE
        touched = [r for r in live if r["name"] in wanted or (doomed and self._extra(r, wanted))]
        blocked = sorted(r["name"] for r in touched if r["position"] >= top)
        if not blocked:
            return

        self._report["blocked_roles"] = blocked
        if not self._dry:
            raise Blocked(f"Drag the bot's role above these roles first: {', '.join(blocked)}")

    def _extra(self, role: Json, wanted: set[str]) -> bool:
        return not role.get("managed") and int(role["id"]) != self._guild and role["name"] not in wanted

    async def _rename(self) -> None:
        if not self._bp.name:
            return

        guild = await self._rest.guild(self._guild)
        if guild["name"] == self._bp.name:
            return

        self._stats["guild_renamed"] += 1
        if not self._dry:
            await self._rest.edit_guild(self._guild, {"name": self._bp.name})

    # ------------------------------------------------------------ roles

    async def _roles(self, live: list[Json]) -> None:
        by_id = {int(r["id"]): r for r in live}
        # Ascending, so the highest of same-named roles wins.
        by_name = {r["name"]: r for r in sorted(live, key=lambda r: r["position"]) if not r.get("managed")}

        self._progress.phase("roles", len(self._bp.roles))
        for role in self._bp.roles:
            self._progress.advance()
            self._role_ids[role.name] = await self._role(role, by_id, by_name)

        await self._prune(live)
        await self._order_roles()

    async def _role(self, role: Role, by_id: dict[int, Json], by_name: dict[str, Json]) -> int:
        """The mapped role, else a same-named one, else a new one; edited only where it differs."""
        key = ROLE + role.name
        row = await self._store.built(self._guild, key)
        current = by_id.get(row["target_id"]) if row else None
        if current is None and role.name in by_name:
            current = by_name[role.name]
            self._stats["roles_adopted"] += 1

        wanted = _role_payload(role)
        if current is None:
            return await self._create_role(key, wanted)

        role_id = int(current["id"])
        if {k: current.get(k) for k in wanted} != wanted:
            self._stats["roles_updated"] += 1
            if not self._dry:
                await self._rest.edit_role(self._guild, role_id, wanted)

        await self._mark(key, role_id)
        return role_id

    async def _create_role(self, key: str, payload: Json) -> int:
        self._stats["roles_created"] += 1
        if self._dry:
            return self._stand_in()

        # Unlisted permissions mean none, not Discord's default copy of @everyone's.
        created = await self._rest.create_role(self._guild, {"permissions": "0"} | payload)
        await self._mark(key, int(created["id"]))
        return int(created["id"])

    async def _prune(self, live: list[Json]) -> None:
        """Roles absent from the blueprint: always reported, deleted only with Prune.DELETE."""
        kept = set(self._role_ids.values())
        extras = [r for r in live if not r.get("managed") and int(r["id"]) not in kept]
        if not extras:
            return

        self._report["extra_roles"] = sorted(r["name"] for r in extras)
        if self._opts.roles is Prune.KEEP:
            return

        for role in extras:
            self._stats["roles_deleted"] += 1
            if not self._dry:
                await self._rest.delete_role(self._guild, int(role["id"]))

    async def _order_roles(self) -> None:
        """Blueprint roles right below the bot in blueprint order; other roles below the bot keep their order.

            The After (bot) ─ High Council ─ Moderator ─ ... ─ Spectator ─ Avrae ─ @everyone
        """
        live = sorted(await self._rest.roles(self._guild), key=lambda r: (-r["position"], int(r["id"])))
        top = self._top(live)
        below = [int(r["id"]) for r in live if r["position"] < top and int(r["id"]) != self._guild]
        ours = [self._role_ids[r.name] for r in self._bp.roles]
        wanted = [i for i in ours if i in below] + [i for i in below if i not in ours]
        if below == wanted:
            return

        self._stats["roles_reordered"] += 1
        if self._dry:
            return

        # Counting down from just below the bot. Roles may share a position (Discord then orders them by id),
        # so there can be more roles than free positions: the surplus shares 1, just above @everyone.
        #   bot 41 ─ Staff 40 ─ ... ─ Spectator 2 ─ Character 1..10 all 1 ─ @everyone 0
        positions = [{"id": str(i), "position": max(1, top - 1 - n)} for n, i in enumerate(wanted)]
        await self._rest.order_roles(self._guild, positions)

    # ------------------------------------------------------------ channels

    async def _channels(self) -> None:
        live = {int(c["id"]): c for c in await self._rest.channels(self._guild)}
        self._progress.phase("channels", sum(1 + len(c.channels) for c in self._bp.categories))
        for category in self._bp.categories:
            self._progress.advance()
            payload = self._category_payload(category)
            parent = await self._channel(CHANNEL + category.id, None, payload, live)
            self._channel_ids[category.id] = parent

            for channel in category.channels:
                self._progress.advance()
                self._progress.detail(channel.name)
                payload = self._channel_payload(channel, category, parent)
                self._channel_ids[channel.id] = await self._channel(CHANNEL + channel.id, channel.adopt, payload, live)

        await self._prune_channels()
        await self._order_channels()

    async def _channel(self, key: str, adopt: int | None, wanted: Json, live: dict[int, Json]) -> int:
        """The mapped channel, else the adopted one, else a new one; edited only where it differs."""
        row = await self._store.built(self._guild, key)
        current = live.get(row["target_id"]) if row else None
        if current is None and adopt in live:
            current = live[adopt]
            self._stats["channels_adopted"] += 1

        # Discord cannot change a channel's type (text -> forum): build a new one, retire the old.
        if current is not None and int(current["type"]) != int(wanted["type"]):
            await self._retire(key, int(current["id"]))
            current = None

        if current is None:
            return await self._create_channel(key, wanted)

        channel_id = int(current["id"])
        ours = {str(i) for i in self._role_ids.values()}
        if _shape(current, ours) != _shape(wanted, ours):
            self._stats["channels_updated"] += 1
            if not self._dry:
                await self._rest.edit_channel(channel_id, _patch(wanted, current, ours))

        await self._mark(key, channel_id)
        return channel_id

    async def _create_channel(self, key: str, payload: Json) -> int:
        self._stats["channels_created"] += 1
        if self._dry:
            return self._stand_in()

        # Discord takes channel flags only on edit, not on creation.
        flags = payload.get("flags", 0)
        created = await self._rest.create_channel(self._guild, {k: v for k, v in payload.items() if k != "flags"})
        channel_id = int(created["id"])
        if flags:
            await self._rest.edit_channel(channel_id, {"flags": flags})

        await self._mark(key, channel_id)
        return channel_id

    async def _retire(self, key: str, channel_id: int) -> None:
        """Remember a replaced channel for pruning; its posted docs belong to the old channel."""
        self._stats["channels_replaced"] += 1
        self._retired.add(channel_id)
        if self._dry:
            return

        await self._store.mark_built(self._guild, f"{RETIRED}{channel_id}", channel_id)
        await self._forget_posts(key[len(CHANNEL):])

    async def _forget_posts(self, blueprint_id: str) -> None:
        """Drop the message and forum-post keys of a blueprint channel id."""
        for prefix in (f"{MESSAGE}{blueprint_id}:", f"{POST}{blueprint_id}:", f"{MESSAGE}{POST}{blueprint_id}:"):
            await self._store.unbuild_under(self._guild, prefix)

    async def _prune_channels(self) -> None:
        """Channels the build made or adopted that the blueprint dropped, or that were replaced.

        Always reported; deleted only with BuildOptions.channels = DELETE. Unknown channels
        (ticket channels, anything made by hand) are never touched.
        """
        wanted = {c.id for c in self._bp.categories} | {ch.id for ch in self._bp.channels()}
        built = await self._store.built_under(self._guild, CHANNEL)
        dropped = [r for r in built if r["key"][len(CHANNEL):] not in wanted]
        retired = await self._store.built_under(self._guild, RETIRED)
        doomed = {int(r["target_id"]): r["key"] for r in dropped + retired}
        doomed |= {i: f"{RETIRED}{i}" for i in self._retired if i not in doomed}
        if not doomed:
            return

        live = {int(c["id"]): c for c in await self._rest.channels(self._guild)}
        self._report["extra_channels"] = sorted(live[i]["name"] for i in doomed if i in live)
        if self._opts.channels is Prune.KEEP:
            return

        for channel_id, key in doomed.items():
            if self._dry:
                self._stats["channels_deleted"] += channel_id in live
                continue

            if not await self._delete_channel(channel_id):
                continue

            await self._store.unbuild(self._guild, key)
            if key.startswith(CHANNEL):
                await self._forget_posts(key[len(CHANNEL):])

    async def _delete_channel(self, channel_id: int) -> bool:
        """False when Discord keeps the channel, e.g. the Community rules channel."""
        try:
            await self._rest.delete_channel(channel_id)
        except Missing:
            return True
        except Rejected as exc:
            log.warning("Channel %s kept: %s", channel_id, exc)
            return False

        self._stats["channels_deleted"] += 1
        return True

    def _category_payload(self, category: Category) -> Json:
        return {
            "name": category.name,
            "type": ChannelType.CATEGORY,
            "permission_overwrites": self._overwrites(category.access, ()),
        }

    def _channel_payload(self, channel: Channel, category: Category, parent: int) -> Json:
        payload = {
            "name": channel.name,
            "type": channel.kind,
            "parent_id": str(parent),
            "nsfw": channel.nsfw,
            "permission_overwrites": self._overwrites(channel.access or category.access, channel.grant),
        }
        if channel.kind is not ChannelType.VOICE:
            payload["topic"] = channel.topic
            payload["rate_limit_per_user"] = channel.slowmode

        if channel.kind is ChannelType.FORUM:
            payload["available_tags"] = [
                {"name": tag.name, "moderated": tag.moderated, "emoji_name": tag.emoji} for tag in channel.tags
            ]
            payload["flags"] = REQUIRE_TAG if channel.require_tag else 0

        return payload

    def _overwrites(self, preset: str, grant: tuple[str, ...]) -> list[Json]:
        """Preset rules as Discord overwrites; GRANT stands for each granted role; one role's rules merge."""
        merged: dict[int, tuple[int, int]] = {}
        for rule in self._bp.presets[preset]:
            for name in grant if rule.role == GRANT else (rule.role,):
                role_id = self._role_ids[name]
                allow, deny = merged.get(role_id, (0, 0))
                merged[role_id] = (allow | int(rule.allow), deny | int(rule.deny))

        return [
            {"id": str(role_id), "type": ROLE_OVERWRITE, "allow": str(allow), "deny": str(deny)}
            for role_id, (allow, deny) in merged.items()
        ]

    async def _order_channels(self) -> None:
        """Categories in blueprint order; inside each, its channels in blueprint order."""
        live = {int(c["id"]): c for c in await self._rest.channels(self._guild)}
        groups = [[c.id for c in self._bp.categories]] + [[ch.id for ch in c.channels] for c in self._bp.categories]

        positions: list[Json] = []
        for group in groups:
            ids = [self._channel_ids[i] for i in group if self._channel_ids[i] in live]
            if sorted(ids, key=lambda i: (live[i].get("position", 0), i)) != ids:
                positions += [{"id": str(i), "position": n} for n, i in enumerate(ids)]

        if not positions:
            return

        self._stats["channels_reordered"] += 1
        if not self._dry:
            await self._rest.order_channels(self._guild, positions)

    # ------------------------------------------------------------ posts

    async def _posts(self) -> None:
        live = {int(c["id"]): c for c in await self._rest.channels(self._guild)}
        work = [c for c in self._bp.channels() if c.docs or c.threads]
        tags = {c.id: _tag_ids(live.get(self._channel_ids[c.id], {})) for c in work}
        self._progress.phase("posts", sum(bool(c.docs) + len(c.threads) for c in work))

        # Every forum post exists before any text renders, so {#page} links always resolve.
        changed: set[str] = set()
        for channel in work:
            before = self._changes()
            for thread in channel.threads:
                await self._open_post(channel, thread, tags[channel.id])

            if self._changes() != before:
                changed.add(channel.id)

        for channel in work:
            self._progress.detail(channel.name)
            before = self._changes()
            if channel.docs:
                self._progress.advance()
                text = f"\n\n{SPLIT}\n\n".join(doc.text for doc in channel.docs)
                await self._messages(MESSAGE + channel.id, self._channel_ids[channel.id], _chunks(self._render(text)))

            for thread in channel.threads:
                self._progress.advance()
                await self._thread(channel, thread, tags[channel.id])

            if self._changes() != before:
                changed.add(channel.id)

            if channel.ordered and channel.id in changed:
                await self._bump(channel)

    def _changes(self) -> tuple[int, ...]:
        return tuple(self._stats[name] for name in CHANGES)

    async def _bump(self, channel: Channel) -> None:
        """Forums sort by latest activity. Touching every post in blueprint order puts the last listed on top:

            blueprint  11 · 10 · … · 00 · Rules   ─►   forum  Rules, 00, 01, … 11

        Each touch is a silent message deleted at once: the post keeps the bump, nobody is notified.
        Pinned posts stay on top anyway, so they are skipped.
        """
        for thread in channel.threads:
            if thread.pin:
                continue

            self._stats["posts_bumped"] += 1
            if self._dry:
                continue

            post_id = (await self._store.built(self._guild, _post_key(channel, thread)))["target_id"]
            await self._rest.edit_channel(post_id, {"archived": False})
            touch = await self._rest.send_message(post_id, {"content": BUMP_TEXT, "flags": SILENT, "allowed_mentions": NO_PINGS})
            await self._rest.delete_message(post_id, int(touch["id"]))

    async def _open_post(self, channel: Channel, thread: Thread, tags: dict[str, int]) -> None:
        """The post's id, created if needed, recorded for {#id} links."""
        key = _post_key(channel, thread)
        row = await self._store.built(self._guild, key)
        post_id = row["target_id"] if row else await self._create_post(key, channel, thread, tags)
        if thread.id is not None:
            self._channel_ids[thread.id] = post_id

    async def _create_post(self, key: str, channel: Channel, thread: Thread, tags: dict[str, int]) -> int:
        """Opened with its title as a stub; the real text replaces it once every link resolves."""
        self._stats["posts_created"] += 1
        if self._dry:
            return self._stand_in()

        meta = _meta(thread, tags)
        message = {"content": thread.title, "allowed_mentions": NO_PINGS}
        forum_id = self._channel_ids[channel.id]
        post = await self._rest.create_post(forum_id, thread.title, message, [int(t) for t in meta["applied_tags"]])
        post_id = int(post["id"])
        if meta["flags"]:
            await self._rest.edit_channel(post_id, {"flags": meta["flags"]})

        # The opening message shares the post's id.
        await self._mark(key, post_id, _hash(json.dumps(meta, sort_keys=True)))
        await self._mark(f"{MESSAGE}{key}:0", post_id, _hash(thread.title))
        self._opened.add(post_id)
        return post_id

    async def _thread(self, channel: Channel, thread: Thread, tags: dict[str, int]) -> None:
        """A post's text: the opening message is part 0, replies follow."""
        key = _post_key(channel, thread)
        prefix = MESSAGE + key
        parts = _chunks(self._render(thread.doc.text))
        row = await self._store.built(self._guild, key)
        if row is None:
            # Dry run: the post was only counted.
            self._stats["messages_sent"] += len(parts) - 1
            return

        post_id = row["target_id"]
        meta = _meta(thread, tags)
        stale_meta = row["digest"] != _hash(json.dumps(meta, sort_keys=True))
        if post_id not in self._opened and (stale_meta or await self._stale(prefix, parts)):
            await self._edit_post(key, post_id, meta, stale_meta)

        await self._messages(prefix, post_id, parts)

    async def _edit_post(self, key: str, post_id: int, meta: Json, stale_meta: bool) -> None:
        """Unarchive before any change (archived posts refuse edits); rename and retag when needed."""
        if stale_meta:
            self._stats["posts_updated"] += 1

        if self._dry:
            return

        await self._rest.edit_channel(post_id, {"archived": False} | (meta if stale_meta else {}))
        await self._mark(key, post_id, _hash(json.dumps(meta, sort_keys=True)))

    async def _stale(self, prefix: str, parts: list[str]) -> bool:
        """True when any message under `prefix` needs a send, edit or delete."""
        for index, part in enumerate(parts):
            row = await self._store.built(self._guild, f"{prefix}:{index}")
            if row is None or row["digest"] != _hash(part):
                return True

        return await self._store.built(self._guild, f"{prefix}:{len(parts)}") is not None

    async def _messages(self, prefix: str, channel_id: int, parts: list[str]) -> None:
        """Message i under `prefix` holds parts[i]: send new, edit changed, delete surplus."""
        for index, part in enumerate(parts):
            await self._message(f"{prefix}:{index}", channel_id, part)

        index = len(parts)
        while row := await self._store.built(self._guild, f"{prefix}:{index}"):
            await self._drop(f"{prefix}:{index}", channel_id, row["target_id"])
            index += 1

    async def _message(self, key: str, channel_id: int, text: str) -> None:
        digest = _hash(text)
        row = await self._store.built(self._guild, key)
        if row and row["digest"] == digest:
            return

        payload = _payload(text)
        if row and await self._edit(key, channel_id, row["target_id"], payload, digest):
            return

        self._stats["messages_sent"] += 1
        if self._dry:
            return

        sent = await self._rest.send_message(channel_id, payload)
        await self._mark(key, int(sent["id"]), digest)

    async def _edit(self, key: str, channel_id: int, message_id: int, payload: Json, digest: str) -> bool:
        """False when the message was deleted by hand, so the caller sends it again (it lands last)."""
        if self._dry:
            self._stats["messages_edited"] += 1
            return True

        try:
            await self._rest.edit_message(channel_id, message_id, payload)
        except Missing:
            log.warning("%s was deleted; sending it again", key)
            return False

        self._stats["messages_edited"] += 1
        await self._mark(key, message_id, digest)
        return True

    async def _drop(self, key: str, channel_id: int, message_id: int) -> None:
        self._stats["messages_deleted"] += 1
        if self._dry:
            return

        try:
            await self._rest.delete_message(channel_id, message_id)
        except Missing:
            pass

        await self._store.unbuild(self._guild, key)


def _post_key(channel: Channel, thread: Thread) -> str:
    return f"{POST}{channel.id}:{thread.doc.name}"


def _meta(thread: Thread, tags: dict[str, int]) -> Json:
    """What a build sets on a forum post besides its text."""
    return {
        "name": thread.title,
        "applied_tags": [str(tags[t]) for t in thread.tags if t in tags],
        "flags": PINNED if thread.pin else 0,
    }


def _tag_ids(channel: Json) -> dict[str, int]:
    return {t["name"]: int(t["id"]) for t in channel.get("available_tags") or []}


def _role_payload(role: Role) -> Json:
    payload = {"name": role.name, "color": role.color, "hoist": role.hoist, "mentionable": role.mentionable}
    if role.permissions is not None:
        payload["permissions"] = str(int(role.permissions))

    return payload


def _shape(channel: Json, ours: set[str]) -> Json:
    """What a build sets on a channel, comparable between a live channel and a wanted payload.

    `ours` holds the ids of the blueprint's roles and @everyone: the only overwrites a build owns.
    """
    return {
        "name": channel["name"],
        "parent_id": str(channel["parent_id"]) if channel.get("parent_id") else None,
        "topic": channel.get("topic") or None,
        "nsfw": bool(channel.get("nsfw")),
        "slowmode": channel.get("rate_limit_per_user") or 0,
        "require_tag": bool((channel.get("flags") or 0) & REQUIRE_TAG),
        "tags": sorted(
            (t["name"], bool(t.get("moderated")), t.get("emoji_name") or None)
            for t in channel.get("available_tags") or []
        ),
        # A member, or a role the blueprint never names (a bot's own role), is staff's business.
        "overwrites": sorted(
            (str(o["id"]), int(o["type"]), int(o["allow"]), int(o["deny"]))
            for o in channel.get("permission_overwrites", [])
            if str(o["id"]) in ours
        ),
    }


def _patch(wanted: Json, current: Json, ours: set[str]) -> Json:
    """Edit payload: the type stays; tags keep the id of the live tag with the same name, so posts keep them;
    overwrites the build does not own, set by hand on Discord, ride along so the edit keeps them."""
    patch = {k: v for k, v in wanted.items() if k != "type"}
    theirs = [o for o in current.get("permission_overwrites", []) if str(o["id"]) not in ours]
    if theirs and "permission_overwrites" in patch:
        patch["permission_overwrites"] = patch["permission_overwrites"] + theirs

    if "available_tags" not in patch:
        return patch

    ids = {t["name"]: t["id"] for t in current.get("available_tags") or []}
    patch["available_tags"] = [({"id": ids[t["name"]]} if t["name"] in ids else {}) | t
                               for t in patch["available_tags"]]
    return patch


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _chunks(text: str, limit: int = config.CONTENT_LIMIT) -> list[str]:
    """Message-sized parts: broken at SPLIT lines, then packed by heading sections, paragraphs, lines.

    Sections stay whole when they fit, e.g. "# A\\n\\nx\\n\\n# B\\n\\ny" is one message,
    while a 5000-character section becomes three.
    """
    parts: list[str] = []
    for block in text.split(SPLIT):
        for piece in _EMBED_START.split(block):
            if _EMBED.match(piece):
                parts.append(piece.strip())
                continue

            sections = [s.strip() for s in _HEADING.split(piece.strip()) if s.strip()]
            parts += _pack([p for s in sections for p in _fit(s, limit)], "\n\n", limit)

    return parts


def _payload(part: str) -> Json:
    """A message for one part: an embed when the part opens with an embed line, else plain text.

    Each kind clears the other, so a message can switch between them on edit.
    """
    marker = _EMBED.match(part)
    if not marker:
        return {"content": part, "embeds": [], "allowed_mentions": NO_PINGS}

    title, _, description = part[marker.end():].strip().partition("\n")
    embed = {
        "title": title.strip(_TITLE_MARKS)[:config.EMBED_TITLE_LIMIT],
        "description": description.strip()[:config.EMBED_TEXT_LIMIT],
    }
    if marker.group(1):
        embed["color"] = int(marker.group(1), 16)

    return {"content": "", "embeds": [embed], "allowed_mentions": NO_PINGS}


def _size(text: str) -> int:
    """Length the strict way: UTF-16 code units, so an emoji counts 2."""
    return len(text.encode("utf-16-le")) // 2


def _fit(text: str, limit: int) -> list[str]:
    """Pieces of at most `limit`: whole if possible, else by paragraphs, else lines, else hard cuts."""
    if _size(text) <= limit:
        return [text]

    for joiner in ("\n\n", "\n"):
        units = [u for u in text.split(joiner) if u.strip()]
        if len(units) > 1:
            return _pack([piece for u in units for piece in _fit(u, limit)], joiner, limit)

    return _cut(text, limit)


def _cut(text: str, limit: int) -> list[str]:
    """Hard cuts for a single line longer than `limit`, never splitting a character."""
    pieces: list[str] = []
    start = size = 0
    for index, char in enumerate(text):
        width = _size(char)
        if size + width > limit:
            pieces.append(text[start:index])
            start, size = index, 0

        size += width

    pieces.append(text[start:])
    return pieces


def _pack(units: list[str], joiner: str, limit: int) -> list[str]:
    """Greedily join units with `joiner` into parts of at most `limit`."""
    parts: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}{joiner}{unit}" if current else unit
        if _size(candidate) <= limit:
            current = candidate
            continue

        if current:
            parts.append(current)

        current = unit

    if current:
        parts.append(current)

    return parts
