"""Server blueprint: a TOML layout plus markdown docs, checked before any Discord call.

    <dir>/server.toml                              <dir>/docs/*.md
    ┌──────────────────────────────────┐          ┌───────────────────────────┐
    │ [server]      guild name         │          │ text posted into channels │
    │ [presets.x]   who sees / writes  │  docs =  │ {#channel-id} and {@Role} │
    │ [[role]]      top → bottom       │ ───────► │ become Discord links      │
    │ [[category]]  ─► [[...channel]]  │          └───────────────────────────┘
    │                  ─► [[...thread]]│  (forum posts)
    └──────────────────────────────────┘

Presets name role overwrites in words: "Approved Player" = { allow = ["view"] }.
"$grant" in a preset stands for each role a channel lists under `grant`,
e.g. one preset shared by every court's private hall.

A category's `default_type` and `default_tags` apply to its channels unless they
set their own. A forum thread with an `id` can be linked from docs like a channel.
"""

import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntFlag
from pathlib import Path
from typing import Any

from vault.config import ChannelType

MANIFEST = "server.toml"
EVERYONE = "@everyone"
GRANT = "$grant"

# Discord limits (spec).
NAME_LIMIT = 100
TOPIC_LIMIT = 1024
TAG_LIMIT = 20
TAG_NAME_LIMIT = 20
CATEGORY_CHILDREN = 50
GUILD_CHANNELS = 500
COLOR_MAX = 0xFFFFFF
SLOWMODE_MAX = 21600

KINDS = {
    "text": ChannelType.TEXT,
    "voice": ChannelType.VOICE,
    "announcement": ChannelType.ANNOUNCEMENT,
    "forum": ChannelType.FORUM,
}

# Discord lowercases these names and strips ASCII punctuation but "-" and "_";
# a blueprint name that differs would be re-applied on every build.
_SLUGGED = {ChannelType.TEXT, ChannelType.ANNOUNCEMENT, ChannelType.FORUM}
_SLUG_BREAKERS = re.compile(r"[\s!\"#$%&'()*+,./:;<=>?@\[\\\]^`{|}~]")

# Kinds that hold the blueprint's posted docs.
_DOC_HOLDERS = {ChannelType.TEXT, ChannelType.ANNOUNCEMENT}

_ID = re.compile(r"^[a-z0-9-]+$")

# Placeholders in docs: {#velaris} -> channel link, {@Staff} -> role mention.
CHANNEL_REF = re.compile(r"\{#([a-z0-9-]+)\}")
ROLE_REF = re.compile(r"\{@([^{}]+)\}")


class Permission(IntFlag):
    """Discord permission bits (spec); blueprints use the lowercase names."""

    CREATE_INVITE = 1 << 0
    KICK = 1 << 1
    BAN = 1 << 2
    MANAGE_CHANNELS = 1 << 4
    MANAGE_GUILD = 1 << 5
    REACT = 1 << 6
    VIEW = 1 << 10
    SEND = 1 << 11
    MANAGE_MESSAGES = 1 << 13
    EMBED = 1 << 14
    ATTACH = 1 << 15
    HISTORY = 1 << 16
    MENTION_EVERYONE = 1 << 17
    CONNECT = 1 << 20
    SPEAK = 1 << 21
    MANAGE_NICKNAMES = 1 << 27
    MANAGE_ROLES = 1 << 28
    MANAGE_THREADS = 1 << 34
    CREATE_PUBLIC_THREADS = 1 << 35
    CREATE_PRIVATE_THREADS = 1 << 36
    SEND_IN_THREADS = 1 << 38
    MODERATE = 1 << 40


class BlueprintError(Exception):
    """Every problem found in a blueprint, one per line."""


@dataclass(frozen=True)
class Overwrite:
    role: str  # role name, EVERYONE or GRANT
    allow: Permission
    deny: Permission


@dataclass(frozen=True)
class Role:
    name: str
    color: int
    hoist: bool
    mentionable: bool
    permissions: Permission | None  # None: an adopted role keeps its own


@dataclass(frozen=True)
class Doc:
    name: str  # path inside the blueprint, e.g. "docs/rules.md"
    text: str


