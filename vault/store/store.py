"""SQLite persistence layer.

Takes raw Discord JSON, writes normalized rows plus history. Services never
write SQL; they call these methods.
"""

import json
from collections.abc import AsyncIterator, Iterable
from enum import Enum
from pathlib import Path
from typing import Any

import aiosqlite

from vault.store.entities import (
    MESSAGE_VOLATILE, SPECS, Json, digest, emoji_key, message_row, now, snowflake_time, to_json,
)

_HERE = Path(__file__).parent


class Change(Enum):
    NEW = "new"
    CHANGED = "changed"
    SAME = "same"


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def open(cls, path: Path) -> "Store":
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.executescript((_HERE / "schema.sql").read_text(encoding="utf-8"))
        await db.executescript((_HERE / "views.sql").read_text(encoding="utf-8"))
        return cls(db)

    async def close(self) -> None:
        await self._db.commit()
        await self._db.close()

    async def commit(self) -> None:
        await self._db.commit()

    # ------------------------------------------------------------ runs & cursors

    async def begin_run(self, kind: str, guild_id: int) -> int:
        cur = await self._db.execute(
            "INSERT INTO runs (kind, guild_id, started_at, status) VALUES (?, ?, ?, 'running')",
            (kind, guild_id, now()),
        )
        await self._db.commit()
        return cur.lastrowid

    async def end_run(self, run_id: int, status: str, stats: Json) -> None:
        await self._db.execute(
            "UPDATE runs SET finished_at = ?, status = ?, stats = ? WHERE run_id = ?",
            (now(), status, to_json(stats), run_id),
        )
        await self._db.commit()

    async def cursor(self, scope: str, key: int) -> int:
        row = await self._one("SELECT value FROM cursors WHERE scope = ? AND key = ?", (scope, key))
        return row["value"] if row else 0

    async def set_cursor(self, scope: str, key: int, value: int) -> None:
        await self._db.execute(
            "INSERT INTO cursors (scope, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (scope, key, value, now()),
        )

    async def flagged(self, scope: str) -> list[int]:
        """Keys of a scope whose cursor value is 1 (on/off flags)."""
        rows = await self._all("SELECT key FROM cursors WHERE scope = ? AND value = 1", (scope,))
        return [r[0] for r in rows]

    # ------------------------------------------------------------ entities

    async def put(self, kind: str, raw: Json, guild_id: int) -> Change:
        """Upsert one entity; append history when its content changed."""
        spec = SPECS[kind]
        row = spec.extract(raw, guild_id)
        keys = tuple(row[k] for k in spec.keys)
        where = " AND ".join(f"{k} = ?" for k in spec.keys)
        stamp = now()
        new_hash = digest(raw, spec.volatile)

        old = await self._one(f"SELECT hash FROM {spec.table} WHERE {where}", keys)
        if old and old["hash"] == new_hash:
            # Unchanged, but may have come back (rejoined, unbanned then re-banned).
            await self._db.execute(f"UPDATE {spec.table} SET gone_at = NULL WHERE {where} AND gone_at IS NOT NULL", keys)
            return Change.SAME

        row |= {"raw": to_json(raw), "hash": new_hash, "updated_at": stamp, "gone_at": None}
        await self._upsert(spec.table, spec.keys, row)
        await self._db.execute(
            "INSERT INTO history (kind, entity_id, observed_at, raw) VALUES (?, ?, ?, ?)",
            (kind, ":".join(map(str, keys)), stamp, row["raw"]),
        )
        await self._children(kind, raw, row)
        return Change.CHANGED if old else Change.NEW

    async def put_many(self, kind: str, raws: Iterable[Json], guild_id: int) -> set[tuple]:
        """Upsert all; returns the keys seen (for mark_gone)."""
        spec = SPECS[kind]
        seen = set()
        for raw in raws:
            await self.put(kind, raw, guild_id)
            row = spec.extract(raw, guild_id)
            seen.add(tuple(row[k] for k in spec.keys))

        return seen

    async def mark_gone(self, kind: str, guild_id: int, seen: set[tuple], where: str = "1") -> int:
        """Flag entities of a full listing that were not seen. `where` narrows the listing scope."""
        spec = SPECS[kind]
        cols = ", ".join(spec.keys)
        rows = await self._all(
            f"SELECT {cols} FROM {spec.table} WHERE guild_id = ? AND gone_at IS NULL AND ({where})", (guild_id,)
        )
        missing = [tuple(r) for r in rows if tuple(r) not in seen]
        for keys in missing:
            await self.gone(kind, *keys)

        return len(missing)

    async def gone(self, kind: str, *keys: Any) -> None:
        spec = SPECS[kind]
        where = " AND ".join(f"{k} = ?" for k in spec.keys)
        await self._db.execute(f"UPDATE {spec.table} SET gone_at = ? WHERE {where} AND gone_at IS NULL", (now(), *keys))

    async def raw(self, kind: str, *keys: Any) -> Json | None:
        spec = SPECS[kind]
        where = " AND ".join(f"{k} = ?" for k in spec.keys)
        row = await self._one(f"SELECT raw FROM {spec.table} WHERE {where}", keys)
        return json.loads(row["raw"]) if row else None

    async def guild_stats(self, raw: Json) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO guild_stats VALUES (?, ?, ?, ?, ?)",
            (int(raw["id"]), now(), raw.get("approximate_member_count"),
             raw.get("approximate_presence_count"), raw.get("premium_subscription_count")),
        )

    async def _children(self, kind: str, raw: Json, row: Json) -> None:
        # Link tables rebuilt whenever the parent changes.
        if kind == "member":
            await self._db.execute("DELETE FROM member_roles WHERE guild_id = ? AND user_id = ?",
                                   (row["guild_id"], row["user_id"]))
            await self._db.executemany(
                "INSERT OR IGNORE INTO member_roles VALUES (?, ?, ?)",
                [(row["guild_id"], row["user_id"], int(r)) for r in raw.get("roles", [])],
            )
            await self.put("user", raw["user"], row["guild_id"])
            return

        if kind == "channel":
            await self._db.execute("DELETE FROM overwrites WHERE channel_id = ?", (row["channel_id"],))
            await self._db.executemany(
                "INSERT INTO overwrites VALUES (?, ?, ?, ?, ?)",
                [(row["channel_id"], int(o["id"]), o["type"], o["allow"], o["deny"])
                 for o in raw.get("permission_overwrites", [])],
            )
            return

        if kind == "ban":
            await self.put("user", raw["user"], row["guild_id"])

    # ------------------------------------------------------------ messages

    async def put_messages(self, guild_id: int, raws: Iterable[Json]) -> int:
        """Upsert messages with attachments, embeds, reactions, mentions. Returns changed count."""
        changed = 0
        for raw in raws:
            if await self._put_message(guild_id, raw) is not Change.SAME:
                changed += 1

        return changed

    async def _put_message(self, guild_id: int, raw: Json) -> Change:
        row = message_row(raw, guild_id)
        mid = row["message_id"]
        new_hash = digest(raw, MESSAGE_VOLATILE)

        old = await self._one("SELECT hash, content, edited_at, raw FROM messages WHERE message_id = ?", (mid,))
        if old and old["hash"] == new_hash:
            return Change.SAME

        # Keep the previous text when an edit replaced it.
        if old and (old["content"] != row["content"] or old["edited_at"] != row["edited_at"]):
            await self._db.execute(
                "INSERT INTO message_versions VALUES (?, ?, ?, ?, ?)",
                (mid, old["edited_at"], old["content"], now(), old["raw"]),
            )

        row |= {"raw": to_json(raw), "hash": new_hash, "updated_at": now(), "gone_at": None}
        await self._upsert("messages", ("message_id",), row)
        await self._message_children(guild_id, raw, mid)
        return Change.CHANGED if old else Change.NEW

    async def _message_children(self, guild_id: int, raw: Json, mid: int) -> None:
        author = raw.get("author")
        if author and not raw.get("webhook_id"):
            await self.put("user", author, guild_id)

        # Thread started from this message.
        if raw.get("thread"):
            await self.put("channel", raw["thread"], guild_id)

        # Attachments: update in place to keep sha256 of downloaded files.
        ids = []
        for a in raw.get("attachments", []):
            ids.append(int(a["id"]))
            await self._db.execute(
                "INSERT INTO attachments (attachment_id, message_id, filename, content_type, size, width, height, "
                "duration, description, url, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (attachment_id) DO UPDATE SET url = excluded.url, raw = excluded.raw, "
                "description = excluded.description",
                (int(a["id"]), mid, a.get("filename"), a.get("content_type"), a.get("size"), a.get("width"),
                 a.get("height"), a.get("duration_secs"), a.get("description"), a.get("url"), to_json(a)),
            )
        marks = ",".join("?" * len(ids)) or "NULL"
        await self._db.execute(f"DELETE FROM attachments WHERE message_id = ? AND attachment_id NOT IN ({marks})",
                               (mid, *ids))

        await self._db.execute("DELETE FROM embeds WHERE message_id = ?", (mid,))
        await self._db.executemany(
            "INSERT INTO embeds VALUES (?, ?, ?, ?, ?, ?)",
            [(mid, i, e.get("type"), e.get("title"), e.get("url"), to_json(e))
             for i, e in enumerate(raw.get("embeds", []))],
        )

        await self._db.execute("DELETE FROM reactions WHERE message_id = ?", (mid,))
        await self._db.executemany(
            "INSERT INTO reactions VALUES (?, ?, ?, ?, ?, ?)",
            [(mid, emoji_key(r["emoji"]), r["emoji"].get("id"), r["emoji"].get("name"), r.get("count"),
              (r.get("count_details") or {}).get("burst", 0))
             for r in raw.get("reactions", [])],
        )

        await self._db.execute("DELETE FROM mentions WHERE message_id = ?", (mid,))
        targets = [("user", int(u["id"])) for u in raw.get("mentions", [])]
        targets += [("role", int(r)) for r in raw.get("mention_roles", [])]
        targets += [("channel", int(c["id"])) for c in raw.get("mention_channels", [])]
        await self._db.executemany("INSERT OR IGNORE INTO mentions VALUES (?, ?, ?)",
                                   [(mid, kind, target) for kind, target in targets])
        for user in raw.get("mentions", []):
            await self.put("user", user, guild_id)

        await self._db.execute("DELETE FROM message_stickers WHERE message_id = ?", (mid,))
        await self._db.executemany(
            "INSERT OR IGNORE INTO message_stickers VALUES (?, ?, ?)",
            [(mid, int(s["id"]), s.get("name")) for s in raw.get("sticker_items", [])],
        )

    async def message_raw(self, message_id: int) -> Json | None:
        row = await self._one("SELECT raw FROM messages WHERE message_id = ?", (message_id,))
        return json.loads(row["raw"]) if row else None

    async def message_gone(self, message_ids: Iterable[int]) -> None:
        stamp = now()
        await self._db.executemany(
            "UPDATE messages SET gone_at = ? WHERE message_id = ? AND gone_at IS NULL",
            [(stamp, int(m)) for m in message_ids],
        )

    async def live_ids(self, channel_id: int, after: int, upto: int) -> set[int]:
        """Stored, not-deleted message ids in (after, upto]."""
        rows = await self._all(
            "SELECT message_id FROM messages WHERE channel_id = ? AND message_id > ? AND message_id <= ? "
            "AND gone_at IS NULL", (channel_id, after, upto),
        )
        return {r[0] for r in rows}

    async def reacted(self, channel_id: int, after: int) -> list[tuple[int, str, int]]:
        """(message_id, emoji_key, burst_count) of reactions on messages newer than `after`."""
        rows = await self._all(
            "SELECT r.message_id, r.emoji_key, r.burst_count FROM reactions r "
            "JOIN messages m ON m.message_id = r.message_id WHERE m.channel_id = ? AND m.message_id > ?",
            (channel_id, after),
        )
        return [tuple(r) for r in rows]

    async def put_reaction_users(self, message_id: int, key: str, burst: int, user_ids: Iterable[int]) -> None:
        await self._db.execute("DELETE FROM reaction_users WHERE message_id = ? AND emoji_key = ? AND burst = ?",
                               (message_id, key, burst))
        await self._db.executemany("INSERT OR IGNORE INTO reaction_users VALUES (?, ?, ?, ?)",
                                   [(message_id, key, u, burst) for u in user_ids])

    async def react(self, message_id: int, emoji: Json, user_id: int, burst: int, delta: int) -> None:
        """Apply one live reaction add (+1) or remove (-1)."""
        key = emoji_key(emoji)
        burst_delta = delta if burst else 0
        await self._db.execute(
            "INSERT INTO reactions VALUES (?, ?, ?, ?, max(?, 0), max(?, 0)) ON CONFLICT (message_id, emoji_key) "
            "DO UPDATE SET count = max(count + ?, 0), burst_count = max(burst_count + ?, 0)",
            (message_id, key, emoji.get("id"), emoji.get("name"), delta, burst_delta, delta, burst_delta),
        )
        if delta > 0:
            await self._db.execute("INSERT OR IGNORE INTO reaction_users VALUES (?, ?, ?, ?)",
                                   (message_id, key, user_id, burst))
            return

        await self._db.execute(
            "DELETE FROM reaction_users WHERE message_id = ? AND emoji_key = ? AND user_id = ? AND burst = ?",
            (message_id, key, user_id, burst),
        )

    async def clear_reactions(self, message_id: int, key: str | None) -> None:
        """Remove all reactions, or only one emoji when key is given."""
        scope, args = ("AND emoji_key = ?", (message_id, key)) if key else ("", (message_id,))
        await self._db.execute(f"DELETE FROM reactions WHERE message_id = ? {scope}", args)
        await self._db.execute(f"DELETE FROM reaction_users WHERE message_id = ? {scope}", args)

    # ------------------------------------------------------------ audit & gateway

    async def put_audit(self, guild_id: int, page: Json) -> int:
        """Store one audit-log page (entries + referenced users/threads/webhooks)."""
        for user in page.get("users", []):
            await self.put("user", user, guild_id)
        for thread in page.get("threads", []):
            await self.put("channel", thread, guild_id)
        for hook in page.get("webhooks", []):
            await self.put("webhook", hook, guild_id)

        entries = page.get("audit_log_entries", [])
        await self._db.executemany(
            "INSERT OR IGNORE INTO audit_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(int(e["id"]), guild_id, e["action_type"], _snowflake(e.get("user_id")),
              _snowflake(e.get("target_id")), e.get("reason"), snowflake_time(e["id"]), to_json(e.get("changes")),
              to_json(e.get("options")), to_json(e)) for e in entries],
        )
        return len(entries)

    async def log_event(self, kind: str, data: Json) -> None:
        user = data.get("user_id") or (data.get("user") or {}).get("id") or (data.get("author") or {}).get("id")
        await self._db.execute(
            "INSERT INTO gateway_events (guild_id, kind, at, channel_id, user_id, raw) VALUES (?, ?, ?, ?, ?, ?)",
            (_snowflake(data.get("guild_id")), kind, now(), _snowflake(data.get("channel_id")),
             _snowflake(user), to_json(data)),
        )

    # ------------------------------------------------------------ media

    async def pending_media(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT a.attachment_id, a.filename, a.url FROM attachments a "
            "JOIN messages m ON m.message_id = a.message_id WHERE m.guild_id = ? AND a.sha256 IS NULL",
            (guild_id,),
        )

    async def put_media(self, attachment_id: int, sha256: str, path: str, size: int) -> None:
        await self._db.execute("INSERT OR IGNORE INTO media VALUES (?, ?, ?)", (sha256, path, size))
        await self._db.execute("UPDATE attachments SET sha256 = ? WHERE attachment_id = ?", (sha256, attachment_id))

    # ------------------------------------------------------------ reads for import

    async def guild_rows(self, table: str, guild_id: int, order: str) -> list[Json]:
        """Live rows of a guild-scoped entity table, raw JSON decoded."""
        rows = await self._all(
            f"SELECT raw FROM {table} WHERE guild_id = ? AND gone_at IS NULL ORDER BY {order}", (guild_id,)
        )
        return [json.loads(r["raw"]) for r in rows]

    async def channel_messages(self, channel_id: int, after: int) -> AsyncIterator[Json]:
        """Live messages of a channel newer than `after`, oldest first."""
        async with self._db.execute(
            "SELECT raw FROM messages WHERE channel_id = ? AND message_id > ? AND gone_at IS NULL "
            "ORDER BY message_id", (channel_id, after),
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            yield json.loads(row["raw"])

    async def message_count(self, channel_id: int) -> int:
        """Live messages of a channel and its threads."""
        row = await self._one(
            "SELECT count(*) FROM messages WHERE gone_at IS NULL AND (channel_id = ? OR channel_id IN "
            "(SELECT channel_id FROM channels WHERE parent_id = ?))", (channel_id, channel_id),
        )
        return row[0]

    async def imported_count(self, target_guild: int, channel_id: int) -> int:
        """Messages of a channel and its threads already imported into target_guild."""
        row = await self._one(
            "SELECT count(*) FROM import_map x JOIN messages m ON m.message_id = x.source_id "
            "WHERE x.target_guild = ? AND x.kind = 'message' AND m.gone_at IS NULL AND (m.channel_id = ? "
            "OR m.channel_id IN (SELECT channel_id FROM channels WHERE parent_id = ?))",
            (target_guild, channel_id, channel_id),
        )
        return row[0]

    async def media_path(self, attachment_id: int) -> str | None:
        row = await self._one(
            "SELECT m.path FROM attachments a JOIN media m ON m.sha256 = a.sha256 WHERE a.attachment_id = ?",
            (attachment_id,),
        )
        return row["path"] if row else None

    async def mapped(self, target_guild: int, kind: str, source_id: int) -> aiosqlite.Row | None:
        return await self._one(
            "SELECT target_id, extra FROM import_map WHERE target_guild = ? AND kind = ? AND source_id = ?",
            (target_guild, kind, source_id),
        )

    async def map(self, target_guild: int, kind: str, source_id: int, target_id: int, extra: str | None = None) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO import_map VALUES (?, ?, ?, ?, ?)",
            (target_guild, kind, source_id, target_id, extra),
        )
        await self._db.commit()

    async def last_mapped(self, target_guild: int, channel_id: int) -> int:
        """Newest source message already imported for a channel (resume point)."""
        row = await self._one(
            "SELECT max(x.source_id) AS last FROM import_map x JOIN messages m ON m.message_id = x.source_id "
            "WHERE x.target_guild = ? AND x.kind = 'message' AND m.channel_id = ?",
            (target_guild, channel_id),
        )
        return row["last"] or 0

    # ------------------------------------------------------------ dump & stats

    async def tables(self) -> list[str]:
        rows = await self._all("SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                               "AND name NOT LIKE 'sqlite_%' ORDER BY type, name")
        return [r[0] for r in rows]

    async def scan(self, table: str) -> AsyncIterator[tuple[list[str], tuple]]:
        """(column names, row) for every row of a table or view."""
        async with self._db.execute(f'SELECT * FROM "{table}"') as cur:
            names = [d[0] for d in cur.description]
            async for row in cur:
                yield names, tuple(row)

    async def counts(self, guild_id: int | None) -> Json:
        tables = ["messages", "channels", "members", "users", "roles", "attachments", "reactions",
                  "audit_log", "gateway_events", "message_versions", "history"]
        out = {}
        for table in tables:
            scoped = guild_id is not None and table not in ("users", "reactions", "attachments",
                                                            "message_versions", "history")
            sql = f"SELECT count(*) FROM {table}" + (" WHERE guild_id = ?" if scoped else "")
            row = await self._one(sql, (guild_id,) if scoped else ())
            out[table] = row[0]

        return out

    # ------------------------------------------------------------ helpers

    async def _upsert(self, table: str, keys: tuple[str, ...], row: Json) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        updates = ", ".join(f"{c} = excluded.{c}" for c in row if c not in keys)
        await self._db.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    async def _one(self, sql: str, args: tuple = ()) -> aiosqlite.Row | None:
        async with self._db.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, args: tuple = ()) -> list[aiosqlite.Row]:
        async with self._db.execute(sql, args) as cur:
            return await cur.fetchall()


def _snowflake(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None
