"""In-memory stand-in for vault.api.Rest."""

import json

from vault.api import Denied, Missing, Rejected

GUILD = 1000
TEXT = 10
FORUM = 20
POST = 21
ROLE = 30
USER = 40


def user(uid: int, name: str = "ann") -> dict:
    return {"id": str(uid), "username": name, "global_name": name.title(), "avatar": None}


def message(mid: int, channel: int, content: str = "hi", **extra) -> dict:
    return {
        "id": str(mid), "channel_id": str(channel), "type": 0, "content": content,
        "author": user(USER), "timestamp": "2024-01-01T00:00:00+00:00", "edited_timestamp": None,
        "attachments": [], "embeds": [], "mentions": [], "mention_roles": [], "pinned": False,
    } | extra


class FakeRest:
    def __init__(self) -> None:
        self.channels_data = [
            {"id": str(TEXT), "type": 0, "name": "general", "position": 0, "permission_overwrites": [
                {"id": str(ROLE), "type": 0, "allow": "1024", "deny": "0"}]},
            {"id": str(FORUM), "type": 15, "name": "ideas", "position": 1, "permission_overwrites": []},
        ]
        self.threads = [{"id": str(POST), "type": 11, "name": "post", "parent_id": str(FORUM),
                         "thread_metadata": {"archived": False}}]
        self.msgs: dict[int, list[dict]] = {TEXT: [], POST: []}
        self.roles_data = [
            {"id": str(GUILD), "name": "@everyone", "permissions": "0", "position": 0},
            {"id": str(ROLE), "name": "mod", "permissions": "8", "position": 1},
        ]
        self.emojis_data: list[dict] = []
        self.emoji_slots = 0  # free emoji slots in the target; 0 = full
        self.role_icons = True  # target boosted enough for role icons
        self.target_types: dict[int, int] = {}  # existing target channels by id
        self.members_data = [{"user": user(USER), "roles": [str(ROLE)], "joined_at": "2023-01-01T00:00:00+00:00"}]
        self.calls: list[tuple] = []
        self._next = 900000

    # reads
    async def guild(self, gid):
        return {"id": str(gid), "name": "G", "approximate_member_count": 1}

    async def roles(self, gid):
        return self.roles_data

    async def channels(self, gid):
        return self.channels_data

    async def emojis(self, gid):
        return self.emojis_data

    async def stickers(self, gid):
        return []

    async def events(self, gid):
        return []

    async def automod(self, gid):
        raise Denied("no")

    async def invites(self, gid):
        return []

    async def webhooks(self, gid):
        return []

    async def bans(self, gid):
        return
        yield

    async def members(self, gid):
        for m in self.members_data:
            yield m

    async def audit(self, gid, after):
        yield {"audit_log_entries": [{"id": "5", "action_type": 1, "user_id": str(USER)}], "users": []}

    async def active_threads(self, gid):
        return {"threads": self.threads}

    async def archived_threads(self, cid, scope):
        return
        yield

    async def messages(self, cid, after):
        page = sorted((m for m in self.msgs.get(cid, []) if int(m["id"]) > after), key=lambda m: int(m["id"]))
        self.calls.append(("messages", cid, after))
        if page:
            yield page

    async def reaction_users(self, *args):
        return
        yield

    async def channel(self, cid):
        return {"id": str(cid), "type": self.target_types[cid]}

    async def cdn(self, url):
        return b"img"

    # writes
    def _id(self) -> int:
        self._next += 1
        return self._next

    async def create_role(self, gid, payload):
        if payload.get("unicode_emoji") and not self.role_icons:
            raise Denied("This server needs more boosts to perform this action")

        self.calls.append(("create_role", payload["name"]))
        return {"id": str(self._id())}

    async def edit_role(self, gid, rid, payload):
        self.calls.append(("edit_role", rid))

    async def create_channel(self, gid, payload):
        self.calls.append(("create_channel", payload["name"], payload.get("permission_overwrites")))
        return {"id": str(self._id()), "type": payload["type"]}

    async def create_thread(self, cid, name):
        self.calls.append(("create_thread", cid, name))
        return {"id": str(self._id())}

    async def create_emoji(self, gid, name, uri):
        if not self.emoji_slots:
            raise Rejected("Maximum number of emojis reached")

        self.emoji_slots -= 1
        self.calls.append(("create_emoji", name))
        return {"id": str(self._id())}

    async def create_webhook(self, cid, name):
        return {"id": str(self._id()), "token": "t"}

    async def execute_webhook(self, wid, token, payload, files, thread_id):
        channel = thread_id or (self._id() if payload.get("thread_name") else 1)
        self.calls.append(("send", payload.get("content"), thread_id, payload.get("thread_name")))
        return {"id": str(self._id()), "channel_id": str(channel)}

    async def pin(self, cid, mid):
        self.calls.append(("pin", mid))


BOT = 50
BOT_ROLE = 51


def role(rid: int, name: str, position: int, managed: bool = False) -> dict:
    return {"id": str(rid), "name": name, "position": position, "permissions": "0", "color": 0,
            "hoist": False, "mentionable": False, "managed": managed}


