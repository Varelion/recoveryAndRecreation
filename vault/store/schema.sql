-- discordVault schema.
--
-- Every entity row keeps `raw` (the exact Discord JSON) next to extracted
-- columns, so fields not modelled here are never lost.
-- `hash` detects change; each change also lands in `history`.
-- `gone_at` marks entities that disappeared (deleted, left, unbanned).
-- Timestamps are ISO-8601 UTC text; ids are Discord snowflakes.

PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------- bookkeeping

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- export | watch | import
    guild_id    INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,              -- running | ok | failed
    stats       TEXT                        -- JSON counters
);

-- Incremental cursors, e.g. ('messages', channel_id) -> newest stored id.
CREATE TABLE IF NOT EXISTS cursors (
    scope      TEXT NOT NULL,
    key        INTEGER NOT NULL,
    value      INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- Every observed version of every entity (append-only).
CREATE TABLE IF NOT EXISTS history (
    kind        TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    raw         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_history ON history (kind, entity_id, observed_at);

-- ---------------------------------------------------------------- guild

CREATE TABLE IF NOT EXISTS guilds (
    guild_id     INTEGER PRIMARY KEY,
    name         TEXT,
    owner_id     INTEGER,
    created_at   TEXT,
    premium_tier INTEGER,
    features     TEXT,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

-- Time series sampled on each export.
CREATE TABLE IF NOT EXISTS guild_stats (
    guild_id       INTEGER NOT NULL,
    observed_at    TEXT NOT NULL,
    member_count   INTEGER,
    presence_count INTEGER,
    boost_count    INTEGER,
    PRIMARY KEY (guild_id, observed_at)
);

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    global_name TEXT,
    bot         INTEGER,
    avatar      TEXT,
    created_at  TEXT,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

CREATE TABLE IF NOT EXISTS members (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    nick          TEXT,
    joined_at     TEXT,
    premium_since TEXT,
    timeout_until TEXT,
    pending       INTEGER,
    flags         INTEGER,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS member_roles (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, role_id)
);

CREATE TABLE IF NOT EXISTS roles (
    role_id       INTEGER PRIMARY KEY,
    guild_id      INTEGER NOT NULL,
    name          TEXT,
    color         INTEGER,
    position      INTEGER,
    permissions   TEXT,                      -- bitfield as decimal string
    hoist         INTEGER,
    mentionable   INTEGER,
    managed       INTEGER,
    unicode_emoji TEXT,
    created_at    TEXT,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

-- Channels, categories, threads and forum posts.
CREATE TABLE IF NOT EXISTS channels (
    channel_id    INTEGER PRIMARY KEY,
    guild_id      INTEGER NOT NULL,
    type          INTEGER NOT NULL,
    name          TEXT,
    parent_id     INTEGER,
    position      INTEGER,
    topic         TEXT,
    nsfw          INTEGER,
    owner_id      INTEGER,                   -- thread creator
    archived      INTEGER,
    locked        INTEGER,
    message_count INTEGER,
    created_at    TEXT,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

CREATE TABLE IF NOT EXISTS overwrites (
    channel_id  INTEGER NOT NULL,
    target_id   INTEGER NOT NULL,
    target_type INTEGER NOT NULL,            -- 0 role, 1 member
    allow       TEXT NOT NULL,
    deny        TEXT NOT NULL,
    PRIMARY KEY (channel_id, target_id)
);

CREATE TABLE IF NOT EXISTS emojis (
    emoji_id   INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    name       TEXT,
    animated   INTEGER,
    creator_id INTEGER,
    created_at TEXT,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

CREATE TABLE IF NOT EXISTS stickers (
    sticker_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    name        TEXT,
    tags        TEXT,
    format_type INTEGER,
    creator_id  INTEGER,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

CREATE TABLE IF NOT EXISTS scheduled_events (
    event_id   INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER,
    creator_id INTEGER,
    name       TEXT,
    start_at   TEXT,
    end_at     TEXT,
    status     INTEGER,
    user_count INTEGER,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

CREATE TABLE IF NOT EXISTS automod_rules (
    rule_id    INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    name       TEXT,
    event_type INTEGER,
    enabled    INTEGER,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER,
    inviter_id INTEGER,
    uses       INTEGER,
    max_uses   INTEGER,
    created_at TEXT,
    expires_at TEXT,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

CREATE TABLE IF NOT EXISTS webhooks (
    webhook_id INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER,
    name       TEXT,
    creator_id INTEGER,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);

-- gone_at = unbanned.
CREATE TABLE IF NOT EXISTS bans (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    reason   TEXT,
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id    INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    action_type INTEGER NOT NULL,
    user_id     INTEGER,
    target_id   INTEGER,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    changes     TEXT,
    options     TEXT,
    raw         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit ON audit_log (guild_id, action_type, created_at);

-- ---------------------------------------------------------------- messages

CREATE TABLE IF NOT EXISTS messages (
    message_id       INTEGER PRIMARY KEY,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    author_id        INTEGER,
    webhook_id       INTEGER,
    application_id   INTEGER,
    type             INTEGER,
    content          TEXT,
    created_at       TEXT NOT NULL,
    edited_at        TEXT,
    pinned           INTEGER,
    tts              INTEGER,
    mention_everyone INTEGER,
    flags            INTEGER,
    ref_message_id   INTEGER,                -- reply / forward / crosspost source
    ref_channel_id   INTEGER,
    thread_id        INTEGER,                -- thread started from this message
    interaction_user INTEGER,
    interaction_name TEXT,
    poll             TEXT,                   -- JSON
    raw TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL, gone_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_messages_channel ON messages (channel_id, message_id);
CREATE INDEX IF NOT EXISTS ix_messages_author ON messages (author_id, created_at);
CREATE INDEX IF NOT EXISTS ix_messages_time ON messages (guild_id, created_at);

-- Prior content of edited messages.
CREATE TABLE IF NOT EXISTS message_versions (
    message_id  INTEGER NOT NULL,
    edited_at   TEXT,
    content     TEXT,
    observed_at TEXT NOT NULL,
    raw         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_versions ON message_versions (message_id);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id INTEGER PRIMARY KEY,
    message_id    INTEGER NOT NULL,
    filename      TEXT,
    content_type  TEXT,
    size          INTEGER,
    width         INTEGER,
    height        INTEGER,
    duration      REAL,
    description   TEXT,
    url           TEXT,
    sha256        TEXT,                      -- set once downloaded
    raw           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attachments_message ON attachments (message_id);

-- Downloaded files, deduplicated by content hash.
CREATE TABLE IF NOT EXISTS media (
    sha256 TEXT PRIMARY KEY,
    path   TEXT NOT NULL,
    size   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS embeds (
    message_id INTEGER NOT NULL,
    idx        INTEGER NOT NULL,
    type       TEXT,
    title      TEXT,
    url        TEXT,
    raw        TEXT NOT NULL,
    PRIMARY KEY (message_id, idx)
);

-- emoji_key: unicode char or "name:id".
CREATE TABLE IF NOT EXISTS reactions (
    message_id  INTEGER NOT NULL,
    emoji_key   TEXT NOT NULL,
    emoji_id    INTEGER,
    emoji_name  TEXT,
    count       INTEGER,
    burst_count INTEGER,
    PRIMARY KEY (message_id, emoji_key)
);

CREATE TABLE IF NOT EXISTS reaction_users (
    message_id INTEGER NOT NULL,
    emoji_key  TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    burst      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, emoji_key, user_id, burst)
);

-- kind: user | role | channel
CREATE TABLE IF NOT EXISTS mentions (
    message_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    target_id  INTEGER NOT NULL,
    PRIMARY KEY (message_id, kind, target_id)
);

CREATE TABLE IF NOT EXISTS message_stickers (
    message_id INTEGER NOT NULL,
    sticker_id INTEGER NOT NULL,
    name       TEXT,
    PRIMARY KEY (message_id, sticker_id)
);

-- ---------------------------------------------------------------- live feed

-- Raw gateway events (joins, leaves, voice, deletes...) from `watch`.
CREATE TABLE IF NOT EXISTS gateway_events (
    event_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER,
    kind       TEXT NOT NULL,
    at         TEXT NOT NULL,
    channel_id INTEGER,
    user_id    INTEGER,
    raw        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gateway ON gateway_events (guild_id, kind, at);

-- ---------------------------------------------------------------- import

-- Source -> target id mapping; makes imports resumable and idempotent.
CREATE TABLE IF NOT EXISTS import_map (
    target_guild INTEGER NOT NULL,
    kind         TEXT NOT NULL,              -- role | channel | emoji | message | webhook
    source_id    INTEGER NOT NULL,
    target_id    INTEGER NOT NULL,
    extra        TEXT,                       -- e.g. webhook token
    PRIMARY KEY (target_guild, kind, source_id)
);
