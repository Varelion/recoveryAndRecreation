import asyncio

from vault.config import Mode
from vault.services.community import COMMUNITY, enable

GUILD = 1000


class Rest:
    """Only what `community` touches; records every write."""

    def __init__(self, features: list[str], channels: list[dict], rules_id: str | None = None) -> None:
        self._guild = {"id": str(GUILD), "features": features, "rules_channel_id": rules_id,
                       "public_updates_channel_id": None}
        self._channels = channels
        self.calls: list[tuple] = []
        self._next = 500

    async def guild(self, gid):
        return self._guild

    async def channels(self, gid):
        return self._channels

    async def create_channel(self, gid, payload):
        self._next += 1
        self.calls.append(("create_channel", payload))
        return {"id": str(self._next), "type": payload["type"]}

    async def edit_guild(self, gid, payload):
        self.calls.append(("edit_guild", payload))
        return payload


def run(coro):
    return asyncio.run(coro)


def test_creates_both_channels_and_enables():
    rest = Rest(["NEWS"], [{"id": "10", "type": 0, "name": "general"}])

    report = run(enable(rest, GUILD, Mode.LIVE))

    assert report.summary() == {"community": "enabled", "rules_channel": 501, "updates_channel": 502,
                                "created": ["rules", "moderator-only"]}
    assert rest.calls[:2] == [("create_channel", {"name": "rules", "type": 0}),
                              ("create_channel", {"name": "moderator-only", "type": 0})]
    patch = rest.calls[2][1]
    assert patch["features"] == ["NEWS", COMMUNITY]
    assert (patch["rules_channel_id"], patch["public_updates_channel_id"]) == ("501", "502")
    assert (patch["verification_level"], patch["explicit_content_filter"]) == (1, 2)


def test_reuses_existing_channels_by_setting_or_name():
    rest = Rest([], [{"id": "10", "type": 0, "name": "general"}, {"id": "11", "type": 0, "name": "moderator-only"}],
                rules_id="12")

    report = run(enable(rest, GUILD, Mode.LIVE))

    assert (report.rules_channel, report.updates_channel, report.created) == (12, 11, ())
    assert [c[0] for c in rest.calls] == ["edit_guild"]


def test_dry_run_writes_nothing():
    rest = Rest([], [])

    report = run(enable(rest, GUILD, Mode.DRY_RUN))

    assert report.community == "would enable"
    assert report.created == ("rules", "moderator-only")
    assert rest.calls == []


def test_already_a_community():
    rest = Rest([COMMUNITY], [{"id": "12", "type": 0, "name": "rules"}])

    report = run(enable(rest, GUILD, Mode.LIVE))

    assert report.community == "already"
    assert report.rules_channel == 12
    assert rest.calls == []
