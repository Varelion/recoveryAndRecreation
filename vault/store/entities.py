"""Mapping of raw Discord JSON to table columns, one spec per entity kind."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

Json = dict[str, Any]

DISCORD_EPOCH_MS = 1420070400000
SNOWFLAKE_TIME_SHIFT = 22


def snowflake_time(snowflake: int | str | None) -> str | None:
    """Creation time embedded in a snowflake, e.g. 175928847299117063 -> 2016-04-30T11:18:25.796+00:00."""
    if snowflake is None:
        return None

    ms = (int(snowflake) >> SNOWFLAKE_TIME_SHIFT) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def to_json(value: Any) -> str | None:
    if value is None:
        return None

    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def digest(raw: Json, volatile: tuple[str, ...] = ()) -> str:
    # Stable hash; volatile keys (counters, cursors) excluded so they do not spam history.
    stable = {k: v for k, v in raw.items() if k not in volatile}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _int(value: Any) -> int | None:
    if value is None:
        return None

    return int(value)


def _id(raw: Json | None) -> int | None:
    if not raw:
        return None

    return _int(raw.get("id"))


def emoji_key(emoji: Json) -> str:
    """Reaction key usable in REST paths: "name:id" for custom, the char for unicode."""
    if emoji.get("id"):
        return f"{emoji.get('name') or '_'}:{emoji['id']}"

    return emoji["name"]


@dataclass(frozen=True)
class Spec:
    table: str
    keys: tuple[str, ...]
    extract: Callable[[Json, int], Json]  # (raw, guild_id) -> columns incl. keys
    volatile: tuple[str, ...] = ()
    guild_scoped: bool = True  # has guild_id column


def _guild(r: Json, _: int) -> Json:
    return {
        "guild_id": int(r["id"]), "name": r.get("name"), "owner_id": _int(r.get("owner_id")),
        "created_at": snowflake_time(r["id"]), "premium_tier": r.get("premium_tier"),
        "features": to_json(r.get("features")),
    }


def _user(r: Json, _: int) -> Json:
    return {
        "user_id": int(r["id"]), "username": r.get("username"), "global_name": r.get("global_name"),
        "bot": int(bool(r.get("bot"))), "avatar": r.get("avatar"), "created_at": snowflake_time(r["id"]),
    }


def _member(r: Json, g: int) -> Json:
    return {
        "guild_id": g, "user_id": int(r["user"]["id"]), "nick": r.get("nick"),
        "joined_at": r.get("joined_at"), "premium_since": r.get("premium_since"),
        "timeout_until": r.get("communication_disabled_until"),
        "pending": int(bool(r.get("pending"))), "flags": r.get("flags"),
    }


def _role(r: Json, g: int) -> Json:
    return {
        "role_id": int(r["id"]), "guild_id": g, "name": r.get("name"), "color": r.get("color"),
        "position": r.get("position"), "permissions": r.get("permissions"),
        "hoist": int(bool(r.get("hoist"))), "mentionable": int(bool(r.get("mentionable"))),
        "managed": int(bool(r.get("managed"))), "unicode_emoji": r.get("unicode_emoji"),
        "created_at": snowflake_time(r["id"]),
    }


def _channel(r: Json, g: int) -> Json:
    meta = r.get("thread_metadata") or {}
    return {
        "channel_id": int(r["id"]), "guild_id": g, "type": r["type"], "name": r.get("name"),
        "parent_id": _int(r.get("parent_id")), "position": r.get("position"), "topic": r.get("topic"),
        "nsfw": int(bool(r.get("nsfw"))), "owner_id": _int(r.get("owner_id")),
        "archived": int(bool(meta.get("archived"))), "locked": int(bool(meta.get("locked"))),
        "message_count": r.get("message_count"), "created_at": snowflake_time(r["id"]),
    }


def _emoji(r: Json, g: int) -> Json:
    return {
        "emoji_id": int(r["id"]), "guild_id": g, "name": r.get("name"),
        "animated": int(bool(r.get("animated"))), "creator_id": _id(r.get("user")),
        "created_at": snowflake_time(r["id"]),
    }


def _sticker(r: Json, g: int) -> Json:
    return {
        "sticker_id": int(r["id"]), "guild_id": g, "name": r.get("name"), "tags": r.get("tags"),
        "format_type": r.get("format_type"), "creator_id": _id(r.get("user")),
    }


def _event(r: Json, g: int) -> Json:
    return {
        "event_id": int(r["id"]), "guild_id": g, "channel_id": _int(r.get("channel_id")),
        "creator_id": _int(r.get("creator_id")), "name": r.get("name"),
        "start_at": r.get("scheduled_start_time"), "end_at": r.get("scheduled_end_time"),
        "status": r.get("status"), "user_count": r.get("user_count"),
    }


def _automod(r: Json, g: int) -> Json:
    return {
        "rule_id": int(r["id"]), "guild_id": g, "name": r.get("name"),
        "event_type": r.get("event_type"), "enabled": int(bool(r.get("enabled"))),
    }


def _invite(r: Json, g: int) -> Json:
    return {
        "code": r["code"], "guild_id": g, "channel_id": _id(r.get("channel")) or _int(r.get("channel_id")),
        "inviter_id": _id(r.get("inviter")), "uses": r.get("uses"), "max_uses": r.get("max_uses"),
        "created_at": r.get("created_at"), "expires_at": r.get("expires_at"),
    }


def _webhook(r: Json, g: int) -> Json:
    return {
        "webhook_id": int(r["id"]), "guild_id": g, "channel_id": _int(r.get("channel_id")),
        "name": r.get("name"), "creator_id": _id(r.get("user")),
    }


def _ban(r: Json, g: int) -> Json:
    return {"guild_id": g, "user_id": int(r["user"]["id"]), "reason": r.get("reason")}


SPECS: dict[str, Spec] = {
    "guild": Spec("guilds", ("guild_id",), _guild,
                  volatile=("approximate_member_count", "approximate_presence_count",
                            "premium_subscription_count", "member_count")),
    "user": Spec("users", ("user_id",), _user, guild_scoped=False),
    "member": Spec("members", ("guild_id", "user_id"), _member),
    "role": Spec("roles", ("role_id",), _role),
    "channel": Spec("channels", ("channel_id",), _channel,
                    volatile=("last_message_id", "last_pin_timestamp", "message_count",
                              "total_message_sent", "member_count", "member", "guild_id")),
    "emoji": Spec("emojis", ("emoji_id",), _emoji),
    "sticker": Spec("stickers", ("sticker_id",), _sticker),
    "event": Spec("scheduled_events", ("event_id",), _event, volatile=("user_count",)),
    "automod": Spec("automod_rules", ("rule_id",), _automod),
    "invite": Spec("invites", ("code",), _invite, volatile=("uses",)),
    "webhook": Spec("webhooks", ("webhook_id",), _webhook, volatile=("token",)),
    "ban": Spec("bans", ("guild_id", "user_id"), _ban),
}


def message_row(r: Json, guild_id: int) -> Json:
    ref = r.get("message_reference") or {}
    meta = r.get("interaction_metadata") or r.get("interaction") or {}
    return {
        "message_id": int(r["id"]), "guild_id": guild_id, "channel_id": int(r["channel_id"]),
        "author_id": _id(r.get("author")), "webhook_id": _int(r.get("webhook_id")),
        "application_id": _int(r.get("application_id")), "type": r.get("type"),
        "content": r.get("content"), "created_at": r.get("timestamp") or snowflake_time(r["id"]),
        "edited_at": r.get("edited_timestamp"), "pinned": int(bool(r.get("pinned"))),
        "tts": int(bool(r.get("tts"))), "mention_everyone": int(bool(r.get("mention_everyone"))),
        "flags": r.get("flags"), "ref_message_id": _int(ref.get("message_id")),
        "ref_channel_id": _int(ref.get("channel_id")), "thread_id": _id(r.get("thread")),
        "interaction_user": _id(meta.get("user")), "interaction_name": meta.get("name"),
        "poll": to_json(r.get("poll")),
    }


# Gateway-only extras; ignored so gateway and REST copies hash equal.
MESSAGE_VOLATILE = ("member", "guild_id")
