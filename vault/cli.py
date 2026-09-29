"""Command line entry point.

    python -m vault export  <guild>            full, then incremental
    python -m vault watch   <guild>            catch up, then record live
    python -m vault import  <source> <target>  recreate into another guild
    python -m vault dump    --out dir          CSV/JSONL of all tables + views
    python -m vault stats                      row counts
    python -m vault bot                        slash commands with live progress
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path

from vault import config
from vault.api import Link
from vault.config import Media, Messages, Mode, ReactionUsers
from vault.services.dump import Format, dump
from vault.services.exporter import ExportOptions
from vault.services.importer import ImportOptions
from vault.services.vault import Vault
from vault.store import Store
from vault.ui.bot import Tracker, register
from vault.ui.console import follow


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vault", description="Discord guild archive for analytics and recreation.")
    root.add_argument("--db", type=Path, default=config.DEFAULT_DB, help="SQLite file (default: vault.db)")
    root.add_argument("-v", "--verbose", action="store_true")
    sub = root.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Export a guild (incremental after the first run)")
    export.add_argument("guild", type=int)
    _export_flags(export)

    watch = sub.add_parser("watch", help="Record live events; runs an incremental export first")
    watch.add_argument("guild", type=int)
    watch.add_argument("--no-catch-up", action="store_true", help="Skip the initial export")
    _export_flags(watch)

    imp = sub.add_parser("import", help="Recreate a stored guild in another guild")
    imp.add_argument("source", type=int, help="Guild id stored in the database")
    imp.add_argument("target", type=int, help="Guild to build into (bot needs Administrator)")
    imp.add_argument("--channel", type=int, action="append", default=[], help="Limit to channel/category (repeat)")
    imp.add_argument("--skip-messages", action="store_true", help="Structure only")
    imp.add_argument("--dry-run", action="store_true", help="Count what would be created")

    out = sub.add_parser("dump", help="Write every table and view as CSV or JSONL")
    out.add_argument("--out", type=Path, required=True)
    out.add_argument("--format", choices=[f.value for f in Format], default=Format.CSV.value)

    stats = sub.add_parser("stats", help="Row counts")
    stats.add_argument("--guild", type=int)

    bot = sub.add_parser("bot", help="Serve /vault slash commands; resumes active watches")
    bot.add_argument("--media-dir", type=Path, default=Path(config.MEDIA_DIR))
    return root


def _export_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--media", action="store_true", help="Download attachments (deduplicated)")
    parser.add_argument("--media-dir", type=Path, default=Path(config.MEDIA_DIR))
    parser.add_argument("--reaction-users", action="store_true", help="Record who reacted (slow: 1 call/emoji)")
    parser.add_argument("--rescan-days", type=int, default=0,
                        help="Re-read recent N days to catch edits, reactions, deletions")
    parser.add_argument("--channel", type=int, action="append", default=[], help="Limit to channel/category (repeat)")
    parser.add_argument("--workers", type=int, default=4, help="Channels fetched in parallel")


def _export_options(args: argparse.Namespace) -> ExportOptions:
    return ExportOptions(
        media=Media.DOWNLOAD if args.media else Media.SKIP,
        reaction_users=ReactionUsers.FETCH if args.reaction_users else ReactionUsers.SKIP,
        rescan_days=args.rescan_days,
        channels=set(args.channel),
        workers=args.workers,
        media_root=args.media_dir,
    )


async def _run(args: argparse.Namespace) -> None:
    store = await Store.open(args.db)
    try:
        await _dispatch(args, store)
    finally:
        await store.close()


async def _dispatch(args: argparse.Namespace, store: Store) -> None:
    if args.command == "dump":
        counts = await dump(store, args.out, Format(args.format))
        print(json.dumps(counts, indent=2))
        return

    if args.command == "stats":
        print(json.dumps(await store.counts(args.guild), indent=2))
        return

    async with Link(config.token()) as link:
        vault = Vault(link, store)
        if args.command == "export":
            print(json.dumps(await follow(vault.export(args.guild, _export_options(args))), indent=2))
            return

        if args.command == "import":
            options = ImportOptions(
                messages=Messages.SKIP if args.skip_messages else Messages.INCLUDE,
                mode=Mode.DRY_RUN if args.dry_run else Mode.LIVE,
                channels=set(args.channel),
            )
            print(json.dumps(await follow(vault.import_guild(args.source, args.target, options)), indent=2))
            return

        if args.command == "watch":
            if not args.no_catch_up:
                await follow(vault.export(args.guild, _export_options(args)))
            await _online(link, follow(await vault.watch(args.guild)))
            return

        register(link.tree, vault, Tracker(link.rest), args.media_dir)
        await link.sync_commands()
        await vault.resume_watches()
        await _online(link, asyncio.Event().wait())


async def _online(link: Link, work) -> None:
    """Run work while the gateway is connected; whichever ends first stops both."""
    tasks = {asyncio.ensure_future(link.connect()), asyncio.ensure_future(work)}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()

    for task in done:
        task.result()


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # discord.py is chatty at INFO about gateway internals.
    logging.getLogger("discord").setLevel(logging.WARNING)

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
