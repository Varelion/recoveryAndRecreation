"""Recover a guild from one of its backups: recreate only what went missing.

    backup.db ──clone──► working copy (import_map is written there; the backup stays read-only)
                              │
    live guild ── roles, channels ──► present? same id, or same name (+ type + parent for channels)
                              │         yes: map stored id ─► live id, never touched
                              ▼         no:  missing
              missing roles ─► categories ─► channels (overwrites, parent remapped) ─► order
                            ─► messages of restored channels via webhook (like import)

Reuses the Importer with source = target = the guild: present objects are
pre-mapped, so its creation and replay skip them. Messages lost from
channels that still exist are not reposted: they would land out of order.
"""

import logging
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from vault import config
from vault.api import Json, Rejected, Rest
from vault.config import Messages, Mode
from vault.services.importer import CHANNEL, PHASES, ROLE, ImportOptions, Importer
from vault.services.progress import Progress
from vault.store import Store

log = logging.getLogger(__name__)

KIND = "restore"
WORK_DB = "restore.db"
ROLE_OVERWRITE = 0  # Discord overwrite type: 0 role, 1 member


class UnknownGuild(Exception):
    """The backup holds no data for that guild."""


@dataclass
class RestoreOptions:
    mode: Mode = Mode.LIVE
    messages: Messages = Messages.INCLUDE
    channels: set[int] = field(default_factory=set)  # channels/categories to restore; empty = all


async def restore(rest: Rest, backup: Path, guild_id: int, options: RestoreOptions,
                  progress: Progress | None = None) -> Json:
    """Restore `guild_id` from `backup` into itself; returns counts + names of what was missing."""
    with tempfile.TemporaryDirectory() as tmp:
        store = await Store.clone(backup, Path(tmp) / WORK_DB)
        try:
            if await store.raw("guild", guild_id) is None:
                raise UnknownGuild(f"{backup} holds no guild {guild_id}")

            # Mappings must come from the live comparison only, never from an earlier import.
            await store.forget(guild_id)
            return await _Restorer(rest, store, guild_id, options, progress).run()
        finally:
            await store.close()


class _Restorer(Importer):
    def __init__(self, rest: Rest, store: Store, guild_id: int, options: RestoreOptions,
                 progress: Progress | None) -> None:
        imports = ImportOptions(messages=options.messages, mode=options.mode, channels=options.channels)
        super().__init__(rest, store, guild_id, guild_id, imports, progress or Progress(KIND, PHASES))
        self._missing: dict[str, list[str]] = {"roles": [], "channels": []}

    async def run(self) -> Json:
        report = await super().run()
        return report | {"missing": self._missing}

    async def _guild(self) -> None:
        roles = await self._lost_roles(await self._rest.roles(self._target))
        channels = await self._lost_channels(await self._rest.channels(self._target))

        # A partial restore brings back only the roles its channels' overwrites need.
        if self._opts.channels:
            roles = _needed(roles, channels)

        self._missing = {"roles": [r["name"] for r in roles], "channels": [c["name"] for c in channels]}
        self._progress.phase("roles", len(roles))
        for role in roles:
            self._progress.advance()
            await self._role(role)

        await self._place_roles(roles)
        await self._channels(channels)
        await self._place_channels(channels)
        if self._opts.messages is Messages.INCLUDE:
            await self._all_messages(channels)

    # ------------------------------------------------------------ live comparison

    async def _lost_roles(self, live: list[Json]) -> list[Json]:
        """Map stored roles still present to their live id; return the missing ones."""
        ids = {r["id"]: r for r in live}
        names = {r["name"]: r for r in live if not r.get("managed")}
        lost = []
        for role in await self._store.guild_rows("roles", self._source, "position DESC"):
            managed = role.get("managed")
            present = ids.get(role["id"]) or (None if managed else names.get(role["name"]))
            if present:
                await self._store.map(self._target, ROLE, int(role["id"]), int(present["id"]))
                continue

            # Bot/integration roles are owned by Discord.
            if not managed:
                lost.append(role)

        return lost

    async def _lost_channels(self, live: list[Json]) -> list[Json]:
        """Map stored channels still present to their live id; return the wanted missing ones.

        Categories come first, so a child's parent is mapped before the child is matched.
        """
        ids = {c["id"]: c for c in live}
        keys = {_key(c["name"], c["type"], c.get("parent_id")): c for c in live}
        stored = await self._store.guild_rows("channels", self._source, "type = 4 DESC, position")
        parents = {c.get("parent_id") for c in stored if self._wanted(c)}
        lost = []
        for channel in stored:
            if channel["type"] in config.THREAD_TYPES:
                continue

            present = ids.get(channel["id"]) or await self._namesake(channel, keys)
            if present:
                # Mapped even when out of scope: a restored child needs its live parent.
                await self._store.map(self._target, CHANNEL, int(channel["id"]), int(present["id"]),
                                      str(present["type"]))
                continue

            # A lost category of a wanted channel comes back too, so the channel is not orphaned.
            if self._wanted(channel) or channel["id"] in parents:
                lost.append(channel)

        # Put those categories in scope only now: their other children stay out.
        if self._opts.channels:
            self._opts.channels |= {int(c["id"]) for c in lost}

        return lost

    async def _namesake(self, channel: Json, keys: dict[tuple, Json]) -> Json | None:
        """Live channel with the same name and type under the same live parent, e.g. remade by hand."""
        parent = channel.get("parent_id")
        live_parent = await self._target_id(CHANNEL, parent)
        if parent and live_parent is None:
            return None

        return keys.get(_key(channel["name"], channel["type"], live_parent))

    # ------------------------------------------------------------ order

    async def _place_roles(self, roles: list[Json]) -> None:
        """Put recreated roles back at their stored position: Discord creates them at the bottom."""
        positions = await self._positions(ROLE, roles)
        if not positions:
            return

        try:
            await self._rest.order_roles(self._target, positions)
        except Rejected as exc:
            # The bot cannot move roles at or above its own.
            log.warning("Restored roles left unordered: %s", exc)
            self._stats["roles_unordered"] += len(positions)

    async def _place_channels(self, channels: list[Json]) -> None:
        positions = await self._positions(CHANNEL, channels)
        if not positions:
            return

        await self._rest.order_channels(self._target, positions)

    async def _positions(self, kind: str, rows: list[Json]) -> list[Json]:
        """Stored positions of the rows created this run, keyed by their new id."""
        if self._dry:
            return []

        positions = []
        for row in rows:
            created = await self._target_id(kind, row["id"])
            if created is not None:
                positions.append({"id": str(created), "position": row["position"]})

        return positions


def _key(name: str, kind: int, parent: int | str | None) -> tuple:
    return name, kind, int(parent) if parent else None


def _needed(roles: list[Json], channels: Iterable[Json]) -> list[Json]:
    """Roles referenced by the role overwrites of `channels`."""
    used = {ow["id"] for c in channels for ow in c.get("permission_overwrites", []) if ow["type"] == ROLE_OVERWRITE}
    return [r for r in roles if r["id"] in used]