class FakeGuild:
    """Stateful in-memory guild for the builder: writes change what later reads return.

    Only writes are logged in `calls`, so `calls == []` means a build changed nothing.
    """

    def __init__(self) -> None:
        self.name = "G"
        self.roles_data = [role(GUILD, "@everyone", 0), role(BOT_ROLE, "Bot", 10, managed=True)]
        self.channels_data: list[dict] = []
        self.posts: dict[int, dict] = {}
        self.messages: dict[int, dict[int, str]] = {}
        self.flags: dict[int, int] = {}  # message id -> flags it was sent with
        self.embeds: dict[int, list] = {}  # message id -> embeds it carries
        self.calls: list[tuple] = []
        self._next = 700000

    def _id(self) -> int:
        self._next += 1
        return self._next

    def channel(self, name: str) -> dict:
        return next(c for c in self.channels_data if c["name"] == name)

    def _role(self, rid: int) -> dict:
        return next(r for r in self.roles_data if r["id"] == str(rid))

    # reads
    async def me(self):
        return {"id": str(BOT)}

    async def member(self, gid, uid):
        return {"roles": [str(BOT_ROLE)]}

    async def guild(self, gid):
        return {"id": str(gid), "name": self.name}

    async def roles(self, gid):
        return [dict(r) for r in self.roles_data]

    async def channels(self, gid):
        return json.loads(json.dumps(self.channels_data))

    # writes
    async def edit_guild(self, gid, payload):
        self.calls.append(("edit_guild", payload["name"]))
        self.name = payload["name"]

    async def create_role(self, gid, payload):
        # Discord inserts new roles at position 1 and shifts the rest up.
        for r in self.roles_data:
            if r["position"] >= 1:
                r["position"] += 1

        created = role(self._id(), payload["name"], 1) | payload
        self.roles_data.append(created)
        self.calls.append(("create_role", payload["name"]))
        return dict(created)

    async def edit_role(self, gid, rid, payload):
        self._role(rid).update(payload)
        self.calls.append(("edit_role", self._role(rid)["name"]))

    async def order_roles(self, gid, positions):
        # Discord refuses to place a role at or above the bot's highest role.
        top = self._role(BOT_ROLE)["position"]
        if any(p["position"] >= top for p in positions):
            raise Rejected("PATCH /guilds/{guild_id}/roles: Missing Permissions")

        for p in positions:
            self._role(int(p["id"]))["position"] = p["position"]

        self.calls.append(("order_roles",))

    async def delete_role(self, gid, rid):
        gone = self._role(rid)
        self.roles_data.remove(gone)
        self.calls.append(("delete_role", gone["name"]))

    def _tags(self, tags: list[dict]) -> list[dict]:
        return [t | {"id": t.get("id") or str(self._id())} for t in tags]

    async def create_channel(self, gid, payload):
        created = json.loads(json.dumps(payload)) | {"id": str(self._id()), "position": len(self.channels_data)}
        created.setdefault("parent_id", None)
        created["available_tags"] = self._tags(created.get("available_tags") or [])
        self.channels_data.append(created)
        self.calls.append(("create_channel", payload["name"]))
        return dict(created)

    async def edit_channel(self, cid, payload):
        self.calls.append(("edit_channel", cid, tuple(sorted(payload))))
        if cid in self.posts:
            self.posts[cid].update(payload)
            return

        target = next(c for c in self.channels_data if c["id"] == str(cid))
        target.update(json.loads(json.dumps(payload)))
        if "available_tags" in payload:
            target["available_tags"] = self._tags(target["available_tags"])

    async def delete_channel(self, cid):
        gone = next(c for c in self.channels_data if c["id"] == str(cid))
        self.channels_data.remove(gone)
        self.messages.pop(cid, None)
        self.calls.append(("delete_channel", gone["name"]))

    async def order_channels(self, gid, positions):
        for p in positions:
            next(c for c in self.channels_data if c["id"] == p["id"])["position"] = p["position"]

        self.calls.append(("order_channels",))

    async def create_post(self, fid, name, message, tags):
        post_id = self._id()
        self.posts[post_id] = {"name": name, "applied_tags": tags, "flags": 0}
        self.messages[post_id] = {post_id: message["content"]}
        self.calls.append(("create_post", name))
        return {"id": str(post_id)}

    async def send_message(self, cid, payload):
        mid = self._id()
        self.messages.setdefault(cid, {})[mid] = payload["content"]
        self.flags[mid] = payload.get("flags", 0)
        self.embeds[mid] = payload.get("embeds", [])
        self.calls.append(("send", cid))
        return {"id": str(mid), "channel_id": str(cid)}

    async def edit_message(self, cid, mid, payload):
        if mid not in self.messages.get(cid, {}):
            raise Missing("Unknown Message")

        self.messages[cid][mid] = payload["content"]
        self.embeds[mid] = payload.get("embeds", [])
        self.calls.append(("edit", cid, mid))

    async def delete_message(self, cid, mid):
        self.messages.get(cid, {}).pop(mid, None)
        self.calls.append(("delete", cid, mid))
