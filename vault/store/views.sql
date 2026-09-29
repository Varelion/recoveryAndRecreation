-- Analytics views. Recreated on every open so edits here apply immediately.

DROP VIEW IF EXISTS v_messages;
CREATE VIEW v_messages AS
SELECT m.message_id, m.guild_id, m.created_at, date(m.created_at) AS day,
       m.channel_id, c.name AS channel, c.type AS channel_type, p.name AS parent,
       m.author_id, coalesce(u.global_name, u.username) AS author, u.bot,
       m.webhook_id, m.type, m.content, length(m.content) AS length,
       m.edited_at IS NOT NULL AS edited, m.pinned, m.ref_message_id,
       m.gone_at IS NOT NULL AS deleted,
       (SELECT count(*) FROM attachments a WHERE a.message_id = m.message_id) AS attachments,
       (SELECT coalesce(sum(r.count), 0) FROM reactions r WHERE r.message_id = m.message_id) AS reactions
FROM messages m
LEFT JOIN channels c ON c.channel_id = m.channel_id
LEFT JOIN channels p ON p.channel_id = c.parent_id
LEFT JOIN users u ON u.user_id = m.author_id;

DROP VIEW IF EXISTS v_daily_activity;
CREATE VIEW v_daily_activity AS
SELECT guild_id, date(created_at) AS day,
       count(*) AS messages,
       count(DISTINCT author_id) AS active_users,
       count(DISTINCT channel_id) AS active_channels
FROM messages
GROUP BY guild_id, day;

DROP VIEW IF EXISTS v_hourly_heatmap;
CREATE VIEW v_hourly_heatmap AS
SELECT guild_id,
       cast(strftime('%w', created_at) AS INTEGER) AS weekday,  -- 0 = Sunday
       cast(strftime('%H', created_at) AS INTEGER) AS hour_utc,
       count(*) AS messages
FROM messages
GROUP BY guild_id, weekday, hour_utc;

DROP VIEW IF EXISTS v_user_activity;
CREATE VIEW v_user_activity AS
SELECT m.guild_id, m.author_id, coalesce(u.global_name, u.username) AS author, u.bot,
       count(*) AS messages,
       sum(length(m.content)) AS characters,
       min(m.created_at) AS first_message,
       max(m.created_at) AS last_message,
       count(DISTINCT date(m.created_at)) AS active_days,
       count(DISTINCT m.channel_id) AS channels_used,
       mb.joined_at, mb.gone_at AS left_at
FROM messages m
LEFT JOIN users u ON u.user_id = m.author_id
LEFT JOIN members mb ON mb.guild_id = m.guild_id AND mb.user_id = m.author_id
GROUP BY m.guild_id, m.author_id;

DROP VIEW IF EXISTS v_channel_activity;
CREATE VIEW v_channel_activity AS
SELECT c.guild_id, c.channel_id, c.name, c.type, p.name AS parent,
       count(m.message_id) AS messages,
       count(DISTINCT m.author_id) AS authors,
       min(m.created_at) AS first_message,
       max(m.created_at) AS last_message
FROM channels c
LEFT JOIN channels p ON p.channel_id = c.parent_id
LEFT JOIN messages m ON m.channel_id = c.channel_id
GROUP BY c.channel_id;

DROP VIEW IF EXISTS v_top_reactions;
CREATE VIEW v_top_reactions AS
SELECT m.guild_id, r.emoji_key, r.emoji_id, r.emoji_name,
       sum(r.count) AS uses, count(DISTINCT r.message_id) AS messages
FROM reactions r
JOIN messages m ON m.message_id = r.message_id
GROUP BY m.guild_id, r.emoji_key;

-- Who replies to whom (social graph edges).
DROP VIEW IF EXISTS v_reply_graph;
CREATE VIEW v_reply_graph AS
SELECT m.guild_id, m.author_id AS from_user, t.author_id AS to_user, count(*) AS replies
FROM messages m
JOIN messages t ON t.message_id = m.ref_message_id
WHERE m.type = 19                                      -- REPLY
GROUP BY m.guild_id, from_user, to_user;

-- Who mentions whom.
DROP VIEW IF EXISTS v_mention_graph;
CREATE VIEW v_mention_graph AS
SELECT m.guild_id, m.author_id AS from_user, x.target_id AS to_user, count(*) AS mentions
FROM mentions x
JOIN messages m ON m.message_id = x.message_id
WHERE x.kind = 'user'
GROUP BY m.guild_id, from_user, to_user;

-- Joins and leaves per day. Leaves are only known from export/watch observations.
DROP VIEW IF EXISTS v_member_flow;
CREATE VIEW v_member_flow AS
SELECT guild_id, day, sum(joined) AS joined, sum(left_) AS left_
FROM (
    SELECT guild_id, date(joined_at) AS day, 1 AS joined, 0 AS left_ FROM members WHERE joined_at IS NOT NULL
    UNION ALL
    SELECT guild_id, date(gone_at), 0, 1 FROM members WHERE gone_at IS NOT NULL
)
GROUP BY guild_id, day;

DROP VIEW IF EXISTS v_role_members;
CREATE VIEW v_role_members AS
SELECT r.guild_id, r.role_id, r.name, r.position, count(mb.user_id) AS members
FROM roles r
LEFT JOIN member_roles mr ON mr.role_id = r.role_id
LEFT JOIN members mb ON mb.guild_id = mr.guild_id AND mb.user_id = mr.user_id AND mb.gone_at IS NULL
WHERE r.gone_at IS NULL
GROUP BY r.role_id;

DROP VIEW IF EXISTS v_voice_log;
CREATE VIEW v_voice_log AS
SELECT guild_id, at, user_id,
       json_extract(raw, '$.channel_id') AS channel_id,   -- NULL = left voice
       json_extract(raw, '$.self_mute') AS self_mute,
       json_extract(raw, '$.self_deaf') AS self_deaf,
       json_extract(raw, '$.self_stream') AS streaming
FROM gateway_events
WHERE kind = 'VOICE_STATE_UPDATE';
