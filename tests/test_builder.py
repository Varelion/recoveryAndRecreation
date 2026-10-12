from pathlib import Path

import pytest

from fakes import GUILD, FakeGuild, role
from vault.config import Mode, Prune
from vault.services.blueprint import Blueprint, BlueprintError, load
from vault.services.builder import (MEMBER_OVERWRITE, REQUIRE_TAG, ROLE_OVERWRITE, SILENT, SPLIT, Blocked, BuildOptions,
                                    Builder, _chunks, _payload, _size)
from vault.store import Store

MANIFEST = """
[server]
name = "Courts Unbound"

[presets.public]
"@everyone" = { allow = ["view"], deny = ["send"] }
"Staff" = { allow = ["send"] }

[presets.hall]
"@everyone" = { deny = ["view"] }
"$grant" = { allow = ["view", "send"] }

[[role]]
name = "Staff"
color = 0xC9A227
hoist = true

[[role]]
name = "Night Court"

[[category]]
id = "info"
name = "INFO"
access = "public"

[[category.channel]]
id = "rules"
name = "rules"
docs = ["docs/rules.md"]

[[category.channel]]
id = "claims"
name = "claims"
type = "forum"
tags = ["Open", "Closed"]

[[category.channel.thread]]
title = "Rank Claims"
doc = "docs/claims.md"
tags = ["Open"]
pin = true

[[category]]
id = "halls"
name = "HALLS"
access = "hall"

[[category.channel]]
id = "night-hall"
name = "night-hall"
grant = ["Night Court"]

[[category.channel]]
id = "lounge"
name = "Lounge"
type = "voice"
access = "public"
"""

DOCS = {
    "docs/rules.md": "# Rules\n\nBe kind. Claims live in {#claims}.",
    "docs/claims.md": "Post your claim here; {@Staff} reviews it.",
}


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


def blueprint(root: Path, manifest: str = MANIFEST, docs: dict[str, str] = DOCS) -> Blueprint:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "server.toml").write_text(manifest, encoding="utf-8")
    for name, text in docs.items():
        (root / name).write_text(text, encoding="utf-8")

    return load(root)


async def build(guild, store, layout, mode=Mode.LIVE, roles=Prune.KEEP, channels=Prune.KEEP):
    return await Builder(guild, store, GUILD, layout, BuildOptions(mode, roles, channels)).run()


LOUNGE = """
[[category.channel]]
id = "lounge"
name = "Lounge"
type = "voice"
access = "public"
"""


async def test_fresh_build_then_rerun_writes_nothing(store, tmp_path):
    guild = FakeGuild()
    layout = blueprint(tmp_path / "bp")
    stats = await build(guild, store, layout)

    assert stats["roles_created"] == 2
    assert stats["channels_created"] == 6
    assert stats["posts_created"] == 1
    assert guild.name == "Courts Unbound"

    # Placeholders became links; the forum post is pinned.
    rules = int(guild.channel("rules")["id"])
    claims = guild.channel("claims")["id"]
    assert f"<#{claims}>" in next(iter(guild.messages[rules].values()))
    assert next(iter(guild.posts.values()))["flags"] == 2

    # Blueprint roles sit right below the bot, in blueprint order.
    order = [r["name"] for r in sorted(guild.roles_data, key=lambda r: -r["position"])]
    assert order[:3] == ["Bot", "Staff", "Night Court"]

    guild.calls.clear()
    await build(guild, store, layout)

    assert guild.calls == []


async def test_grant_opens_a_hall_to_one_role(store, tmp_path):
    guild = FakeGuild()
    await build(guild, store, blueprint(tmp_path / "bp"))

    night = next(r for r in guild.roles_data if r["name"] == "Night Court")
    overwrites = {o["id"]: o for o in guild.channel("night-hall")["permission_overwrites"]}
    assert overwrites[str(GUILD)]["deny"] == str(1 << 10)
    assert overwrites[night["id"]]["allow"] == str(1 << 10 | 1 << 11)


