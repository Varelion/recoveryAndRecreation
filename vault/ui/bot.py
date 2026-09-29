"""Slash commands and live progress messages.

    /vault export | import | watch     start a job, post a live progress message
    /vault jobs                        list this server's jobs
    /vault progress <job>              post another live progress message for any job
    /vault cancel <job>                stop a job
    /vault stats                       row counts

Commands are server-only and default to Administrator; server owners can
change who sees them under Server Settings > Integrations.
"""

import asyncio
import json
import logging
from enum import Enum
from pathlib import Path

import discord
from discord import app_commands

from vault.api import Denied, Missing, Rest
from vault.config import Media, Messages, Mode, ReactionUsers
from vault.services.exporter import ExportOptions
from vault.services.importer import ImportOptions
from vault.services.jobs import Busy, Job
from vault.services.vault import Vault
from vault.ui import render

log = logging.getLogger(__name__)

EDIT_INTERVAL = 5  # seconds between progress edits; Discord allows 5 edits / 5 s per channel
MAX_CHOICES = 25   # Discord autocomplete limit
MAX_RESCAN_DAYS = 3650


class WatchAction(Enum):
    START = "start"
    STOP = "stop"


class Tracker:
    """Keeps one channel message per (job, request) in sync with the job's progress."""

    def __init__(self, rest: Rest) -> None:
        self._rest = rest
        self._tasks: set[asyncio.Task] = set()

    def follow(self, job: Job, channel_id: int) -> None:
        task = asyncio.create_task(self._run(job, channel_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: Job, channel_id: int) -> None:
        try:
            message = await self._rest.send_message(channel_id, self._payload(job))
        except (Denied, Missing):
            log.warning("Cannot post progress in channel %s", channel_id)
            return

        while True:
            # Read "done" before rendering so the final state is always shown.
            done = job.task.done()
            try:
                await self._rest.edit_message(channel_id, int(message["id"]), self._payload(job))
            except (Denied, Missing):
                # Message deleted by someone: stop tracking, job keeps running.
                return

            if done:
                return

            await asyncio.wait({job.task}, timeout=EDIT_INTERVAL)

    @staticmethod
    def _payload(job: Job) -> dict:
        return {"embeds": [render.embed(job.id, job.progress.snapshot())]}


def register(tree: app_commands.CommandTree, vault: Vault, tracker: Tracker, media_root: Path) -> None:
    group = app_commands.Group(
        name="vault",
        description="Archive, analyse and recreate this server",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    async def started(interaction: discord.Interaction, job: Job) -> None:
        await interaction.response.send_message(f"Started {job.kind} job #{job.id}.", ephemeral=True)
        tracker.follow(job, interaction.channel_id)

    async def busy(interaction: discord.Interaction, error: Busy) -> None:
        await interaction.response.send_message(
            f"{error}. Use `/vault progress {error.job.id}` to follow it.", ephemeral=True
        )

    def own_job(interaction: discord.Interaction, job_id: int) -> Job | None:
        # Jobs of other servers stay invisible.
        job = vault.jobs.get(job_id)
        return job if job and job.guild_id == interaction.guild_id else None

    async def job_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        jobs = [j for j in vault.jobs.list(interaction.guild_id) if current in str(j.id)]
        return [
            app_commands.Choice(name=f"#{j.id} {j.kind} · {j.progress.status.value}", value=j.id)
            for j in jobs[:MAX_CHOICES]
        ]

    @group.command(description="Export this server; incremental after the first run")
    @app_commands.describe(
        media="Download attachments",
        reaction_users="Record who reacted (slow)",
        rescan_days="Re-read the last N days to catch edits, reactions and deletions",
    )
    async def export(
        interaction: discord.Interaction,
        media: Media = Media.SKIP,
        reaction_users: ReactionUsers = ReactionUsers.SKIP,
        rescan_days: app_commands.Range[int, 0, MAX_RESCAN_DAYS] = 0,
    ) -> None:
        options = ExportOptions(media=media, reaction_users=reaction_users, rescan_days=rescan_days,
                                media_root=media_root)
        try:
            job = vault.export(interaction.guild_id, options, interaction.user.id)
        except Busy as error:
            await busy(interaction, error)
            return

        await started(interaction, job)

    @group.command(name="import", description="Recreate a stored server inside this one")
    @app_commands.describe(
        source="Id of a server already exported to the database",
        mode="DRY_RUN only counts what would be created",
        messages="SKIP recreates structure only",
    )
    async def import_(
        interaction: discord.Interaction,
        source: str,
        mode: Mode = Mode.DRY_RUN,
        messages: Messages = Messages.INCLUDE,
    ) -> None:
        if not source.isdigit() or not await vault.known(int(source)):
            await interaction.response.send_message("Unknown source: export it first.", ephemeral=True)
            return

        if int(source) == interaction.guild_id:
            await interaction.response.send_message("Source and target are the same server.", ephemeral=True)
            return

        options = ImportOptions(messages=messages, mode=mode)
        try:
            job = vault.import_guild(int(source), interaction.guild_id, options, interaction.user.id)
        except Busy as error:
            await busy(interaction, error)
            return

        await started(interaction, job)

    @group.command(description="Start or stop recording live events of this server")
    async def watch(interaction: discord.Interaction, action: WatchAction) -> None:
        if action is WatchAction.STOP:
            stopped = await vault.unwatch(interaction.guild_id)
            await interaction.response.send_message("Stopped." if stopped else "Not watching.", ephemeral=True)
            return

        try:
            job = await vault.watch(interaction.guild_id, interaction.user.id)
        except Busy as error:
            await busy(interaction, error)
            return

        await started(interaction, job)

    @group.command(description="List this server's jobs")
    async def jobs(interaction: discord.Interaction) -> None:
        lines = [render.line(j.id, j.progress.snapshot()) for j in vault.jobs.list(interaction.guild_id)]
        text = "\n".join(lines) or "No jobs."
        await interaction.response.send_message(f"```\n{text[:1900]}\n```", ephemeral=True)

    @group.command(description="Post a live progress message for a job")
    @app_commands.autocomplete(job=job_choices)
    async def progress(interaction: discord.Interaction, job: int) -> None:
        found = own_job(interaction, job)
        if found is None:
            await interaction.response.send_message(f"No job #{job} here.", ephemeral=True)
            return

        await interaction.response.send_message(f"Tracking job #{job}.", ephemeral=True)
        tracker.follow(found, interaction.channel_id)

    @group.command(description="Cancel a running job")
    @app_commands.autocomplete(job=job_choices)
    async def cancel(interaction: discord.Interaction, job: int) -> None:
        found = own_job(interaction, job)
        cancelled = found is not None and vault.jobs.cancel(found.id)
        await interaction.response.send_message(
            f"Cancelled job #{job}." if cancelled else f"Job #{job} is not running here.", ephemeral=True
        )

    @group.command(description="Row counts stored for this server")
    async def stats(interaction: discord.Interaction) -> None:
        counts = await vault.stats(interaction.guild_id)
        await interaction.response.send_message(f"```json\n{json.dumps(counts, indent=2)}\n```", ephemeral=True)

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("Command failed", exc_info=error)
        reply = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await reply(f"Failed: {error}", ephemeral=True)

    tree.add_command(group)
