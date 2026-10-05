"""One monotonic budget inherited by nested browser operations.

This is execution-local accounting, not persisted authority. Nested helpers may
shorten a budget, never restart or extend it. Cancellation stays cancellation.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar


_DEADLINE: ContextVar[float | None] = ContextVar("browser_operation_deadline", default=None)
# The outermost operation's timeout and how much queue time it may still be
# credited. Only browser_deadline creates them; nested deadlines leave them be.
_TIMEOUT: ContextVar["asyncio.Timeout | None"] = ContextVar("browser_operation_timeout", default=None)
_QUEUE_ALLOWANCE: ContextVar["list[float] | None"] = ContextVar("browser_queue_allowance", default=None)

#: Longest queue wait (behind other tabs' actions on the same site or owner)
#: credited back to one operation. The operation's own budget is for its work:
#: on 2026-10-04 three 3-step sequences on Workday tabs each waited 18.7 s for
#: the same-site lock and all three failed "deadline exhausted" at 60 s.
QUEUE_CREDIT_CAP_SECONDS = 45.0


def credit_queue_wait(waited_seconds: float) -> float:
    """Give the current operation back time it spent waiting in a queue.

    The one sanctioned extension: queue time is not the operation's work, and
    counting it reported other tabs' activity as this action's failure. Capped
    per operation by QUEUE_CREDIT_CAP_SECONDS. Returns the seconds credited.
    """
    timeout = _TIMEOUT.get()
    allowance = _QUEUE_ALLOWANCE.get()
    deadline = _DEADLINE.get()
    if timeout is None or allowance is None or deadline is None or waited_seconds <= 0:
        return 0.0
    credit = min(float(waited_seconds), allowance[0])
    if credit <= 0:
        return 0.0
    allowance[0] -= credit
    try:
        when = timeout.when()
        if when is None:
            return 0.0
        timeout.reschedule(when + credit)
    except RuntimeError:
        return 0.0  # already expired or exited; nothing to extend
    _DEADLINE.set(deadline + credit)
    return credit


def remaining_seconds(default: float | None = None) -> float | None:
    """How long the current browser operation has left, if it is bounded.

    Waiting helpers use this to stay inside the budget they were given: a
    settle pause that outlives the deadline turns a precise failure into a
    bare timeout.
    """
    deadline = _DEADLINE.get()
    if deadline is None:
        return default
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return default
    return max(0.0, deadline - loop.time())


@asynccontextmanager
async def browser_deadline(seconds: float):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, seconds)
    inherited = _DEADLINE.get()
    if inherited is not None:
        deadline = min(deadline, inherited)
    token = _DEADLINE.set(deadline)
    timeout_token = allowance_token = None
    try:
        # Stay in the same task: private verification ContextVars must remain
        # visible to the manager which creates the final receipt.
        async with asyncio.timeout_at(deadline) as timeout:
            if inherited is None:
                timeout_token = _TIMEOUT.set(timeout)
                allowance_token = _QUEUE_ALLOWANCE.set([QUEUE_CREDIT_CAP_SECONDS])
            if loop.time() >= deadline:
                raise TimeoutError("Browser operation deadline exhausted before dispatch")
            yield deadline
    finally:
        if allowance_token is not None:
            _QUEUE_ALLOWANCE.reset(allowance_token)
        if timeout_token is not None:
            _TIMEOUT.reset(timeout_token)
        _DEADLINE.reset(token)
