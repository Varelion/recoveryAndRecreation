"""Discord REST driver.

Returns raw Discord JSON so every field Discord sends is kept, including
ones discord.py does not model. Rate limits and retries are delegated to
discord.py's HTTPClient.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import discord
from discord.http import HTTPClient, Route

from vault import config

Json = dict[str, Any]
log = logging.getLogger(__name__)

_BODY_KEYS = ("json", "form", "reason")
HTTP_BAD_REQUEST = 400
SAFE_METHOD = "GET"

# Connection lost before a response; the request may never have arrived.
_NET_ERRORS = (aiohttp.ClientConnectionError, asyncio.TimeoutError)


class Denied(Exception):
    """Bot lacks permission (HTTP 403)."""


class Missing(Exception):
    """Resource gone (HTTP 404)."""


class Rejected(Exception):
    """Request refused as invalid (HTTP 400), e.g. feature not enabled."""


class Rest:
    def __init__(self, http: HTTPClient) -> None:
        self._http = http

    # ------------------------------------------------------------ transport

    async def _call(self, method: str, path: str, *, params: Json | None = None, **kwargs: Any) -> Any:
        # kwargs split: request body options vs. path placeholders.
        body = {k: v for k, v in kwargs.items() if k in _BODY_KEYS}
        route = Route(method, path, **{k: v for k, v in kwargs.items() if k not in _BODY_KEYS})

        # Drop unset query values; Discord rejects "None".
        query = {k: v for k, v in (params or {}).items() if v is not None}

        try:
            return await self._request(route, query, body)
        except discord.Forbidden as exc:
            raise Denied(f"{method} {path}: {exc.text}") from exc
        except discord.NotFound as exc:
            raise Missing(f"{method} {path}: {exc.text}") from exc
        except discord.HTTPException as exc:
            if exc.status != HTTP_BAD_REQUEST:
                raise
            raise Rejected(f"{method} {path}: {exc.text}") from exc

    async def _request(self, route: Route, query: Json, body: Json) -> Any:
        # Retry reads across network drops (e.g. WinError 64); writes are
        # not retried since the server may have applied them already.
        delay = config.NET_RETRY_DELAY
        for _ in range(config.NET_RETRIES):
            try:
                return await self._http.request(route, params=query, **body)
            except _NET_ERRORS as exc:
                if route.method != SAFE_METHOD:
                    raise

                log.warning("%s %s: %r, retrying in %.0fs", route.method, route.path, exc, delay)

            await asyncio.sleep(delay)
            delay *= 2

        return await self._http.request(route, params=query, **body)

    async def cdn(self, url: str) -> bytes:
        try:
            return await self._http.get_from_cdn(url)
        except discord.NotFound as exc:
            raise Missing(url) from exc
        except discord.Forbidden as exc:
            raise Denied(url) from exc

    # ------------------------------------------------------------ guild

    async def me(self) -> Json:
        return await self._call("GET", "/users/@me")

    async def guild(self, guild_id: int) -> Json:
        return await self._call("GET", "/guilds/{guild_id}", guild_id=guild_id, params={"with_counts": "true"})

    async def channels(self, guild_id: int) -> list[Json]:
        return await self._call("GET", "/guilds/{guild_id}/channels", guild_id=guild_id)

    async def roles(self, guild_id: int) -> list[Json]:
        return await self._call("GET", "/guilds/{guild_id}/roles", guild_id=guild_id)

    async def emojis(self, guild_id: int) -> list[Json]:
        return await self._call("GET", "/guilds/{guild_id}/emojis", guild_id=guild_id)

    async def stickers(self, guild_id: int) -> list[Json]:
        return await self._call("GET", "/guilds/{guild_id}/stickers", guild_id=guild_id)

    async def events(self, guild_id: int) -> list[Json]:
        return await self._call(
            "GET", "/guilds/{guild_id}/scheduled-events", guild_id=guild_id, params={"with_user_count": "true"}
        )

    async def automod(self, guild_id: int) -> list[Json]:
        return await self._call("GET", "/guilds/{guild_id}/auto-moderation/rules", guild_id=guild_id)

    async def invites(self, guild_id: int) -> list[Json]:
        return await self._call("GET", "/guilds/{guild_id}/invites", guild_id=guild_id)

    async def webhooks(self, guild_id: int) -> list[Json]:
        return await self._call("GET", "/guilds/{guild_id}/webhooks", guild_id=guild_id)

    async def members(self, guild_id: int) -> AsyncIterator[Json]:
        # Snowflake cursor pagination, ascending user id.
        after = 0
        while True:
            page = await self._call(
                "GET", "/guilds/{guild_id}/members", guild_id=guild_id,
                params={"limit": config.MEMBER_PAGE, "after": after},
            )
            for member in page:
                yield member

            if len(page) < config.MEMBER_PAGE:
                return

            after = max(int(m["user"]["id"]) for m in page)

    async def bans(self, guild_id: int) -> AsyncIterator[Json]:
        after = 0
        while True:
            page = await self._call(
                "GET", "/guilds/{guild_id}/bans", guild_id=guild_id,
                params={"limit": config.BAN_PAGE, "after": after},
            )
            for ban in page:
                yield ban

            if len(page) < config.BAN_PAGE:
                return

            after = max(int(b["user"]["id"]) for b in page)

    async def audit(self, guild_id: int, after: int) -> AsyncIterator[Json]:
        """Audit log entries newer than `after`, newest first.

        Pages walk backwards with `before` until reaching `after`, since
        Discord's ordering for `after` queries is not documented.
        Each yielded page is the full response (entries, users, threads...).
        """
        before = None
        while True:
            page = await self._call(
                "GET", "/guilds/{guild_id}/audit-logs", guild_id=guild_id,
                params={"limit": config.AUDIT_PAGE, "before": before},
            )
            entries = [e for e in page["audit_log_entries"] if int(e["id"]) > after]
            page["audit_log_entries"] = entries
            yield page

            if len(entries) < config.AUDIT_PAGE:
                return

            before = min(int(e["id"]) for e in entries)

    # ------------------------------------------------------------ threads

    async def active_threads(self, guild_id: int) -> Json:
        return await self._call("GET", "/guilds/{guild_id}/threads/active", guild_id=guild_id)

    async def archived_threads(self, channel_id: int, scope: str) -> AsyncIterator[Json]:
        """Archived threads of a channel. `scope` is "public" or "private"."""
        before = None
        while True:
            page = await self._call(
                "GET", f"/channels/{{channel_id}}/threads/archived/{scope}", channel_id=channel_id,
                params={"limit": config.THREAD_PAGE, "before": before},
            )
            for thread in page["threads"]:
                yield thread

            if not page.get("has_more") or not page["threads"]:
                return

            before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]

    # ------------------------------------------------------------ messages

    async def messages(self, channel_id: int, after: int) -> AsyncIterator[list[Json]]:
        """Pages of messages newer than `after`, oldest page first, each sorted ascending.

        after=0 walks the whole history from channel creation.
        """
        while True:
            page = await self._call(
                "GET", "/channels/{channel_id}/messages", channel_id=channel_id,
                params={"limit": config.MESSAGE_PAGE, "after": after},
            )
            if not page:
                return

            page.sort(key=lambda m: int(m["id"]))
            yield page

            if len(page) < config.MESSAGE_PAGE:
                return

            after = int(page[-1]["id"])

    async def reaction_users(self, channel_id: int, message_id: int, emoji: str, burst: int) -> AsyncIterator[Json]:
        """Users behind one reaction. burst=1 lists super reactions."""
        after = 0
        while True:
            page = await self._call(
                "GET", "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}",
                channel_id=channel_id, message_id=message_id, emoji=emoji,
                params={"limit": config.REACTION_PAGE, "after": after, "type": burst},
            )
            for user in page:
                yield user

            if len(page) < config.REACTION_PAGE:
                return

            after = max(int(u["id"]) for u in page)

    # ------------------------------------------------------------ writes

    async def create_role(self, guild_id: int, payload: Json) -> Json:
        return await self._call("POST", "/guilds/{guild_id}/roles", guild_id=guild_id, json=payload)

    async def edit_role(self, guild_id: int, role_id: int, payload: Json) -> Json:
        return await self._call(
            "PATCH", "/guilds/{guild_id}/roles/{role_id}", guild_id=guild_id, role_id=role_id, json=payload
        )

    async def order_roles(self, guild_id: int, positions: list[Json]) -> list[Json]:
        return await self._call("PATCH", "/guilds/{guild_id}/roles", guild_id=guild_id, json=positions)

    async def create_channel(self, guild_id: int, payload: Json) -> Json:
        return await self._call("POST", "/guilds/{guild_id}/channels", guild_id=guild_id, json=payload)

    async def create_thread(self, channel_id: int, name: str) -> Json:
        """Public thread without a starter message."""
        return await self._call(
            "POST", "/channels/{channel_id}/threads", channel_id=channel_id,
            json={"name": name, "type": config.ChannelType.PUBLIC_THREAD},
        )

    async def create_emoji(self, guild_id: int, name: str, image_uri: str) -> Json:
        return await self._call(
            "POST", "/guilds/{guild_id}/emojis", guild_id=guild_id, json={"name": name, "image": image_uri}
        )

    async def create_webhook(self, channel_id: int, name: str) -> Json:
        return await self._call("POST", "/channels/{channel_id}/webhooks", channel_id=channel_id, json={"name": name})

    async def execute_webhook(
        self, webhook_id: int, webhook_token: str, payload: Json,
        files: list[tuple[str, bytes]], thread_id: int | None,
    ) -> Json:
        """Send as webhook. files: (filename, bytes). Returns the created message."""
        form = [{"name": "payload_json", "value": json.dumps(payload)}]
        for index, (name, data) in enumerate(files):
            form.append({
                "name": f"files[{index}]", "value": data,
                "filename": name, "content_type": "application/octet-stream",
            })

        return await self._call(
            "POST", "/webhooks/{webhook_id}/{webhook_token}",
            webhook_id=webhook_id, webhook_token=webhook_token, form=form,
            params={"wait": "true", "thread_id": thread_id},
        )

    async def send_message(self, channel_id: int, payload: Json) -> Json:
        return await self._call("POST", "/channels/{channel_id}/messages", channel_id=channel_id, json=payload)

    async def edit_message(self, channel_id: int, message_id: int, payload: Json) -> Json:
        return await self._call(
            "PATCH", "/channels/{channel_id}/messages/{message_id}",
            channel_id=channel_id, message_id=message_id, json=payload,
        )

    async def pin(self, channel_id: int, message_id: int) -> None:
        await self._call(
            "PUT", "/channels/{channel_id}/pins/{message_id}", channel_id=channel_id, message_id=message_id
        )