async def test_adopts_existing_channel_and_same_named_role(store, tmp_path):
    guild = FakeGuild()
    guild.roles_data.append(role(60, "Staff", 3))
    guild.channels_data.append({"id": "555", "type": 0, "name": "old-rules", "parent_id": None, "position": 0,
                                "permission_overwrites": []})
    layout = blueprint(tmp_path / "bp", MANIFEST.replace('id = "rules"\n', 'id = "rules"\nadopt = 555\n'))
    stats = await build(guild, store, layout)

    assert stats["roles_adopted"] == 1
    assert stats["channels_adopted"] == 1
    assert ("create_role", "Staff") not in guild.calls
    assert ("create_channel", "rules") not in guild.calls

    adopted = next(c for c in guild.channels_data if c["id"] == "555")
    assert adopted["name"] == "rules"
    assert adopted["parent_id"] == guild.channel("INFO")["id"]


async def test_prune_deletes_only_unmanaged_extras(store, tmp_path):
    guild = FakeGuild()
    guild.roles_data += [role(61, "Old Faction", 2), role(62, "Avrae", 3, managed=True)]
    layout = blueprint(tmp_path / "bp")

    kept = await build(guild, store, layout)
    assert kept["extra_roles"] == ["Old Faction"]
    assert not [c for c in guild.calls if c[0] == "delete_role"]

    await build(guild, store, layout, roles=Prune.DELETE)
    assert ("delete_role", "Old Faction") in guild.calls
    assert {"@everyone", "Bot", "Avrae"} <= {r["name"] for r in guild.roles_data}


async def test_doc_edit_touches_only_changed_message(store, tmp_path):
    first, second = "a" * 1500, "b" * 1500
    guild = FakeGuild()
    await build(guild, store, blueprint(tmp_path / "bp", docs=DOCS | {"docs/rules.md": f"{first}\n\n{second}"}))
    rules = int(guild.channel("rules")["id"])
    assert len(guild.messages[rules]) == 2

    guild.calls.clear()
    await build(guild, store, blueprint(tmp_path / "bp", docs=DOCS | {"docs/rules.md": f"{first}\n\n{'c' * 1500}"}))
    assert [c[0] for c in guild.calls] == ["edit"]

    guild.calls.clear()
    await build(guild, store, blueprint(tmp_path / "bp", docs=DOCS | {"docs/rules.md": first}))
    assert [c[0] for c in guild.calls] == ["delete"]


async def test_message_deleted_by_hand_is_sent_again(store, tmp_path):
    guild = FakeGuild()
    await build(guild, store, blueprint(tmp_path / "bp"))
    rules = int(guild.channel("rules")["id"])
    guild.messages[rules].clear()

    guild.calls.clear()
    await build(guild, store, blueprint(tmp_path / "bp", docs=DOCS | {"docs/rules.md": "# Rules\n\nChanged."}))
    assert [c[0] for c in guild.calls] == ["send"]


async def test_forum_tags_follow_the_blueprint_and_keep_ids(store, tmp_path):
    guild = FakeGuild()
    tagged = MANIFEST.replace('tags = ["Open", "Closed"]',
                              'tags = [{ name = "Open", emoji = "🟢", moderated = true }, "Closed"]\nrequire_tag = true')
    await build(guild, store, blueprint(tmp_path / "bp", tagged))
    claims = guild.channel("claims")
    before = {t["name"]: t for t in claims["available_tags"]}
    assert (before["Open"]["emoji_name"], before["Open"]["moderated"]) == ("🟢", True)
    assert claims["flags"] & REQUIRE_TAG

    # A new tag keeps the old ones' ids, so existing posts keep their tags.
    grown = tagged.replace('"Closed"]', '"Closed", "Pending"]')
    await build(guild, store, blueprint(tmp_path / "bp", grown))
    after = {t["name"]: t for t in guild.channel("claims")["available_tags"]}
    assert after["Open"]["id"] == before["Open"]["id"]
    assert after["Closed"]["id"] == before["Closed"]["id"]
    assert after["Open"]["emoji_name"] == "🟢"
    assert "Pending" in after

    guild.calls.clear()
    await build(guild, store, blueprint(tmp_path / "bp", grown))
    assert guild.calls == []