@dataclass(frozen=True)
class Tag:
    """Forum tag. Moderated tags can only be applied by members who manage threads."""

    name: str
    emoji: str | None
    moderated: bool


@dataclass(frozen=True)
class Thread:
    """Forum post; its doc becomes the opening message and the replies after it."""

    id: str | None  # lets docs link the post: {#id}
    title: str
    doc: Doc
    tags: tuple[str, ...]
    pin: bool


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    kind: ChannelType
    topic: str | None
    access: str | None  # preset; None = the category's
    grant: tuple[str, ...]
    adopt: int | None  # existing channel taken over instead of created
    nsfw: bool
    slowmode: int
    tags: tuple[Tag, ...]
    require_tag: bool  # forums: every new post must carry a tag
    ordered: bool  # forums: posts re-bumped in blueprint order whenever the build changes one
    docs: tuple[Doc, ...]
    threads: tuple[Thread, ...]


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    access: str
    channels: tuple[Channel, ...]


@dataclass(frozen=True)
class Blueprint:
    name: str | None
    presets: dict[str, tuple[Overwrite, ...]]
    roles: tuple[Role, ...]
    categories: tuple[Category, ...]

    def channels(self) -> Iterator[Channel]:
        for category in self.categories:
            yield from category.channels


def load(root: Path) -> Blueprint:
    """Parse and check `root`/server.toml with its docs; raises BlueprintError listing every problem."""
    try:
        raw = tomllib.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BlueprintError(f"{root / MANIFEST}: {exc}") from exc

    problems: list[str] = []
    blueprint = _Parser(root, problems).blueprint(raw)
    _check(blueprint, problems)
    if problems:
        raise BlueprintError("\n".join(problems))

    return blueprint


# ---------------------------------------------------------------- parsing


class _Parser:
    """TOML tables -> dataclasses; problems are collected, not raised, so one run shows them all."""

    def __init__(self, root: Path, problems: list[str]) -> None:
        self._root = root
        self._problems = problems

    def blueprint(self, raw: dict[str, Any]) -> Blueprint:
        presets = {name: self._preset(name, spec) for name, spec in raw.get("presets", {}).items()}
        return Blueprint(
            name=raw.get("server", {}).get("name"),
            presets=presets,
            roles=tuple(self._role(spec) for spec in raw.get("role", [])),
            categories=tuple(self._category(spec) for spec in raw.get("category", [])),
        )

    def _preset(self, name: str, spec: dict[str, Any]) -> tuple[Overwrite, ...]:
        where = f"preset {name}"
        return tuple(
            Overwrite(role, self._flags(rule.get("allow", []), where), self._flags(rule.get("deny", []), where))
            for role, rule in spec.items()
        )

    def _role(self, spec: dict[str, Any]) -> Role:
        name = spec.get("name", "")
        words = spec.get("permissions")
        return Role(
            name=name,
            color=spec.get("color", 0),
            hoist=spec.get("hoist", False),
            mentionable=spec.get("mentionable", False),
            permissions=None if words is None else self._flags(words, f"role {name}"),
        )

    def _category(self, spec: dict[str, Any]) -> Category:
        defaults = {"type": spec.get("default_type", "text"), "tags": spec.get("default_tags", [])}
        return Category(
            id=spec.get("id", ""),
            name=spec.get("name", ""),
            access=spec.get("access", ""),
            channels=tuple(self._channel(child, defaults) for child in spec.get("channel", [])),
        )

    def _channel(self, spec: dict[str, Any], defaults: dict[str, Any]) -> Channel:
        where = f"channel {spec.get('id', '?')}"
        word = spec.get("type", defaults["type"])
        kind = KINDS.get(word)
        if kind is None:
            self._problems.append(f"{where}: unknown type {word!r} (use {', '.join(KINDS)})")
            kind = ChannelType.TEXT

        # Default tags only make sense on forums.
        default_tags = defaults["tags"] if kind is ChannelType.FORUM else []
        topic = spec.get("topic")
        return Channel(
            id=spec.get("id", ""),
            name=spec.get("name", ""),
            kind=kind,
            topic=topic.strip() if topic else None,
            access=spec.get("access"),
            grant=tuple(spec.get("grant", [])),
            adopt=spec.get("adopt"),
            nsfw=spec.get("nsfw", False),
            slowmode=spec.get("slowmode", 0),
            tags=tuple(self._tag(tag, where) for tag in spec.get("tags", default_tags)),
            require_tag=spec.get("require_tag", False),
            ordered=self._ordered(spec, kind, where),
            docs=tuple(d for d in (self._doc(path, where) for path in spec.get("docs", [])) if d),
            threads=tuple(self._thread(post, where) for post in spec.get("thread", [])),
        )

    def _ordered(self, spec: dict[str, Any], kind: ChannelType, where: str) -> bool:
        ordered = spec.get("ordered", False)
        if ordered and kind is not ChannelType.FORUM:
            self._problems.append(f"{where}: only forums can be ordered")

        return ordered

    def _tag(self, spec: str | dict[str, Any], where: str) -> Tag:
        """"Open" or { name = "Landmark", emoji = "📍", moderated = true }."""
        if isinstance(spec, str):
            return Tag(spec, None, False)

        if "name" not in spec:
            self._problems.append(f"{where}: tag {spec!r} has no name")

        return Tag(spec.get("name", ""), spec.get("emoji"), spec.get("moderated", False))

    def _thread(self, spec: dict[str, Any], where: str) -> Thread:
        return Thread(
            id=spec.get("id"),
            title=spec.get("title", ""),
            doc=self._doc(spec.get("doc", ""), where) or Doc(spec.get("doc", ""), ""),
            tags=tuple(spec.get("tags", [])),
            pin=spec.get("pin", False),
        )

    def _doc(self, path: str, where: str) -> Doc | None:
        try:
            text = (self._root / path).read_text(encoding="utf-8").strip()
        except OSError:
            self._problems.append(f"{where}: doc {path!r} not found")
            return None

        if not text:
            self._problems.append(f"{where}: doc {path!r} is empty")

        return Doc(path, text)

    def _flags(self, words: list[str], where: str) -> Permission:
        flags = Permission(0)
        for word in words:
            try:
                flags |= Permission[word.upper()]
            except KeyError:
                self._problems.append(f"{where}: unknown permission {word!r}")

        return flags


