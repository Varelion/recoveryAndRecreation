"""Progress snapshot -> text line (console) or embed (Discord).

    export #3  ▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱  62%  messages · #general  3m 10s  ETA 1m 55s
"""

from vault.services.progress import Snapshot, Status

BAR_WIDTH = 20
FULL, EMPTY = "▰", "▱"
MAX_COUNTERS = 15
FIELD_LIMIT = 1024  # Discord embed field value limit

COLORS = {
    Status.RUNNING: 0x5865F2,
    Status.DONE: 0x57F287,
    Status.FAILED: 0xED4245,
    Status.CANCELLED: 0x95A5A6,
}


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


def counters(snap: Snapshot) -> str:
    items = list(snap.counters.items())[:MAX_COUNTERS]
    if not items:
        return "—"

    width = max(len(k) for k, _ in items)
    body = "\n".join(f"{k.ljust(width)}  {v:,}" for k, v in items)
    return f"```\n{body[:FIELD_LIMIT - 8]}\n```"


def embed(job_id: int, snap: Snapshot) -> dict:
    fields = [
        {"name": "Phase", "value": _where(snap)[:FIELD_LIMIT], "inline": True},
        {"name": "Time", "value": _timing(snap), "inline": True},
        {"name": "Counters", "value": counters(snap), "inline": False},
    ]
    if snap.error:
        fields.append({"name": "Error", "value": snap.error[:FIELD_LIMIT], "inline": False})

    return {
        "title": f"{snap.kind.title()} · job #{job_id}",
        "description": f"`{bar(snap.fraction)}`",
        "color": COLORS[snap.status],
        "fields": fields,
        "footer": {"text": snap.status.value},
    }
