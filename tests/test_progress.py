import asyncio

import discord
import pytest
from discord import app_commands

from fakes import GUILD, TEXT, FakeRest, message
from vault.services.exporter import ExportOptions, Exporter
from vault.services.jobs import Busy, Jobs
from vault.services.progress import Progress, Status
from vault.services.vault import Vault
from vault.store import Store
from vault.ui import render
from vault.ui.bot import Tracker, register


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


def test_weighted_fraction_and_eta():
    p = Progress("x", {"a": 1, "b": 3})
    p.phase("a", 10)
    p.advance(5)
    assert p.fraction() == pytest.approx(0.125)

    p.phase("b", 4)
    p.advance(2)
    assert p.fraction() == pytest.approx(0.625)
    assert p.snapshot().eta is not None

    p.end(Status.DONE)
    assert p.fraction() == 1.0 and p.snapshot().eta is None


def test_open_ended_progress_has_no_fraction():
    p = Progress("watch")
    p.phase("listening")
    assert p.fraction() is None
    assert "live" in render.line(1, p.snapshot())


def test_render_embed():
    p = Progress("export", {"messages": 1})
    p.phase("messages", 4)
    p.advance(1)
    p.detail("#general")
    p.counters["messages_seen"] = 1234

    e = render.embed(7, p.snapshot())

    assert e["title"] == "Export · job #7"
    assert "25%" in e["description"]
    assert "#general" in e["fields"][0]["value"]
    assert "1,234" in e["fields"][2]["value"]


async def test_jobs_lifecycle():
    jobs = Jobs()
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return "ok"

    job = jobs.start("export", 1, None, Progress("export"), work())
    with pytest.raises(Busy):
        jobs.start("export", 1, None, Progress("export"), work())

    other = jobs.start("export", 2, None, Progress("export"), work())
    assert jobs.cancel(other.id)

    gate.set()
    await asyncio.sleep(0)
    await asyncio.gather(job.task, return_exceptions=True)
    await asyncio.gather(other.task, return_exceptions=True)

    assert job.progress.status is Status.DONE
    assert other.progress.status is Status.CANCELLED
    assert [j.id for j in jobs.list(1)] == [job.id]


async def test_failed_job_reports_error():
    jobs = Jobs()

    async def boom():
        raise ValueError("bad")

    job = jobs.start("import", 1, None, Progress("import"), boom())
    await asyncio.gather(job.task, return_exceptions=True)

    assert job.progress.status is Status.FAILED
    assert "bad" in job.progress.snapshot().error


async def test_export_progress_reaches_full(store):
    rest = FakeRest()
    rest.channels_data[0]["last_message_id"] = "102"
    rest.msgs[TEXT] = [message(100, TEXT), message(101, TEXT), message(102, TEXT)]
    progress = Progress("export", {"messages": 1})
    seen = []

    # Record the fraction after each stored page.
    original = rest.messages

    async def spy(cid, after):
        async for page in original(cid, after):
            yield page
            seen.append(progress.fraction())

    rest.messages = spy
    await Exporter(rest, store, GUILD, ExportOptions(), progress).run()

    assert seen and seen[-1] == pytest.approx(1.0)
    assert progress.counters["messages_seen"] == 3


class _Link:
    def __init__(self, rest):
        self.rest = rest


async def test_tracker_posts_and_finalizes(store):
    rest = FakeRest()
    sent, edits = [], []

    async def send_message(cid, payload):
        sent.append(payload)
        return {"id": "1"}

    async def edit_message(cid, mid, payload):
        edits.append(payload)

    rest.send_message, rest.edit_message = send_message, edit_message
    vault = Vault(_Link(rest), store)
    job = vault.export(GUILD, ExportOptions())

    Tracker(rest).follow(job, TEXT)
    await job.task
    for _ in range(5):
        await asyncio.sleep(0)

    assert sent
    assert edits[-1]["embeds"][0]["footer"]["text"] == "done"


async def test_slash_commands_register(store, tmp_path):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register(tree, Vault(_Link(FakeRest()), store), Tracker(FakeRest()), tmp_path)

    group = tree.get_command("vault")
    names = sorted(c.name for c in group.commands)
    payload = group.to_dict(tree)

    assert names == ["cancel", "export", "import", "jobs", "progress", "stats", "watch"]
    export = next(o for o in payload["options"] if o["name"] == "export")
    media = next(o for o in export["options"] if o["name"] == "media")
    assert {c["value"] for c in media["choices"]} == {"skip", "download"}
