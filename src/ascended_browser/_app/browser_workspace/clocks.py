from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ParkingClocks:
    """Pause compute and lease clocks together while leaving attention TTL wall-clock based."""

    started_at: float
    last_progress_at: float
    paused_at: float | None = None

    def pause(self, now: float | None = None) -> None:
        if self.paused_at is None:
            self.paused_at = float(now if now is not None else time.time())

    def resume(self, now: float | None = None) -> float:
        if self.paused_at is None:
            return 0.0
        resumed = float(now if now is not None else time.time())
        parked = max(0.0, resumed - self.paused_at)
        self.started_at += parked
        self.last_progress_at += parked
        self.paused_at = None
        return parked