# ---------------------------------------------------------------- checks


def _check(blueprint: Blueprint, problems: list[str]) -> None:
    roles = _check_roles(blueprint.roles, problems)
    _check_presets(blueprint.presets, roles, problems)

    ids: set[str] = set()
    adopted: set[int] = set()
    count = 0
    for category in blueprint.categories:
        where = f"category {category.id or '?'}"
        _check_id(category.id, ids, where, problems)
        _check_name(category.name, where, problems)
        if category.access not in blueprint.presets:
            problems.append(f"{where}: unknown access preset {category.access!r}")

        if len(category.channels) > CATEGORY_CHILDREN:
            problems.append(f"{where}: {len(category.channels)} channels, Discord allows {CATEGORY_CHILDREN}")

        count += 1 + len(category.channels)
        for channel in category.channels:
            _check_channel(channel, category, blueprint, ids, problems)
            if not isinstance(channel.adopt, int):
                continue

            if channel.adopt in adopted:
                problems.append(f"channel {channel.id}: channel {channel.adopt} adopted twice")

            adopted.add(channel.adopt)

    if count > GUILD_CHANNELS:
        problems.append(f"{count} channels, Discord allows {GUILD_CHANNELS}")

    # Placeholders resolve against every id, so they are checked once all ids are known.
    for channel in blueprint.channels():
        for doc in channel.docs + tuple(t.doc for t in channel.threads):
            _check_refs(doc, ids, roles, problems)


def _check_roles(roles: tuple[Role, ...], problems: list[str]) -> set[str]:
    names: set[str] = set()
    for role in roles:
        where = f"role {role.name or '?'}"
        _check_name(role.name, where, problems)
        if role.name in names:
            problems.append(f"{where}: duplicate")

        if not 0 <= role.color <= COLOR_MAX:
            problems.append(f"{where}: color out of range")

        names.add(role.name)

    return names


