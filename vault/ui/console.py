"""Terminal progress: prints a status line while a job runs."""

import asyncio
import sys
from typing import Any

from vault.services.jobs import Job
from vault.ui.render import line

PRINT_INTERVAL = 5
UTF8 = "utf-8"


async def follow(job: Job) -> Any:
    """Print progress until the job ends; returns its result or re-raises its error."""
    # Windows consoles default to cp1252, which lacks bar glyphs and emoji channel names.
    if sys.stdout.encoding.lower() != UTF8:
        sys.stdout.reconfigure(encoding=UTF8)

    while not job.task.done():
        await asyncio.wait({job.task}, timeout=PRINT_INTERVAL)
        print(line(job.id, job.progress.snapshot()), flush=True)

    return job.task.result()
