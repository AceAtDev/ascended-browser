"""Execution-local action accounting; the workspace journal owns persistence.

Operation IDs correlate attempts. They do not promise external exactly-once
execution. Crossing a dispatch boundary means an effect is *possible*, even if
the browser transport never returns an acknowledgement.
"""
from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class BrowserAttempt:
    persist: Callable[[dict], None]
    action: str
    tab_id: str
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stage: str = "validate"
    dispatch_possible: bool = False
    started: float = field(default_factory=time.monotonic)
    stage_started: float = field(default_factory=time.monotonic)
    timings: dict[str, float] = field(default_factory=dict)

    def event(self, event: str, **details) -> None:
        self.persist({"version": 1, "event": event, "at": time.time(),
                      "operation_id": self.operation_id, "action": self.action,
                      "tab_id": self.tab_id, **details})

    def enter(self, stage: str) -> None:
        now = time.monotonic()
        self.timings[self.stage] = self.timings.get(self.stage, 0) + now - self.stage_started
        self.stage, self.stage_started = stage, now

    def before_dispatch(self) -> None:
        # Persist the possible effect *before* allowing the transport call.
        self.event("browser_action_dispatch", dispatch_status="possible")
        self.dispatch_possible = True
        self.enter("dispatch")

    def withdraw_dispatch(self, reason: str) -> None:
        """Record that the boundary was crossed but no input event was sent.

        Only for a failure that is proof of non-dispatch — a Playwright trial
        click performs the actionability checks and never sends input. It keeps
        a reachable-but-refused target from being reported as an uncertain
        effect that the caller must re-observe before retrying.
        """
        self.event("browser_action_not_dispatched", reason=reason[:200])
        self.dispatch_possible = False

    def receipt(self, evidence: dict | None = None, *, failed: bool = False) -> dict:
        evidence = dict(evidence or {})
        effect = "not_dispatched" if not self.dispatch_possible else "uncertain"
        if self.dispatch_possible and evidence.get("verified") is True:
            effect = "verified"
        elif self.dispatch_possible and evidence.get("failure_reason") == "state_mismatch":
            effect = "contradicted"
        timings = dict(self.timings)
        timings[self.stage] = timings.get(self.stage, 0) + time.monotonic() - self.stage_started
        return {**evidence, "version": 1, "operation_id": self.operation_id,
                "action": self.action, "effect_state": effect,
                "dispatch_status": "possible" if self.dispatch_possible else "not_dispatched",
                "retry_safe": not self.dispatch_possible,
                "failed_stage": self.stage if failed else None,
                "stage_timings_ms": {key: round(value * 1000, 3) for key, value in timings.items()},
                "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3)}


CURRENT_ATTEMPT: ContextVar[BrowserAttempt | None] = ContextVar("browser_attempt", default=None)


def action_stage(stage: str) -> None:
    attempt = CURRENT_ATTEMPT.get()
    if attempt is not None:
        attempt.enter(stage)


def before_dispatch() -> None:
    attempt = CURRENT_ATTEMPT.get()
    if attempt is not None:
        attempt.before_dispatch()


def dispatch_withdrawn(reason: str) -> None:
    attempt = CURRENT_ATTEMPT.get()
    if attempt is not None:
        attempt.withdraw_dispatch(reason)
