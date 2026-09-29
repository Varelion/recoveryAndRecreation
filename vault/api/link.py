"""Discord connection: REST session, slash-command tree and raw gateway feeds.

    Link ──login──► Rest (HTTP, rate limited)
      ├──connect──► gateway ──raw JSON──┬─► feed queue ──► watcher A
      │                                 └─► feed queue ──► watcher B
      └──tree─────► slash commands (published by sync_commands)

Each feed is a FIFO queue, so a consumer sees events in arrival order
(e.g. MESSAGE_CREATE before its MESSAGE_UPDATE).
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import discord
from discord import app_commands

from vault.api.rest import Rest

Event = tuple[str, dict[str, Any]]

_DISPATCH_OP = 0

log = logging.getLogger(__name__)


def _intents() -> discord.Intents:
    # Everything useful for analytics; presence and typing are noisy.
    intents = discord.Intents.all()
    intents.presences = False
    intents.typing = False
    return intents


class _Client(discord.Client):
    def __init__(self) -> None:
        super().__init__(
            intents=_intents(),
            enable_debug_events=True,
            max_messages=None,
            chunk_guilds_at_startup=False,
        )
        self.feeds: set[asyncio.Queue] = set()
        self.tree = app_commands.CommandTree(self)

    async def on_socket_raw_receive(self, msg: str | dict) -> None:
        # Raw payloads keep fields discord.py drops. No await before put: keeps order.
        payload = json.loads(msg) if isinstance(msg, str) else msg
        if payload.get("op") != _DISPATCH_OP:
            return

        for feed in self.feeds:
            feed.put_nowait((payload["t"], payload["d"]))


class Link:
    def __init__(self, token: str) -> None:
        self._token = token
        self._client = _Client()
        self.rest: Rest | None = None

    async def __aenter__(self) -> "Link":
        await self._client.login(self._token)
        self.rest = Rest(self._client.http)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.close()

    @property
    def tree(self) -> app_commands.CommandTree:
        return self._client.tree

    async def sync_commands(self) -> None:
        """Publish registered slash commands. Bulk overwrite: idempotent, one request."""
        await self._client.tree.sync()

    async def connect(self) -> None:
        """Run the gateway until closed; reconnects on drops."""
        await self._client.connect(reconnect=True)

    @asynccontextmanager
    async def feed(self) -> AsyncIterator[asyncio.Queue]:
        """Queue of (event type, raw data) for the lifetime of the context."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._client.feeds.add(queue)
        try:
            yield queue
        finally:
            self._client.feeds.discard(queue)
