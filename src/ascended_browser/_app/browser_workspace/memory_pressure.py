"""Memory-pressure signal for the browser's tab-sleep policy.

The browser is the process tree that grows with use (about 290 MB per open
tab), so it is where memory is given back when the machine runs short. This
module only answers "is memory short right now?"; the workspace manager owns
what to sleep.

Two views are combined and the tighter one wins:

* the host (``/proc/meminfo`` MemAvailable against MemTotal), and
* the tightest cgroup v2 limit above this process (``memory.max`` or
  ``memory.high``), which is what a Docker container or a unit with
  ``MemoryMax=`` is actually held to. Reclaimable page cache
  (``inactive_file``) counts as available there, as it does in MemAvailable.

Linux pressure-stall information (PSI) is also read: sustained stalls mean
the system is already thrashing even when the byte counts look acceptable.

Entering and leaving pressure use different thresholds so the policy does not
flap around one value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Enter pressure below 10% available; leave only above 15%.
LOW_AVAILABLE_RATIO = 0.10
RECOVERED_AVAILABLE_RATIO = 0.15
# PSI "some" avg10: share of the last 10 s in which a task stalled on memory.
HIGH_STALL_PERCENT = 25.0
RECOVERED_STALL_PERCENT = 10.0

_CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class MemoryPressure:
    available_bytes: int
    limit_bytes: int
    stall_percent: float
    source: str  # "host" or the cgroup path whose limit is tightest

    @property
    def available_ratio(self) -> float:
        return self.available_bytes / self.limit_bytes if self.limit_bytes else 1.0

    def is_low(self, *, already_low: bool) -> bool:
        if already_low:
            return (
                self.available_ratio < RECOVERED_AVAILABLE_RATIO
                or self.stall_percent >= RECOVERED_STALL_PERCENT
            )
        return (
            self.available_ratio < LOW_AVAILABLE_RATIO
            or self.stall_percent >= HIGH_STALL_PERCENT
        )

    def journal(self) -> dict:
        return {
            "available_mb": self.available_bytes // (1024 * 1024),
            "limit_mb": self.limit_bytes // (1024 * 1024),
            "stall_percent": round(self.stall_percent, 2),
            "pressure_source": self.source,
        }


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _psi_some_avg10(path: Path) -> float:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("some "):
                for field in line.split():
                    if field.startswith("avg10="):
                        return float(field[6:])
    except (OSError, ValueError):
        pass
    return 0.0


def _host() -> MemoryPressure | None:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    values[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError):
        return None
    if "MemTotal" not in values or "MemAvailable" not in values:
        return None
    return MemoryPressure(
        available_bytes=values["MemAvailable"],
        limit_bytes=values["MemTotal"],
        stall_percent=_psi_some_avg10(Path("/proc/pressure/memory")),
        source="host",
    )


def _cgroup_limited() -> MemoryPressure | None:
    """Tightest cgroup v2 memory limit above this process, if any is set."""
    try:
        with open("/proc/self/cgroup", encoding="ascii") as fh:
            rel = next(
                (line.split(":", 2)[2].strip() for line in fh if line.startswith("0::")),
                None,
            )
    except OSError:
        return None
    if rel is None:
        return None
    tightest: MemoryPressure | None = None
    path = _CGROUP_ROOT / rel.lstrip("/")
    while True:
        limits = [
            value for value in (
                _read_int(path / "memory.max"), _read_int(path / "memory.high"),
            ) if value
        ]
        current = _read_int(path / "memory.current")
        if limits and current is not None:
            reclaimable = 0
            try:
                for line in (path / "memory.stat").read_text().splitlines():
                    if line.startswith("inactive_file "):
                        reclaimable = int(line.split()[1])
                        break
            except (OSError, ValueError):
                pass
            limit = min(limits)
            candidate = MemoryPressure(
                available_bytes=max(0, limit - max(0, current - reclaimable)),
                limit_bytes=limit,
                stall_percent=_psi_some_avg10(path / "memory.pressure"),
                source=str(path),
            )
            if tightest is None or candidate.available_ratio < tightest.available_ratio:
                tightest = candidate
        if path == _CGROUP_ROOT or path.parent == path:
            break
        path = path.parent
    return tightest


def read_memory_pressure() -> MemoryPressure | None:
    """Current memory headroom, or None where it cannot be read (non-Linux)."""
    if not os.path.isdir("/proc"):
        return None
    host = _host()
    limited = _cgroup_limited()
    if host is None:
        return limited
    if limited is None:
        return host
    tighter = limited if limited.available_ratio < host.available_ratio else host
    return MemoryPressure(
        available_bytes=tighter.available_bytes,
        limit_bytes=tighter.limit_bytes,
        stall_percent=max(host.stall_percent, limited.stall_percent),
        source=tighter.source,
    )
