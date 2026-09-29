"""Settings, Discord spec constants and CLI option enums."""

import os
from enum import Enum, IntEnum
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TOKEN_ENV = "DISCORD_TOKEN"
DEFAULT_DB = Path("vault.db")
MEDIA_DIR = "media"


def token() -> str:
    value = os.getenv(TOKEN_ENV)
    if not value:
        raise SystemExit(f"Set {TOKEN_ENV} in the environment or .env")

    return value


# Discord API page limits (spec maximums).
MESSAGE_PAGE = 100
MEMBER_PAGE = 1000
AUDIT_PAGE = 100
BAN_PAGE = 1000
THREAD_PAGE = 100
REACTION_PAGE = 100

# Discord message limits (spec).
CONTENT_LIMIT = 2000
EMBED_LIMIT = 10
WEBHOOK_NAME_LIMIT = 80

CDN = "https://cdn.discordapp.com"

# Transient network drops: retries for idempotent reads, doubling delay (s).
NET_RETRIES = 10
NET_RETRY_DELAY = 2.0


class ChannelType(IntEnum):
    TEXT = 0
    VOICE = 2
    CATEGORY = 4
    ANNOUNCEMENT = 5
    ANNOUNCEMENT_THREAD = 10
    PUBLIC_THREAD = 11
    PRIVATE_THREAD = 12
    STAGE = 13
    DIRECTORY = 14
    FORUM = 15
    MEDIA = 16


THREAD_TYPES = {
    ChannelType.ANNOUNCEMENT_THREAD,
    ChannelType.PUBLIC_THREAD,
    ChannelType.PRIVATE_THREAD,
}

# Channels whose threads (archived included) must be listed explicitly.
THREAD_PARENTS = {
    ChannelType.TEXT,
    ChannelType.ANNOUNCEMENT,
    ChannelType.FORUM,
    ChannelType.MEDIA,
}

# Channels holding messages directly (voice/stage have text chat).
MESSAGE_HOLDERS = {
    ChannelType.TEXT,
    ChannelType.ANNOUNCEMENT,
    ChannelType.VOICE,
    ChannelType.STAGE,
} | THREAD_TYPES


class Media(Enum):
    SKIP = "skip"
    DOWNLOAD = "download"


class ReactionUsers(Enum):
    SKIP = "skip"
    FETCH = "fetch"


class Messages(Enum):
    INCLUDE = "include"
    SKIP = "skip"


class Mode(Enum):
    LIVE = "live"
    DRY_RUN = "dry-run"
