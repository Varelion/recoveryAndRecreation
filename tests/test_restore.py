import pytest

from fakes import CATEGORY, CHAT, GENERAL, GUILD, ROLE, VIEW, FakeLive
from vault.config import Media, Messages, Mode
from vault.services.backup import snapshot
from vault.services.exporter import ExportOptions, Exporter
from vault.services.restore import RestoreOptions, UnknownGuild, restore
from vault.store import Store

EDITS = {"edit_channel", "edit_role", "edit_guild", "delete_channel", "delete_role"}


@pytest.fixture
async def backup(tmp_path):
    """Back up a FakeLive guild; tests then damage the guild and restore it."""
    live = FakeLive()
    store = await Store.open(tmp_path / "t.db")
    await Exporter(live, store, GUILD, ExportOptions(media=Media.SKIP)).run()
    path = await snapshot(store, GUILD, tmp_path / "backups")
    await store.close()
    return live, path


def _damage(live: FakeLive) -> None:
    # Lose general (and its 2 messages), the mod role, and one message of the surviving chat.
    live.delete(GENERAL)
    live.drop_role(ROLE)
    live.texts[CHAT].pop(203)


def _named(live: FakeLive, name: str) -> dict:
    return next(c for c in live.channels_data if c["name"] == name)


async def test_dry_run_lists_missing_only(backup):
    live, path = backup
    _damage(live)

    report = await restore(live, path, GUILD, RestoreOptions(mode=Mode.DRY_RUN))

    assert report["missing"] == {"roles": ["mod"], "channels": ["general"]}
    assert (report["roles"], report["channels"], report["messages"]) == (1, 1, 2)
    assert live.calls == []


async def test_restores_missing_as_it_was(backup):
    live, path = backup
    _damage(live)

    report = await restore(live, path, GUILD, RestoreOptions())

    mod = next(r for r in live.roles_data if r["name"] == "mod")
    general = _named(live, "general")
    assert general["id"] != str(GENERAL)
    assert str(general["parent_id"]) == str(CATEGORY)
    assert general["topic"] == "hello"
    assert general["position"] == 1
    assert general["permission_overwrites"] == [{"id": mod["id"], "type": 0, "allow": VIEW, "deny": "0"}]
    texts = [t.split("\n")[0] for _, t in sorted(live.texts[int(general["id"])].items())]
    assert texts == ["a", "b"]
    assert report["messages"] == 2

    # Survivors are left alone: no edits, and chat's lost message is not reposted.
    assert not [c for c in live.calls if c[0] in EDITS]
    assert ("send", CHAT) not in live.calls
    assert list(live.texts[CHAT]) == []


async def test_structure_only_skips_messages(backup):
    live, path = backup
    _damage(live)

    report = await restore(live, path, GUILD, RestoreOptions(messages=Messages.SKIP))

    assert report["channels"] == 1 and not report.get("messages")
    assert not [c for c in live.calls if c[0] == "send"]


async def test_channel_limits_scope(backup):
    live, path = backup
    _damage(live)
    live.delete(CHAT)

    # chat's overwrites need no role, so mod stays missing.
    report = await restore(live, path, GUILD, RestoreOptions(channels={CHAT}))

    assert report["missing"] == {"roles": [], "channels": ["chat"]}
    assert [c["name"] for c in live.channels_data] == ["INFO", "chat"]
    assert not [r for r in live.roles_data if r["name"] == "mod"]


async def test_channel_scope_brings_lost_parent(backup):
    live, path = backup
    live.delete(CHAT)
    live.delete(GENERAL)
    live.delete(CATEGORY)

    report = await restore(live, path, GUILD, RestoreOptions(channels={CHAT}))

    assert report["missing"]["channels"] == ["INFO", "chat"]
    assert str(_named(live, "chat")["parent_id"]) == _named(live, "INFO")["id"]


async def test_channel_scope_brings_needed_role(backup):
    live, path = backup
    _damage(live)

    report = await restore(live, path, GUILD, RestoreOptions(channels={GENERAL}))

    assert report["missing"] == {"roles": ["mod"], "channels": ["general"]}


async def test_nothing_missing_does_nothing(backup):
    live, path = backup

    report = await restore(live, path, GUILD, RestoreOptions())

    assert report["missing"] == {"roles": [], "channels": []}
    assert live.calls == []


async def test_same_name_counts_as_present(backup):
    live, path = backup
    # Someone recreated general by hand: new id, same name, type and parent.
    live.channel_by_id(GENERAL)["id"] = "999"

    report = await restore(live, path, GUILD, RestoreOptions())

    assert report["missing"]["channels"] == []
    assert live.calls == []


async def test_unknown_guild_fails(backup):
    live, path = backup

    with pytest.raises(UnknownGuild):
        await restore(live, path, GUILD + 1, RestoreOptions())