async def test_dropped_channel_is_deleted_only_on_request(store, tmp_path):
    guild = FakeGuild()
    await build(guild, store, blueprint(tmp_path / "bp"))
    layout = blueprint(tmp_path / "bp", MANIFEST.replace(LOUNGE, ""))

    kept = await build(guild, store, layout)
    assert kept["extra_channels"] == ["Lounge"]
    assert not [c for c in guild.calls if c[0] == "delete_channel"]

    await build(guild, store, layout, channels=Prune.DELETE)
    assert ("delete_channel", "Lounge") in guild.calls
    assert "extra_channels" not in await build(guild, store, layout)


async def test_channels_the_build_never_made_are_never_pruned(store, tmp_path):
    guild = FakeGuild()
    guild.channels_data.append({"id": "777", "type": 0, "name": "ticket-0001", "parent_id": None, "position": 9,
                                "permission_overwrites": []})
    stats = await build(guild, store, blueprint(tmp_path / "bp"), channels=Prune.DELETE)

    assert "extra_channels" not in stats
    assert any(c["name"] == "ticket-0001" for c in guild.channels_data)


async def test_overwrites_for_one_member_set_by_hand_are_kept(store, tmp_path):
    # Staff open a channel to one person on Discord; blueprints speak of roles, so builds leave people alone.
    guild = FakeGuild()
    await build(guild, store, blueprint(tmp_path / "bp"))
    person = {"id": "424242", "type": MEMBER_OVERWRITE, "allow": "1024", "deny": "0"}
    guild.channel("rules")["permission_overwrites"].append(person)

    guild.calls.clear()
    await build(guild, store, blueprint(tmp_path / "bp"))
    assert guild.calls == []

    # An edit made for another reason keeps the person too.
    retopic = MANIFEST.replace('name = "rules"\n', 'name = "rules"\ntopic = "Read me."\n')
    await build(guild, store, blueprint(tmp_path / "bp", retopic))
    assert guild.channel("rules")["topic"] == "Read me."
    assert person in guild.channel("rules")["permission_overwrites"]


async def test_overwrites_for_roles_the_blueprint_never_names_are_kept(store, tmp_path):
    # Bots invited by hand (Tupperbox) bring their own roles, and staff open channels to them on Discord.
    guild = FakeGuild()
    guild.roles_data.append(role(90, "Tupperbox", 1, managed=True))
    await build(guild, store, blueprint(tmp_path / "bp"))
    proxy = {"id": "90", "type": ROLE_OVERWRITE, "allow": "1024", "deny": "0"}
    guild.channel("rules")["permission_overwrites"].append(proxy)

    guild.calls.clear()
    await build(guild, store, blueprint(tmp_path / "bp"))
    assert guild.calls == []

    retopic = MANIFEST.replace('name = "rules"\n', 'name = "rules"\ntopic = "Read me."\n')
    await build(guild, store, blueprint(tmp_path / "bp", retopic))
    assert proxy in guild.channel("rules")["permission_overwrites"]


async def test_type_change_replaces_the_channel(store, tmp_path):
    guild = FakeGuild()
    await build(guild, store, blueprint(tmp_path / "bp"))
    old = guild.channel("night-hall")["id"]
    layout = blueprint(tmp_path / "bp", MANIFEST.replace('name = "night-hall"\n', 'name = "night-hall"\ntype = "forum"\n'))

    kept = await build(guild, store, layout)
    assert kept["channels_replaced"] == 1
    assert kept["extra_channels"] == ["night-hall"]

    await build(guild, store, layout, channels=Prune.DELETE)
    halls = [c for c in guild.channels_data if c["name"] == "night-hall"]
    assert [(h["type"], h["id"] != old) for h in halls] == [(15, True)]


