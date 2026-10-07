"""Progress snapshot -> console text line.

    export #3  ▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱  62%  messages · #general  3m 10s  ETA 1m 55s
"""

from vault.services.progress import Snapshot, Status

BAR_WIDTH = 20
FULL, EMPTY = "▰", "▱"


def duration(seconds: float) -> str:
    """e.g. 42 -> "42s", 190 -> "3m 10s", 3725 -> "1h 02m"."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"

    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"

    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def bar(fraction: float | None) -> str:
    if fraction is None:
        return "live"

    filled = round(fraction * BAR_WIDTH)
    return f"{FULL * filled}{EMPTY * (BAR_WIDTH - filled)}  {fraction:.0%}"


def _where(snap: Snapshot) -> str:
    return f"{snap.phase} · {snap.detail}" if snap.detail else snap.phase


def _timing(snap: Snapshot) -> str:
    parts = [duration(snap.elapsed)]
    if snap.eta is not None:
        parts.append(f"ETA {duration(snap.eta)}")

    return "  ".join(parts)


def line(job_id: int, snap: Snapshot) -> str:
    text = f"{snap.kind} #{job_id}  {bar(snap.fraction)}  {_where(snap)}  {_timing(snap)}"
    if snap.status is not Status.RUNNING:
        text += f"  [{snap.status.value}]"

    return text

