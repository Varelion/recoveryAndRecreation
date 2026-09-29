"""In-memory stand-in for vault.api.Rest."""

from vault.api import Denied

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
        return []

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

    async def cdn(self, url):
        return b"img"

    # writes
    def _id(self) -> int:
        self._next += 1
        return self._next

    async def create_role(self, gid, payload):
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

    async def create_webhook(self, cid, name):
        return {"id": str(self._id()), "token": "t"}

    async def execute_webhook(self, wid, token, payload, files, thread_id):
        channel = thread_id or (self._id() if payload.get("thread_name") else 1)
        self.calls.append(("send", payload.get("content"), thread_id, payload.get("thread_name")))
        return {"id": str(self._id()), "channel_id": str(channel)}

    async def pin(self, cid, mid):
        self.calls.append(("pin", mid))
