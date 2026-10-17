"""Turn a guild into a Community, so `build` can create forum and announcement channels.

    guild ──► features has COMMUNITY?  yes ──► nothing to do
                      │ no
                      ▼
       rules + moderator-only text channels (created when missing)
                      │
                      ▼
       PATCH guild: features + COMMUNITY, the two channel ids, and the
       safety settings Discord demands (verification, media filter, mentions-only)
"""

from dataclasses import dataclass

from vault.api import Json, Rest
from vault.config import ChannelType, Mode

COMMUNITY = "COMMUNITY"
RULES, UPDATES = "rules", "moderator-only"

# Community prerequisites (spec): verification at least LOW, explicit media scanned
# for everyone, notifications for mentions only.
VERIFICATION_LOW = 1
FILTER_ALL_MEMBERS = 2
NOTIFY_MENTIONS_ONLY = 1


@dataclass(frozen=True)
class Report:
    community: str  # "already", "enabled" or "would enable"
    rules_channel: int | None
    updates_channel: int | None
    created: tuple[str, ...]

    def summary(self) -> Json:
        return {
            "community": self.community,
            "rules_channel": self.rules_channel,
            "updates_channel": self.updates_channel,
            "created": list(self.created),
        }


async def enable(rest: Rest, guild_id: int, mode: Mode) -> Report:
    guild = await rest.guild(guild_id)
    features = list(guild.get("features", []))
    channels = await rest.channels(guild_id)
    rules = _pick(channels, guild.get("rules_channel_id"), RULES)
    updates = _pick(channels, guild.get("public_updates_channel_id"), UPDATES)
    if COMMUNITY in features:
        return Report("already", rules, updates, ())

    created = tuple(name for name, found in ((RULES, rules), (UPDATES, updates)) if found is None)
    if mode is Mode.DRY_RUN:
        return Report("would enable", rules, updates, created)

    if rules is None:
        rules = await _text_channel(rest, guild_id, RULES)

    if updates is None:
        updates = await _text_channel(rest, guild_id, UPDATES)

    await rest.edit_guild(guild_id, {
        "features": features + [COMMUNITY],
        "rules_channel_id": str(rules),
        "public_updates_channel_id": str(updates),
        "verification_level": VERIFICATION_LOW,
        "explicit_content_filter": FILTER_ALL_MEMBERS,
        "default_message_notifications": NOTIFY_MENTIONS_ONLY,
    })
    return Report("enabled", rules, updates, created)


def _pick(channels: list[Json], configured: str | None, name: str) -> int | None:
    """The channel the guild already points at, else a text channel of that name."""
    if configured:
        return int(configured)

    for channel in channels:
        if channel["type"] == ChannelType.TEXT and channel["name"] == name:
            return int(channel["id"])

    return None


async def _text_channel(rest: Rest, guild_id: int, name: str) -> int:
    created = await rest.create_channel(guild_id, {"name": name, "type": ChannelType.TEXT})
    return int(created["id"])
