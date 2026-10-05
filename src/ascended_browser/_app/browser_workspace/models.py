from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

WorkspaceStatus = Literal["sleeping", "waking", "active", "draining", "needs_recovery", "error"]
TabDisposition = Literal["discard", "deliverable", "handoff"]
TabRestorability = Literal["restorable_get", "non_restorable"]


@dataclass
class TabLease:
    actor: Literal["parent", "worker", "user"]
    actor_id: str
    mode: Literal["read", "write"] = "write"
    generation: int = 1
    acquired_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    lease_id: str = field(default_factory=lambda: "lease_" + uuid.uuid4().hex)


@dataclass
class TabHold:
    """Why a human is being asked to step in on this tab.

    Written when `gate_browser_action` stops an action, so the takeover view can
    say *what* it stopped instead of leaving the user to find it in the
    transcript. Purely explanatory: the refusal itself is the tool result.
    """
    action_class: str = ""
    target: str = ""
    domain: str = ""
    kind: str = ""
    reason: str = ""
    raised_at: float = field(default_factory=time.time)


@dataclass
class TabRecord:
    tab_id: str = field(default_factory=lambda: "tab_" + uuid.uuid4().hex)
    url: str = "about:blank"
    title: str = ""
    disposition: TabDisposition = "handoff"
    restorability: TabRestorability = "restorable_get"
    owner_kind: str = "parent"
    owner_id: str = "parent"
    dirty: bool = False
    uncertain: bool = False
    last_method: str = "GET"
    last_observation_id: str = ""
    last_screenshot: str = ""
    lease: TabLease | None = None
    hold: TabHold | None = None
    # Durable return address for a human takeover. A dynamic attribute here
    # used to disappear on restart and strand worker-owned tabs as parent tabs.
    takeover_resume_actor: str = ""
    takeover_resume_actor_id: str = ""
    # Tabs opened while one viewer controls the browser share one takeover.
    # The group is durable so a restart or popup switch cannot strand a child
    # tab under a user lease with no route back to its agent.
    takeover_group_id: str = ""
    #: What the user says they did while holding the tab, handed to the agent
    #: once on its next look at this tab. See BrowserWorkspaceManager.take_note.
    user_note: str = ""
    # Opaque client operation identity for a user-requested fresh tab. It is
    # durable solely to make a lost HTTP response safe to retry; it conveys no
    # lease, owner, or live-view authority.
    open_operation_id: str = ""
    # ``updated_at`` also changes for lifecycle bookkeeping.  Sleeping policy
    # needs the narrower answer to "when did a person or agent last use this
    # page?" so an old page is not discarded merely because another record was
    # saved, nor kept forever by unrelated bookkeeping.
    last_active_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Monotonic count of state-changing human native inputs accepted on this
    # tab (keys, text, clicks, wheel; pointer motion excluded). Agent actions
    # capture it at observation time and re-resolve when it moved, so a person
    # editing or scrolling between observe and act can never be overwritten by
    # a stale snapshot. See authorize_live_input and the act preconditions.
    human_input_revision: int = 0
    # Epoch timestamp of the last agent-driven tool effect on this tab
    # (observe/act/navigate via the workspace manager). The interface renders
    # an agent-focus marker on the tab strip for a bounded grace after it, so
    # "which tab is the agent using" is readable at a glance without colors,
    # and the mark survives a page reload through the durable record.
    last_agent_act_at: float = 0.0
    # Scroll position captured when the page was put to sleep, re-applied
    # once the same URL reloads on wake so a slept page reopens where it was.
    sleep_scroll_x: int = 0
    sleep_scroll_y: int = 0
    # When unsaved form input was discarded because the tab went unused past
    # the draft lifetime. Non-zero until the tab wakes and the loss is
    # reported to the agent.
    draft_discarded_at: float = 0.0
    # Highest lease generation this tab has carried. Sleep, restart and close
    # drop a parent lease to ambient ownership; the next lease must still
    # fence above every epoch a viewer has seen. A revived lease starting
    # again at 1 made the live view reject its own mint as an older snapshot
    # ("Live view state could not be confirmed").
    lease_epoch_floor: int = 0

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "lease" and value is not None:
            floor = int(getattr(self, "lease_epoch_floor", 0) or 0)
            current = getattr(self, "lease", None)
            replacing = current is None or current.lease_id != value.lease_id
            if replacing and int(value.generation or 0) <= floor:
                value.generation = floor + 1
            object.__setattr__(self, "lease_epoch_floor", max(floor, int(value.generation or 0)))
        elif name == "lease_epoch_floor":
            lease = getattr(self, "lease", None)
            value = max(int(value or 0), int(getattr(lease, "generation", 0) or 0))
        object.__setattr__(self, name, value)

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("last_screenshot", None)
        # Internal lifecycle identity, never a UI authority token.
        value.pop("takeover_group_id", None)
        return value


@dataclass
class Attention:
    audience: Literal["parent", "user"]
    reason: str
    worker_id: str = ""
    # Structured return address for browser handoffs.  Older checkpoints omit
    # these fields and deserialize through the defaults below.
    kind: str = ""
    tab_id: str = ""
    attention_id: str = field(default_factory=lambda: "attn_" + uuid.uuid4().hex)
    revision: int = 1
    raised_at: float = field(default_factory=time.time)
    delivered_at: float | None = None
    expires_at: float | None = None
    resolved_at: float | None = None
    resolution: str = ""


@dataclass
class WorkspaceRecord:
    workspace_id: str
    owner: str
    session_id: str
    status: WorkspaceStatus = "sleeping"
    checkpoint_version: int = 0
    auth_base_version: int = 0
    tabs: dict[str, TabRecord] = field(default_factory=dict)
    workers: dict[str, dict[str, Any]] = field(default_factory=dict)
    attentions: dict[str, Attention] = field(default_factory=dict)
    active_group: dict[str, Any] | None = None
    resumable_count: int = 0
    recovery_warning: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_human_activity_at: float = field(default_factory=time.time)
    last_runtime_activity_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "status": self.status,
            "checkpoint_version": self.checkpoint_version,
            "tabs": [tab.public() for tab in self.tabs.values()],
            "workers": list(self.workers.values()),
            "attention": [asdict(item) for item in self.attentions.values() if not item.resolved_at],
            "resumable_count": self.resumable_count,
            "recovery_warning": self.recovery_warning,
            "updated_at": self.updated_at,
        }