async def test_docs_link_forum_posts_by_id(store, tmp_path):
    guild = FakeGuild()
    # A second post in the claims forum; each post links to the other.
    second = '\n[[category.channel.thread]]\nid = "faces"\ntitle = "Face Claims"\ndoc = "docs/faces.md"\n'
    manifest = MANIFEST.replace('title = "Rank Claims"', 'id = "rank-claims"\ntitle = "Rank Claims"') \
        .replace("pin = true\n", "pin = true\n" + second, 1)
    docs = DOCS | {"docs/claims.md": "Faces: {#faces}.", "docs/faces.md": "Ranks: {#rank-claims}."}
    layout = blueprint(tmp_path / "bp", manifest, docs)
    await build(guild, store, layout)

    ids = {post["name"]: post_id for post_id, post in guild.posts.items()}
    assert guild.messages[ids["Rank Claims"]][ids["Rank Claims"]] == f"Faces: <#{ids['Face Claims']}>."
    assert guild.messages[ids["Face Claims"]][ids["Face Claims"]] == f"Ranks: <#{ids['Rank Claims']}>."

    guild.calls.clear()
    await build(guild, store, layout)
    assert guild.calls == []


# An ordered forum: pages listed last first, so the last listed ends on top once bumped.
ORDERED = MANIFEST.replace('tags = ["Open", "Closed"]\n', 'tags = ["Open", "Closed"]\nordered = true\n', 1).replace(
    'tags = ["Open"]\npin = true\n',
    'tags = ["Open"]\n\n[[category.channel.thread]]\ntitle = "Face Claims"\ndoc = "docs/faces.md"\n',
)
ORDERED_DOCS = DOCS | {"docs/faces.md": "Faces."}


def bumps(guild) -> list[int]:
    """Posts touched by a send then deleted at once, in call order."""
    sends = [c for c in guild.calls if c[0] == "send"]
    deleted = {c[1] for c in guild.calls if c[0] == "delete"}
    return [c[1] for c in sends if c[1] in deleted]


async def test_ordered_forum_is_bumped_silently_in_blueprint_order_when_it_changes(store, tmp_path):
    guild = FakeGuild()
    await build(guild, store, blueprint(tmp_path / "bp", ORDERED, ORDERED_DOCS))
    ids = {post["name"]: post_id for post_id, post in guild.posts.items()}
    order = [ids["Rank Claims"], ids["Face Claims"]]
    assert bumps(guild) == order

    # The touch is silent and leaves only the page text behind.
    assert {guild.flags[c[2]] for c in guild.calls if c[0] == "delete"} == {SILENT}
    assert list(guild.messages[ids["Face Claims"]].values()) == ["Faces."]

    guild.calls.clear()
    await build(guild, store, blueprint(tmp_path / "bp", ORDERED, ORDERED_DOCS))
    assert guild.calls == []

    guild.calls.clear()
    await build(guild, store, blueprint(tmp_path / "bp", ORDERED, ORDERED_DOCS | {"docs/claims.md": "Changed."}))
    assert bumps(guild) == order


async def test_unordered_forum_is_never_bumped(store, tmp_path):
    guild = FakeGuild()
    manifest = ORDERED.replace("ordered = true\n", "")
    await build(guild, store, blueprint(tmp_path / "bp", manifest, ORDERED_DOCS))
    assert bumps(guild) == []


async def test_dry_run_counts_bumps(store, tmp_path):
    stats = await build(FakeGuild(), store, blueprint(tmp_path / "bp", ORDERED, ORDERED_DOCS), mode=Mode.DRY_RUN)
    assert stats["posts_bumped"] == 2


def test_only_forums_can_be_ordered(tmp_path):
    with pytest.raises(BlueprintError, match="ordered"):
        blueprint(tmp_path / "bp", MANIFEST.replace('docs = ["docs/rules.md"]\n', 'docs = ["docs/rules.md"]\nordered = true\n', 1))


async def test_dry_run_writes_nothing(store, tmp_path):
    guild = FakeGuild()
    stats = await build(guild, store, blueprint(tmp_path / "bp"), mode=Mode.DRY_RUN)

    assert guild.calls == []
    assert stats["channels_created"] == 6
    assert stats["posts_created"] == 1
    assert stats["messages_sent"] == 1


