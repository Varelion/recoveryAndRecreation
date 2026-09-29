# discordVault

Archives a Discord guild into SQLite for analytics, keeps it current
incrementally, and recreates it in another guild.

```
   ui:        cli.py (console progress)      ui/bot.py (/vault commands, live progress messages)
                        └──────────────┬──────────────┘
   facade:                  Vault ── Jobs (task + Progress per action)
                                       │
   services:  Exporter   Watcher   Importer   Downloader   dump
                 │          │          │           │         │
   drivers:   api.Rest ◄────┴──────────┘───────────┘         │
              api.Link (gateway, command tree)               │
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
- Invite with scopes `bot` and `applications.commands` to use slash commands.

## Commands

```
python -m vault export <guild> [--media] [--reaction-users] [--rescan-days N] [--channel ID]...
python -m vault watch  <guild> [--no-catch-up] [export flags]
python -m vault import <source> <target> [--dry-run] [--skip-messages] [--channel ID]...
python -m vault dump --out dir [--format csv|jsonl]
python -m vault stats [--guild ID]
```

`--db path` (before the command) picks the database; default `vault.db`.
CLI jobs print a progress line every 5 s.

## From the server

```
python -m vault bot
```

Serves slash commands in every guild the bot is in. Every action runs as a
background job with a live progress message (bar, %, ETA, phase, counters)
edited every 5 s in the channel where it was started.

| Command | Does |
|---|---|
| `/vault export [media] [reaction_users] [rescan_days]` | Export this server |
| `/vault import source:<id> [mode] [messages]` | Recreate a stored server here. Defaults to `DRY_RUN`. |
| `/vault watch action:start\|stop` | Record live events; resumed after a bot restart |
| `/vault jobs` | This server's jobs with status and % |
| `/vault progress job:<#>` | Post another live tracker for any job (autocomplete) |
| `/vault cancel job:<#>` | Stop a job; it resumes where it stopped if started again |
| `/vault stats` | Row counts |

Commands default to Administrator; change who sees them under
Server Settings > Integrations. One job per kind per server runs at a time.
Job history lives in memory (last 50); the `runs` table keeps a permanent log.

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
- Attachments come from `--media` downloads; otherwise the stored CDN link is
  tried and usually has expired.
- Forum/announcement/stage channels need a Community guild; they fall back to
  text/voice otherwise.
- Resumable: every created object is recorded in `import_map`; re-running continues.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```
