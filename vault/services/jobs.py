"""Background jobs: each action runs as an asyncio task with its own Progress.

    start() ─► Job(id, kind, guild, progress, task) ─► final Status set inside the task,
    so anyone awaiting the task already sees it.
"""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from vault.services.progress import Progress, Status

log = logging.getLogger(__name__)

KEEP_FINISHED = 50


class Busy(Exception):
    """Same kind of job already running for that guild."""

    def __init__(self, job: "Job") -> None:
        super().__init__(f"{job.kind} job #{job.id} already running")
        self.job = job


@dataclass
class Job:
    id: int
    kind: str
    guild_id: int
    by: int | None  # user who started it
    progress: Progress
    task: asyncio.Task = field(repr=False)

    @property
    def running(self) -> bool:
        return not self.task.done()


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._next = 1

    def start(self, kind: str, guild_id: int, by: int | None, progress: Progress,
              work: Coroutine[Any, Any, Any]) -> Job:
        running = self.running(kind, guild_id)
        if running:
            work.close()
            raise Busy(running)

        job = Job(self._next, kind, guild_id, by, progress, asyncio.create_task(_tracked(work, progress)))
        # Safety net for task deaths _tracked could not observe.
        job.task.add_done_callback(lambda task: self._finish(job, task, work))
        self._jobs[job.id] = job
        self._next += 1
        self._prune()
        return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, guild_id: int | None = None) -> list[Job]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)
        if guild_id is None:
            return jobs

        return [j for j in jobs if j.guild_id == guild_id]

    def running(self, kind: str, guild_id: int) -> Job | None:
        return next((j for j in self._jobs.values() if j.running and j.kind == kind and j.guild_id == guild_id), None)

    def cancel(self, job_id: int) -> bool:
        job = self._jobs.get(job_id)
        if job is None or not job.running:
            return False

        # Mark now: a task cancelled before it started never runs _tracked.
        job.task.cancel()
        job.progress.end(Status.CANCELLED)
        return True

    @staticmethod
    def _finish(job: Job, task: asyncio.Task, work: Coroutine[Any, Any, Any]) -> None:
        # No-op if it ran; silences "never awaited" if cancelled before starting.
        work.close()
        if job.progress.status is not Status.RUNNING:
            return

        job.progress.end(Status.CANCELLED if task.cancelled() else Status.FAILED)

    def _prune(self) -> None:
        # Bound memory: drop the oldest finished jobs.
        finished = [j for j in sorted(self._jobs.values(), key=lambda j: j.id) if not j.running]
        for job in finished[:-KEEP_FINISHED]:
            del self._jobs[job.id]


async def _tracked(work: Coroutine[Any, Any, Any], progress: Progress) -> Any:
    try:
        result = await work
    except asyncio.CancelledError:
        progress.end(Status.CANCELLED)
        raise
    except Exception as error:
        log.exception("%s job failed", progress.kind)
        progress.end(Status.FAILED, f"{type(error).__name__}: {error}")
        raise

    progress.end(Status.DONE)
    return result