async def test_roles_sharing_a_position_still_order_below_the_bot(store, tmp_path):
    # Discord lets roles share a position: here twelve sit at 1, so more roles are below the bot than its position.
    guild = FakeGuild()
    guild.roles_data += [role(800 + n, f"Character {n}", 1) for n in range(12)]
    layout = blueprint(tmp_path / "bp")

    await build(guild, store, layout)

    order = [r["name"] for r in sorted(guild.roles_data, key=lambda r: (-r["position"], int(r["id"])))]
    assert order[:3] == ["Bot", "Staff", "Night Court"]
    assert order[3:-1] == [f"Character {n}" for n in range(12)]

    guild.calls.clear()
    await build(guild, store, layout)
    assert guild.calls == []


async def test_roles_above_the_bot_block_a_live_build(store, tmp_path):
    guild = FakeGuild()
    guild.roles_data.append(role(60, "Staff", 20))
    layout = blueprint(tmp_path / "bp")

    with pytest.raises(Blocked):
        await build(guild, store, layout)
    assert guild.calls == []

    stats = await build(guild, store, layout, mode=Mode.DRY_RUN)
    assert stats["blocked_roles"] == ["Staff"]


def test_chunks_keep_sections_whole_and_cut_long_text():
    assert _chunks("# A\n\nx\n\n# B\n\ny") == ["# A\n\nx\n\n# B\n\ny"]
    assert _chunks(f"one\n\n{SPLIT}\n\ntwo") == ["one", "two"]

    parts = _chunks("# A\n\n" + "w" * 3000 + "\n\n# B\n\n" + "line\n" * 900)
    assert all(_size(p) <= 2000 for p in parts)
    assert parts[0] == "# A"
    assert "".join(parts).count("w") == 3000

    # Emoji count double, as Discord may count them.
    emoji = _chunks("🌙" * 1500)
    assert [_size(p) for p in emoji] == [2000, 1000]


SAIYAN = "<!-- embed #C0392B -->\n**🐒 Saiyan**\n750–1,500 · Vegeta\n\nGrows stronger after near-death."
NAMEK = "<!-- embed -->\n**🟢 Namekian**\nRegenerates."


def test_chunks_give_each_embed_its_own_part():
    text = f"Fourteen races.\n\n{SAIYAN}\n\n{NAMEK}"
    assert _chunks(text) == ["Fourteen races.", SAIYAN, NAMEK]


def test_payload_turns_an_embed_part_into_a_titled_colored_embed():
    embed = _payload(SAIYAN)
    assert embed["content"] == ""
    assert embed["embeds"] == [{"title": "🐒 Saiyan", "description": "750–1,500 · Vegeta\n\nGrows stronger after near-death.", "color": 0xC0392B}]
    assert _payload(NAMEK)["embeds"] == [{"title": "🟢 Namekian", "description": "Regenerates."}]

    # Plain text clears any embed the message carried before.
    assert _payload("Hi.") == {"content": "Hi.", "embeds": [], "allowed_mentions": {"parse": []}}


async def test_doc_embeds_are_sent_and_edited_one_by_one(store, tmp_path):
    guild = FakeGuild()
    docs = DOCS | {"docs/rules.md": f"Races.\n\n{SAIYAN}\n\n{NAMEK}"}
    await build(guild, store, blueprint(tmp_path / "bp", docs=docs))
    rules = int(guild.channel("rules")["id"])
    sent = list(guild.messages[rules])
    assert len(sent) == 3
    assert guild.embeds[sent[1]][0]["title"] == "🐒 Saiyan"

    guild.calls.clear()
    docs = DOCS | {"docs/rules.md": f"Races.\n\n{SAIYAN}\n\n{NAMEK} Stretches."}
    await build(guild, store, blueprint(tmp_path / "bp", docs=docs))
    assert guild.calls == [("edit", rules, sent[2])]
    assert guild.embeds[sent[2]][0]["description"] == "Regenerates. Stretches."
