import asyncio

import pytest

from fakes import GUILD, TEXT, FakeRest, message
from vault.services.exporter import ExportOptions, Exporter
from vault.services.jobs import Busy, Jobs
from vault.services.progress import Progress, Status
from vault.store import Store
from vault.ui import render


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

