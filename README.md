# discordVault

Archives a Discord guild into SQLite for analytics, keeps it current
incrementally, and recreates it in another guild.

```
   ui:                    cli.py (console progress)
                                       │
   facade:                  Vault ── Jobs (task + Progress per action)
                                       │
   services:  Exporter   Watcher   Importer   Downloader   dump
                 │          │          │           │         │
   drivers:   api.Rest ◄────┴──────────┘───────────┘         │
              api.Link (gateway)                             │
                 │                                           │
   store:     Store ──► SQLite (tables + history + views) ◄──┘
```

## Setup

```
pip install -r requirements.txt
copy .env.example .env      # set DISCORD_TOKEN
```

Bot, in the Developer Portal:
- Privileged intents: **Server Members** and **Message Content**.
- Source guild permissions: View Channels, Read Message History, View Audit Log,
  Manage Guild (invites), Manage Webhooks (webhook list), Ban Members (ban list),
  Manage Threads (private archived threads). Anything missing is skipped and counted.
- Target guild (import): Administrator.
- Invite with scope `bot`. It registers no slash commands: the bot's Discord application
  may be shared with another bot (Courts Unbound's Bot2), whose commands stay its own.

## Commands

```
python -m vault export <guild> [--no-media] [--no-reaction-users] [--rescan-days N] [--channel ID]...
python -m vault watch  <guild> [--no-catch-up] [export flags]
python -m vault import <source> <target> [--dry-run] [--skip-messages] [--channel ID]... [--into ID]
python -m vault build  <guild> <blueprint-dir> [--dry-run] [--prune-roles] [--prune-channels]
python -m vault dump --out dir [--format csv|jsonl]
python -m vault stats [--guild ID]
```

`--db path` (before the command) picks the database; default `vault.db`.
CLI jobs print a progress line every 5 s.

## What is collected

Guild, member-count time series, roles, channels, categories, threads,
forum posts, permission overwrites, members + their roles, users, emojis,
stickers, scheduled events, automod rules, invites, webhooks, bans, audit log,
messages (with attachments, embeds, reactions, mentions, stickers, polls,
replies, interactions), optional reaction users, optional attachment files.

Every row keeps `raw`, the exact Discord JSON, so nothing is dropped.
Each content change is appended to `history`; edited message text goes to
`message_versions`. Vanished entities get `gone_at` (deleted, member left, unbanned).

## Incremental

- `export`: per-channel cursor = newest stored message id. Re-runs fetch only
  newer messages; channels with no new activity cost no request. Interrupted
  runs resume from the last committed page.
- `--rescan-days N`: re-reads the last N days to catch edits, reaction changes
  and deletions that happened while nothing was listening.
- `watch`: catches up with an export, then records gateway events live:
  edits, deletes, reactions (with time), joins/leaves, role and channel
  changes, voice activity, audit entries. Raw events go to `gateway_events`.
  Events between the catch-up and the gateway connecting are picked up by
  the next export.

Suggested routine: keep `watch` running; schedule `export --rescan-days 7` daily.

## Analytics views

`v_messages`, `v_daily_activity`, `v_hourly_heatmap`, `v_user_activity`,
`v_channel_activity`, `v_top_reactions`, `v_reply_graph`, `v_mention_graph`,
`v_member_flow`, `v_role_members`, `v_voice_log`. `dump` writes them next to
the tables for pandas, DuckDB or spreadsheets.

## Import limits

- Webhooks show original name and avatar but cannot backdate; the original time
  is appended as a subtext line.
- Reactions, polls, stickers and component buttons are not replayed.
- Attachments come from media downloads (skipped by `--no-media`); otherwise the stored CDN link is
  tried and usually has expired.
- Forum/announcement/stage channels need a Community guild; they fall back to
  text/voice otherwise.
- Resumable: every created object is recorded in `import_map`; re-running continues.

## Build

`build` converges a guild onto a blueprint (`server.toml` + markdown docs, see
`blueprints/courts_unbound`). Re-running edits only what changed; docs are
re-synced message by message. Roles absent from the blueprint are deleted only
with `--prune-roles`; channels the build made and the blueprint dropped (or
replaced, since Discord cannot change a channel's type) only with
`--prune-channels`. Channels the build never made are never touched. The bot's
role must sit above every role it edits or deletes.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```
