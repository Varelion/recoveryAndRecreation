"""UI-agnostic progress of a long action.

Work is split into weighted phases; each phase reports done/total units.
Overall fraction = finished phase weights + current weight x phase fraction.

    export:  structure 5 │ members 10 │ audit 5 │ threads 5 │ messages 70 │ media 5
             ██████████████████████████░░░░░░░░░░░░  ← 0.62, ETA from elapsed / fraction

Services write; any UI (console, Discord message) polls `snapshot()`.
"""

import time
from collections import Counter
from dataclasses import dataclass
from enum import Enum


class Status(Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Snapshot:
    kind: str
    status: Status
    phase: str
    detail: str
    fraction: float | None  # None = open-ended (e.g. watch)
    elapsed: float
    eta: float | None
    counters: dict[str, int]
    error: str | None


class Progress:
    def __init__(self, kind: str, phases: dict[str, float] | None = None) -> None:
        self.kind = kind
        self.counters: Counter = Counter()
        self.status = Status.RUNNING
        self.error: str | None = None
        self._weights = phases or {}
        self._scale = sum(self._weights.values()) or 1
        self._finished_weight = 0.0
        self._phase = "starting"
        self._detail = ""
        self._done = 0
        self._total = 0
        self._started = time.monotonic()
        self._ended: float | None = None

    # ------------------------------------------------------------ writers

    def phase(self, name: str, total: int = 0) -> None:
        """Close the current phase and open `name` with `total` units (0 = unknown)."""
        self._finished_weight += self._weights.get(self._phase, 0)
        self._phase = name
        self._detail = ""
        self._done = 0
        self._total = total

    def plan(self, total: int) -> None:
        self._total = total

    def advance(self, units: int = 1) -> None:
        self._done += units

    def detail(self, text: str) -> None:
        self._detail = text

    def end(self, status: Status, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self._ended = time.monotonic()
        if status is Status.DONE:
            self._finished_weight = self._scale
            self._phase = "finished"
            self._detail = ""

    # ------------------------------------------------------------ readers

    def fraction(self) -> float | None:
        if not self._weights:
            return None

        inner = min(self._done / self._total, 1.0) if self._total else 0.0
        current = self._weights.get(self._phase, 0) * inner
        return min((self._finished_weight + current) / self._scale, 1.0)

    def snapshot(self) -> Snapshot:
        elapsed = (self._ended or time.monotonic()) - self._started
        fraction = self.fraction()
        eta = None
        if self.status is Status.RUNNING and fraction:
            eta = elapsed / fraction - elapsed

        return Snapshot(
            kind=self.kind, status=self.status, phase=self._phase, detail=self._detail,
            fraction=fraction, elapsed=elapsed, eta=eta,
            counters={k: v for k, v in self.counters.items() if v}, error=self.error,
        )
