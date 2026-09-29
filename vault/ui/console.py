"""Terminal progress: prints a status line while a job runs."""

import asyncio
from typing import Any

from vault.services.jobs import Job
from vault.ui.render import line

PRINT_INTERVAL = 5


async def follow(job: Job) -> Any:
    """Print progress until the job ends; returns its result or re-raises its error."""
    while not job.task.done():
        await asyncio.wait({job.task}, timeout=PRINT_INTERVAL)
        print(line(job.id, job.progress.snapshot()), flush=True)

    return job.task.result()
