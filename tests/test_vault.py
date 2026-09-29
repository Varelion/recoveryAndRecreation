import pytest

from fakes import GUILD, POST, ROLE, TEXT, USER, FakeRest, message
from vault import config
from vault.config import Mode
from vault.services.exporter import ExportOptions, Exporter
from vault.services.importer import ImportOptions, Importer, _content
from vault.services.watcher import Watcher
from vault.store import Store

TARGET = 5000


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


async def export(rest, store, **opts):
    return await Exporter(rest, store, GUILD, ExportOptions(**opts)).run()


async def scalar(store, sql, *args):
    return (await store._one(sql, args))[0]


async def test_full_then_incremental(store):
    rest = FakeRest()
    rest.msgs[TEXT] = [message(100, TEXT), message(101, TEXT)]
    await export(rest, store)

    assert await scalar(store, "SELECT count(*) FROM messages") == 2
    assert await scalar(store, "SELECT count(*) FROM member_roles WHERE role_id = ?", ROLE) == 1
    assert await scalar(store, "SELECT count(*) FROM overwrites") == 1
    assert await scalar(store, "SELECT count(*) FROM audit_log") == 1
    assert await store.cursor("messages", TEXT) == 101

    # Second run asks only for messages after the cursor.
    rest.msgs[TEXT].append(message(102, TEXT))
    rest.calls.clear()
    await export(rest, store)

    assert ("messages", TEXT, 101) in rest.calls
    assert await scalar(store, "SELECT count(*) FROM messages") == 3


async def test_skips_channel_without_news(store):
    rest = FakeRest()
    rest.msgs[TEXT] = [message(100, TEXT)]
    await export(rest, store)

    rest.channels_data[0]["last_message_id"] = "100"
    rest.calls.clear()
    await export(rest, store)

    assert not [c for c in rest.calls if c[:2] == ("messages", TEXT)]


async def test_rescan_catches_edit_and_delete(store):
    rest = FakeRest()
    rest.msgs[TEXT] = [message(100, TEXT, "old"), message(101, TEXT)]
    await export(rest, store)

    rest.msgs[TEXT] = [message(100, TEXT, "new", edited_timestamp="2024-01-02T00:00:00+00:00")]
    stats = await export(rest, store, rescan_days=100000)

    assert stats["messages_deleted"] == 1
    assert await scalar(store, "SELECT content FROM message_versions WHERE message_id = 100") == "old"
    assert await scalar(store, "SELECT content FROM messages WHERE message_id = 100") == "new"
    assert await scalar(store, "SELECT gone_at IS NOT NULL FROM messages WHERE message_id = 101") == 1


async def test_member_left_and_role_history(store):
    rest = FakeRest()
    await export(rest, store)

    rest.members_data = []
    rest.roles_data[1] = rest.roles_data[1] | {"name": "moderator"}
    stats = await export(rest, store)

    assert stats["members_left"] == 1
    assert await scalar(store, "SELECT count(*) FROM history WHERE kind = 'role' AND entity_id = ?", str(ROLE)) == 2
    assert await scalar(store, "SELECT count(*) FROM v_member_flow WHERE left_ = 1") == 1


async def test_unchanged_export_adds_no_history(store):
    rest = FakeRest()
    await export(rest, store)
    before = await scalar(store, "SELECT count(*) FROM history")
    await export(rest, store)

    assert await scalar(store, "SELECT count(*) FROM history") == before


async def test_watcher_events(store):
    watcher = Watcher(link=None, store=store, guild_id=GUILD)
    g = str(GUILD)
    emoji = {"id": None, "name": "👍"}

    await watcher._on_event("MESSAGE_CREATE", message(200, TEXT) | {"guild_id": g})
    await watcher._on_event("MESSAGE_UPDATE", {"id": "200", "channel_id": str(TEXT), "guild_id": g,
                                               "content": "edited", "edited_timestamp": "2024-01-03T00:00:00Z"})
    await watcher._on_event("MESSAGE_REACTION_ADD", {"guild_id": g, "message_id": "200", "user_id": str(USER),
                                                     "emoji": emoji})
    assert await scalar(store, "SELECT count FROM reactions WHERE message_id = 200") == 1

    await watcher._on_event("MESSAGE_REACTION_REMOVE", {"guild_id": g, "message_id": "200",
                                                        "user_id": str(USER), "emoji": emoji})
    await watcher._on_event("MESSAGE_DELETE", {"guild_id": g, "id": "200", "channel_id": str(TEXT)})
    await watcher._on_event("MESSAGE_CREATE", message(201, TEXT) | {"guild_id": "999"})  # other guild

    assert await scalar(store, "SELECT content FROM messages WHERE message_id = 200") == "edited"
    assert await scalar(store, "SELECT author_id FROM messages WHERE message_id = 200") == USER
    assert await scalar(store, "SELECT count FROM reactions WHERE message_id = 200") == 0
    assert await scalar(store, "SELECT gone_at IS NOT NULL FROM messages WHERE message_id = 200") == 1
    assert await scalar(store, "SELECT count(*) FROM messages WHERE message_id = 201") == 0
    assert await scalar(store, "SELECT count(*) FROM gateway_events") == 4


async def test_import_recreates_and_resumes(store):
    rest = FakeRest()
    rest.msgs[TEXT] = [message(100, TEXT, pinned=True), message(101, TEXT)]
    rest.msgs[POST] = [message(300, POST, "first"), message(301, POST, "second")]
    await export(rest, store)

    importer = Importer(rest, store, GUILD, TARGET, ImportOptions())
    stats = await importer.run()

    assert stats["messages"] == 4
    assert ("create_role", "mod") in [c[:2] for c in rest.calls]
    assert ("edit_role", TARGET) in rest.calls
    created = next(c for c in rest.calls if c[:2] == ("create_channel", "general"))
    new_role = (await store.mapped(TARGET, "role", ROLE))["target_id"]
    assert created[2][0]["id"] == str(new_role)

    # Forum post opened by its first message, second goes into it.
    sends = [c for c in rest.calls if c[0] == "send"]
    assert sends[2][3] == "post" and sends[2][2] is None
    assert sends[3][2] is not None and sends[3][3] is None
    assert len([c for c in rest.calls if c[0] == "pin"]) == 1

    # Re-run: nothing duplicated.
    rest.calls.clear()
    again = await Importer(rest, store, GUILD, TARGET, ImportOptions()).run()
    assert not again.get("messages")
    assert not [c for c in rest.calls if c[0] in ("send", "create_channel", "create_role")]


async def test_import_dry_run_writes_nothing(store):
    rest = FakeRest()
    rest.msgs[TEXT] = [message(100, TEXT)]
    await export(rest, store)
    rest.calls.clear()

    stats = await Importer(rest, store, GUILD, TARGET, ImportOptions(mode=Mode.DRY_RUN)).run()

    assert stats["channels"] == 2 and stats["messages"] == 1
    assert rest.calls == []


def test_content_fits_limit():
    text = _content(message(1, TEXT, "x" * 5000), 0)

    assert len(text) <= config.CONTENT_LIMIT
    assert text.endswith("2024-01-01 00:00 UTC")