def _check_presets(presets: dict[str, tuple[Overwrite, ...]], roles: set[str], problems: list[str]) -> None:
    for name, overwrites in presets.items():
        for ow in overwrites:
            if ow.role not in roles | {EVERYONE, GRANT}:
                problems.append(f"preset {name}: unknown role {ow.role!r}")

            if ow.allow & ow.deny:
                problems.append(f"preset {name}: {ow.role} both allows and denies {(ow.allow & ow.deny)!r}")


def _check_channel(channel: Channel, category: Category, blueprint: Blueprint, ids: set[str],
                   problems: list[str]) -> None:
    where = f"channel {channel.id or '?'}"
    _check_id(channel.id, ids, where, problems)
    _check_name(channel.name, where, problems)
    if channel.kind in _SLUGGED and (channel.name != channel.name.lower() or _SLUG_BREAKERS.search(channel.name)):
        problems.append(f"{where}: name {channel.name!r} must be lowercase without spaces or punctuation but - _")

    if channel.topic and len(channel.topic) > TOPIC_LIMIT:
        problems.append(f"{where}: topic over {TOPIC_LIMIT} characters")

    if not 0 <= channel.slowmode <= SLOWMODE_MAX:
        problems.append(f"{where}: slowmode out of range")

    if channel.adopt is not None and not isinstance(channel.adopt, int):
        problems.append(f"{where}: adopt must be a channel id")

    access = channel.access or category.access
    if access not in blueprint.presets:
        problems.append(f"{where}: unknown access preset {access!r}")
    elif channel.grant and not any(ow.role == GRANT for ow in blueprint.presets[access]):
        problems.append(f"{where}: grant needs a preset with {GRANT!r}, {access!r} has none")

    for name in channel.grant:
        if name not in {r.name for r in blueprint.roles}:
            problems.append(f"{where}: grant names unknown role {name!r}")

    if channel.docs and channel.kind not in _DOC_HOLDERS:
        problems.append(f"{where}: docs need a text or announcement channel")

    _check_forum(channel, where, ids, problems)


def _check_forum(channel: Channel, where: str, ids: set[str], problems: list[str]) -> None:
    if channel.kind is not ChannelType.FORUM:
        if channel.tags or channel.threads or channel.require_tag:
            problems.append(f"{where}: tags, threads and require_tag need a forum")
        return

    if len(channel.tags) > TAG_LIMIT:
        problems.append(f"{where}: {len(channel.tags)} tags, Discord allows {TAG_LIMIT}")

    names = [tag.name for tag in channel.tags]
    for name in names:
        if not 0 < len(name) <= TAG_NAME_LIMIT:
            problems.append(f"{where}: tag {name!r} must be 1-{TAG_NAME_LIMIT} characters")

    if len(set(names)) != len(names):
        problems.append(f"{where}: duplicate tag names")

    if channel.require_tag and not channel.tags:
        problems.append(f"{where}: require_tag needs tags")

    for thread in channel.threads:
        _check_name(thread.title, f"{where} thread", problems)
        if channel.require_tag and not thread.tags:
            problems.append(f"{where} thread {thread.title!r}: the forum requires a tag")

        unknown = set(thread.tags) - set(names)
        if unknown:
            problems.append(f"{where} thread {thread.title!r}: unknown tags {sorted(unknown)}")

        if thread.id is not None:
            _check_id(thread.id, ids, f"{where} thread {thread.title!r}", problems)


def _check_id(value: str, seen: set[str], where: str, problems: list[str]) -> None:
    if not _ID.match(value):
        problems.append(f"{where}: id {value!r} must be lowercase letters, digits and -")

    if value in seen:
        problems.append(f"{where}: duplicate id")

    seen.add(value)


def _check_name(value: str, where: str, problems: list[str]) -> None:
    if not 0 < len(value) <= NAME_LIMIT:
        problems.append(f"{where}: name must be 1-{NAME_LIMIT} characters")


def _check_refs(doc: Doc, ids: set[str], roles: set[str], problems: list[str]) -> None:
    for ref in CHANNEL_REF.findall(doc.text):
        if ref not in ids:
            problems.append(f"{doc.name}: unknown channel {{#{ref}}}")

    for ref in ROLE_REF.findall(doc.text):
        if ref not in roles | {EVERYONE}:
            problems.append(f"{doc.name}: unknown role {{@{ref}}}")
