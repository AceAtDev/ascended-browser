"""Browser workspace manager facade with semantic-control reliability patches.

The historical manager implementation lives in :mod:`manager_core`. This keeps
the public import surface stable while layering verified postconditions,
semantic stale-node recovery, transaction-like form batches, and best-effort
verified replay over the proven exact-ref fast path.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import time
from typing import Any
from urllib.parse import urlparse

from ascended_browser._app.browser_workspace import manager_core as _core
from ascended_browser._app.browser_action_receipts import build_action_receipt, enforce_receipt
from ascended_browser._app.browser_click_helpers import ref_identity, resolve_live_target
from ascended_browser._app.browser_live_verification import (
    refresh_fill_evidence,
    _describe as _describe_control,
    _candidate as _selection_candidate,
    _read_fill_value as _read_control_value,
)
from ascended_browser._app.browser_sensitive_state import is_password_element
from ascended_browser._app.browser_form_runtime import field_summary, rebase_pending_field, should_stop_form_batch
from ascended_browser._app.browser_observation_delta import delta_baseline, newly_appeared_refs, observation_delta
from ascended_browser._app.browser_flows import (
    FlowJournal, FlowStore, compile_flow, describe_step, flow_summary, is_menu_item,
    journal_entry, opens_menu, page_route, plan_steps, recordable, same_target,
)
from ascended_browser._app.browser_semantic_cache import SemanticReplayCache
from ascended_browser._app.browser_semantic_target import (
    SemanticTarget,
    hydrate_action_from_snapshot,
    recover_action_from_snapshot,
    semantic_target_for_action,
)

for _name, _value in vars(_core).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

_BaseBrowserWorkspaceManager = _core.BrowserWorkspaceManager
_ACTION_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "browser_workspace_action_context", default=None,
)
# Set while a saved flow replays, so its steps journal as replayed.
_FLOW_REPLAY: ContextVar[str | None] = ContextVar("browser_flow_replay", default=None)
# Re-observe waits for a replayed step whose target is still rendering.
_FLOW_TARGET_WAITS = (0.0, 0.3, 0.6, 1.2, 2.4)
# The sequence executor's own step limit.
_FLOW_CHUNK = 12


def _runnable(step: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in step.items() if key not in {"_flow_step", "_page", "_unnamed"}}


_FLOW_FIELD_KINDS = frozenset({"fill", "select", "check", "date"})


def _flow_units(steps: list[dict[str, Any]], begin: int) -> list[tuple[int, int]]:
    """[start, end) step ranges: a run of fields on one page, or one step."""
    units: list[tuple[int, int]] = []
    index = begin
    while index < len(steps):
        end = index + 1
        if steps[index].get("kind") in _FLOW_FIELD_KINDS and not steps[index].get("_unnamed"):
            while (end < len(steps) and steps[end].get("kind") in _FLOW_FIELD_KINDS
                   and not steps[end].get("_unnamed")
                   and steps[end].get("_page") == steps[index].get("_page")):
                end += 1
        units.append((index, end))
        index = end
    return units


def _unnamed_unit(steps: list[dict[str, Any]], unit: tuple[int, int]) -> int | None:
    """The first step in a unit whose target has nothing to find it by."""
    return next((index for index in range(*unit) if steps[index].get("_unnamed")), None)


def _unit_action(steps: list[dict[str, Any]], unit: tuple[int, int]) -> dict[str, Any]:
    start, end = unit
    if end - start == 1:
        return _runnable(steps[start])
    return {"kind": "fill_form", "fields": [_runnable(step) for step in steps[start:end]]}


def _first_result(result: dict[str, Any]) -> dict[str, Any]:
    """The step detail of a one-step sequence (or the result itself)."""
    details = result.get("results") if isinstance(result, dict) else None
    if isinstance(details, list) and details and isinstance(details[0], dict):
        return details[0]
    return result if isinstance(result, dict) else {}


def _target_missing(detail: dict[str, Any]) -> bool:
    """A step that stopped before dispatch because its element was absent.

    Ambiguity is not absence: waiting cannot make two matches one.
    """
    error = str(detail.get("error") or "")
    if detail.get("status") == "not_dispatched" and "target was missing or ambiguous" in error:
        return str((detail.get("resolution") or {}).get("reason") or "") != "ambiguous"
    # Resolved from the page as it was, then gone before the effect (a menu
    # that closed): nothing was dispatched, so looking again is safe.
    receipt = detail.get("receipt") if isinstance(detail.get("receipt"), dict) else {}
    return (
        receipt.get("dispatch_status") == "not_dispatched"
        and any(word in error.lower() for word in ("removed", "replaced", "stale", "observe the tab again"))
    )
_SELECT_EVIDENCE: ContextVar[dict[str, Any] | None] = ContextVar(
    "browser_workspace_select_evidence", default=None,
)


def _field_label_from_element(element: dict[str, Any]) -> str:
    for key in ("label", "aria_label", "placeholder", "text"):
        value = str(element.get(key) or "").strip()
        if value:
            return value
    return ""


def _hydrate_field_labels(fields: list[dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Capture each field's semantic identity before the first form mutation."""
    hydrated: list[dict[str, Any]] = []
    by_ref = {
        str(element.get("ref") or ""): element
        for element in snapshot.get("elements") or []
        if isinstance(element, dict) and element.get("ref")
    }
    for raw in fields:
        entry = hydrate_action_from_snapshot(dict(raw), snapshot)
        if not str(entry.get("label") or "").strip():
            element = by_ref.get(str(entry.get("ref") or ""))
            if element:
                label = _field_label_from_element(element)
                if label:
                    entry["label"] = label
        hydrated.append(entry)
    return hydrated


def _conflicting_form_fields(
    fields: list[dict[str, Any]], snapshot: dict[str, Any],
) -> tuple[int, int] | None:
    """Find two requested edits to one observed control before either dispatches.

    Refs normally encode a node token, but two refs from different observations
    can still name the same live node. The current scoped registry supplies its
    full node identity; display labels are never treated as target identity.
    """
    by_ref = {
        str(element.get("ref") or ""): element
        for element in snapshot.get("elements") or []
        if isinstance(element, dict) and element.get("ref")
    }
    seen: dict[tuple[Any, ...], int] = {}
    for index, field in enumerate(fields):
        ref = str(field.get("ref") or "")
        if not ref:
            continue
        element = by_ref.get(ref) or {}
        token = str(element.get("node_token") or "")
        key: tuple[Any, ...] = (
            "node", element.get("frame_index"), element.get("frame_url"), token,
        ) if token else ("ref", ref)
        previous = seen.get(key)
        if previous is not None:
            return previous, index
        seen[key] = index
    return None


def _single_field_action(entry: dict[str, Any], observation_id: str = "") -> dict[str, Any]:
    kind = str(entry.get("kind") or entry.get("type") or "fill").strip().lower()
    if kind in {"choose", "autocomplete"}:
        kind = "select"
    elif kind in {"checkbox", "radio"}:
        kind = "check"
    elif kind not in {"fill", "select", "check", "date", "upload"}:
        kind = "fill"
    action = {**entry, "kind": kind}
    if observation_id and not action.get("observation_id"):
        action["observation_id"] = observation_id
    return action


def _unattempted_field(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable request for one field not yet dispatched."""
    kind = str(entry.get("kind") or entry.get("type") or "fill")[:24]
    summary: dict[str, Any] = {
        "ref": str(entry.get("ref") or ""),
        "label": str(entry.get("label") or "")[:120],
        "kind": kind,
        "status": "unattempted",
    }
    if str(entry.get("id") or "").strip():
        summary["id"] = str(entry["id"]).strip()[:120]
    if isinstance(entry.get("depends_on"), list):
        summary["depends_on"] = [
            str(value).strip()[:120] for value in entry["depends_on"]
            if str(value or "").strip()
        ]
    sensitive = bool(entry.get("sensitive")) or str(
        entry.get("input_type") or entry.get("control_type") or ""
    ).casefold() == "password"
    if kind == "check":
        summary["requested"] = bool(entry.get("checked", entry.get("value", True)))
    elif kind == "select" and isinstance(entry.get("options"), list):
        summary["requested"] = [str(value) for value in entry["options"]]
    else:
        requested = entry.get("option", entry.get("value"))
        if requested is not None:
            summary["requested"] = "[masked]" if sensitive else requested
            if sensitive:
                summary["requested_length"] = len(str(requested))
    return summary


def _is_stale_error(exc: BaseException) -> bool:
    from ascended_browser._app.browser_click_helpers import StaleRefError

    if isinstance(exc, StaleRefError):
        return True
    text = str(exc).casefold()
    return "stale ref" in text or "observed element was replaced" in text or "observed element was removed" in text


def _verified_result(result: dict[str, Any]) -> bool:
    if not bool(result.get("success")):
        return False
    receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else None
    return receipt is None or receipt.get("verified") is not False


_SEQUENCE_TARGET_KINDS = frozenset({"click", "fill", "select", "check", "date", "upload"})


def _unattempted_step(step: dict[str, Any], index: int) -> dict[str, Any]:
    """Describe an undispatched sequence step without copying field values."""
    kind = str(step.get("kind") or step.get("type") or "")[:24]
    item: dict[str, Any] = {"index": index, "kind": kind, "status": "unattempted"}
    label = str(step.get("label") or "").strip()
    if label:
        item["label"] = label[:120]
    fields = [field for field in step.get("fields") or [] if isinstance(field, dict)]
    if fields:
        item["fields"] = [
            {"label": str(field.get("label") or "")[:120],
             "kind": str(field.get("kind") or field.get("type") or "fill")[:24]}
            for field in fields[:20]
        ]
    return item


class BrowserWorkspaceManager(_BaseBrowserWorkspaceManager):
    """Manager with verified actions and fail-safe semantic stale-node recovery."""

    def _replay_cache(self) -> SemanticReplayCache:
        cache = getattr(self, "_browser_semantic_replay_cache", None)
        if cache is None:
            cache = SemanticReplayCache()
            self._browser_semantic_replay_cache = cache
        return cache

    def _observation_history(self) -> dict[tuple[str, str], dict[str, Any]]:
        history = getattr(self, "_browser_semantic_observation_history", None)
        if history is None:
            history = {}
            self._browser_semantic_observation_history = history
        return history

    def _project_observation(self, record, tab, snapshot: dict, *, text: str = "", **extra: Any) -> dict:
        key = (record.workspace_id, tab.tab_id)
        scoped = {**snapshot, "workspace_id": key[0], "tab_id": key[1]}
        if extra.get("view"):
            # A narrowed view (query/within/cursor) shows a subset on purpose.
            # It must not become the change baseline, or the next full
            # observation would report the rest of the page as newly appeared.
            return super()._project_observation(record, tab, scoped, text=text, **extra)
        history = self._observation_history()
        previous = history.get(key)
        fresh = newly_appeared_refs(previous, scoped)
        if fresh:
            scoped["_new_refs"] = sorted(fresh)
        delta = observation_delta(previous, scoped)
        history[key] = delta_baseline(scoped)
        output = super()._project_observation(record, tab, scoped, text=text, **extra)
        if previous is not None:
            output["delta"] = delta
        return output

    def _semantic_snapshot(self, owner: str, session_id: str, tab_id: str, snapshot: dict) -> dict:
        """Recover from full current identities owned by the scoped ref registry."""
        from ascended_browser._app.browser_click_helpers import current_ref_candidates

        candidates = current_ref_candidates(
            workspace_id=self.workspace_id(owner, session_id), tab_id=tab_id,
            observation_id=str(snapshot.get("observation_id") or "") or None,
        )
        if not candidates:
            if snapshot.get("workspace_id") or snapshot.get("tab_id"):
                # A typed current snapshot whose generation is no longer in
                # the registry cannot be safely recovered from display data.
                return {**snapshot, "elements": []}
            return snapshot  # compatibility for legacy collectors without registry identities
        return {**snapshot, "elements": candidates}

    def _semantic_target(self, action: dict[str, Any], *, owner: str, session_id: str, tab_id: str) -> SemanticTarget:
        identity: dict[str, Any] | None = None
        ref = str(action.get("ref") or "")
        if ref:
            try:
                from ascended_browser._app.browser_click_helpers import ref_identity

                identity = ref_identity(
                    ref,
                    workspace_id=self.workspace_id(owner, session_id),
                    tab_id=tab_id,
                    observation_id=action.get("observation_id"),
                )
            except Exception:
                identity = None
        if identity is None and ref:
            # A framework may replace the node after we returned a settled
            # page but before the next model round dispatches. The scoped ref
            # registry correctly rejects that stale generation; retain only
            # its semantic identity from the manager's last observation so the
            # executor can re-observe and recover the unique current target.
            history = self._observation_history().get(
                (self.workspace_id(owner, session_id), tab_id), {}
            )
            matches = [
                element for element in history.get("elements") or []
                if isinstance(element, dict) and str(element.get("ref") or "") == ref
            ]
            if len(matches) == 1:
                identity = matches[0]
        return semantic_target_for_action(action, identity=identity)

    async def _current_url(self, owner: str, session_id: str, tab_id: str) -> str:
        try:
            record = await self.ensure_awake(owner, session_id)
            tab, page = await self._get_tab(record, tab_id)
            return str(getattr(page, "url", "") or tab.url)
        except Exception:
            return ""

    async def _reconcile_uncertain_target(
        self, owner: str, session_id: str, tab_id: str, action: dict[str, Any],
    ) -> dict[str, Any]:
        """Read the target back after a possibly-dispatched action failed.

        The deadline that produced the failure is entered inside ``act`` and has
        already unwound here, so this runs on its own small budget. It only
        reads: it never re-dispatches, because the point is to find out whether
        the effect already happened.

        Failure to reconcile is itself an answer ("still unknown"), so every
        error becomes a verdict rather than replacing one exception with another.
        """
        requested = action.get("value")
        if requested is None:
            requested = action.get("option")
        if requested is None and isinstance(action.get("checked"), bool):
            requested = action["checked"]
        ref = str(action.get("ref") or "")
        # Values stay out of the outcome until the control's own type proves
        # they are safe to echo; a password field must never be read back into
        # a model-facing receipt.
        outcome: dict[str, Any] = {"verified": None, "ref": ref}
        if requested is None:
            # upload, multi-select and click carry no single scalar to compare.
            # Saying "no readable target" about them reads as a broken ref and
            # sends the caller hunting for one that was never the problem.
            outcome["summary"] = (
                f"a {str(action.get('kind') or 'this')} action has no single value to read back, "
                "so its effect must be confirmed by observing the tab"
            )
            return outcome
        if not ref:
            outcome["summary"] = (
                "this action carried no element ref, so its target cannot be read back; "
                "observe the tab before retrying"
            )
            return outcome
        try:
            async with asyncio.timeout(3):
                record = await self.ensure_awake(owner, session_id)
                _tab, page = await self._get_tab(record, tab_id)
                root, selector = await resolve_live_target(
                    page, ref, workspace_id=record.workspace_id, tab_id=tab_id,
                )
                control, identity, verify_selector = await _describe_control(_core, root, selector)
                if str(action.get("kind") or "") == "select":
                    # A combobox can contain the exact search text without a
                    # committed selection. Reuse the semantic control's local
                    # commit check; generic input_value readback caused a
                    # false success after a failed option click.
                    from ascended_browser._app.browser_semantic_controls import (
                        SemanticControlError, verify_selection_commit,
                    )

                    try:
                        committed = await verify_selection_commit(
                            root, selector=verify_selector,
                            candidate=_selection_candidate(control, identity),
                            value=str(requested), timeout_ms=1200,
                            close_popup=False,
                        )
                    except SemanticControlError:
                        outcome["summary"] = (
                            "the control contains no independently confirmed selection; "
                            "observe the tab before deciding whether to retry"
                        )
                        return outcome
                    outcome.update({
                        "requested": requested,
                        "observed": committed.get("observed"),
                        "verified": True,
                        "verification": "post_failure_selection_commit",
                        "summary": "the selected option is confirmed from the control; do not repeat it",
                    })
                    return outcome
                observed = await _read_control_value(root, verify_selector, control)
        except Exception:
            outcome["summary"] = (
                "the target could not be read back, so the effect remains unknown; "
                "observe the tab before retrying"
            )
            return outcome
        if observed is None:
            outcome["summary"] = (
                "the target no longer reads back a value; observe the tab before retrying"
            )
            return outcome
        matched = (
            observed is requested if isinstance(requested, bool)
            else str(observed) == str(requested)
        )
        sensitive = is_password_element(control)
        outcome.update({
            "requested": "[masked]" if sensitive else requested,
            "observed": "[masked]" if sensitive else observed,
            "verified": bool(matched),
            "verification": "post_failure_control_readback",
            "summary": (
                "the requested value is present on the control, so the action did land — "
                "do not repeat it"
                if matched else
                "the control does not hold the requested value, so the action did not land — "
                "it is safe to retry"
            ),
        })
        return outcome

    async def _execute_base_once(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        action: dict[str, Any],
        *,
        actor: str,
        actor_id: str,
        parallel_tab: bool,
    ) -> dict:
        action_token = _ACTION_CONTEXT.set(dict(action))
        select_token = _SELECT_EVIDENCE.set(None)
        try:
            return await _BaseBrowserWorkspaceManager.act(
                self, owner, session_id, tab_id, action,
                actor=actor, actor_id=actor_id, parallel_tab=parallel_tab,
            )
        except Exception as exc:
            receipt = getattr(exc, "browser_receipt", None)
            if isinstance(receipt, dict) and receipt.get("dispatch_status") == "possible":
                # Never turn a transport/readback failure after dispatch into
                # stale-target replay. The effect must be reconciled first —
                # but reconciling is this layer's job, not the caller's. A bare
                # "uncertain" forces an observe-and-guess round trip for an
                # answer the page can give now, and an action that actually
                # landed gets retried and applied twice.
                reconciliation = await self._reconcile_uncertain_target(
                    owner, session_id, tab_id, action,
                )
                settled = reconciliation.get("verified") is True
                result = {
                    "success": settled,
                    "receipt": {**receipt, "reconciliation": reconciliation},
                    "verification_failed": not settled,
                    "tab_id": tab_id,
                    **_failure_evidence(exc),
                }
                if not settled:
                    # Reconciliation adds to the diagnosis; it must never
                    # replace it. Dropping the original message left upload and
                    # multi-select failures describing only what the reconciler
                    # could not do, with the real cause gone.
                    cause = str(exc).strip()
                    parts = [f"{type(exc).__name__}: action effect is uncertain"]
                    if cause and cause.casefold() not in parts[0].casefold():
                        parts.append(cause)
                    parts.append(
                        str(reconciliation.get("summary") or "inspect before retrying")
                    )
                    result["error"] = "; ".join(parts)
                return result
            raise
        finally:
            _SELECT_EVIDENCE.reset(select_token)
            _ACTION_CONTEXT.reset(action_token)

    async def _run_base_action(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        action: dict[str, Any],
        *,
        actor: str,
        actor_id: str,
        parallel_tab: bool,
    ) -> dict:
        target = self._semantic_target(action, owner=owner, session_id=session_id, tab_id=tab_id)
        enriched = {**action, "_semantic_target": target.to_dict()}
        url = await self._current_url(owner, session_id, tab_id)
        try:
            result = await self._execute_base_once(
                owner, session_id, tab_id, enriched,
                actor=actor, actor_id=actor_id, parallel_tab=parallel_tab,
            )
            if _verified_result(result):
                self._replay_cache().learn_success(url, target)
            return result
        except Exception as first_error:
            if not _is_stale_error(first_error) or not target.signature():
                raise

            # Historical successes are not authority to mutate a raw selector:
            # IDs can be reused or duplicated after a render. Recover only from
            # a current observation, retaining its scoped identity and revision.
            snapshot = await self.observe(
                owner, session_id, tab_id, actor=actor, actor_id=actor_id,
            )
            recovered, diagnostic = recover_action_from_snapshot(
                enriched, self._semantic_snapshot(owner, session_id, tab_id, snapshot), target=target,
            )
            if not recovered:
                raise first_error
            result = await self._execute_base_once(
                owner, session_id, tab_id, recovered,
                actor=actor, actor_id=actor_id, parallel_tab=parallel_tab,
            )
            result = dict(result)
            result["semantic_recovery"] = {
                **diagnostic,
                "old_ref": str(action.get("ref") or ""),
                "new_ref": str(recovered.get("ref") or ""),
                "cache": self._replay_cache().stats(),
            }
            result["self_healed"] = result.get("self_healed") or (
                "The observed node was replaced; Ascended re-resolved the target "
                "from its semantic identity. Consult the receipt for effect verification."
            )
            if _verified_result(result):
                self._replay_cache().learn_success(url, target)
            return result

    async def _finish_act(
        self, record, tab, page, *,
        tab_id: str, kind: str, domain: str,
        before_auth: str, after_auth: str, detail: str,
        field_results: list[dict], upload_evidence: dict[str, Any] | None,
        self_healed: bool,
        click_evidence: dict | None = None,
        **extra: Any,
    ) -> dict:
        # Forward anything the base layer accepts that this override does not
        # name. Enumerating the base signature here once cost a live session:
        # a new base keyword raised TypeError at dispatch, and the model fell
        # back to hand-written page scripts for the rest of the run.
        result = await super()._finish_act(
            record, tab, page,
            tab_id=tab_id,
            kind=kind,
            domain=domain,
            before_auth=before_auth,
            after_auth=after_auth,
            detail=detail,
            field_results=field_results,
            upload_evidence=upload_evidence,
            self_healed=self_healed,
            click_evidence=click_evidence,
            **extra,
        )
        action = _ACTION_CONTEXT.get()
        if not action:
            return result

        page_snapshot = result.get("page") if isinstance(result.get("page"), dict) else None
        private_readback = await refresh_fill_evidence() if kind in {"fill", "check"} else False
        if kind in {"fill", "check"} and not private_readback and page_snapshot is None:
            page_snapshot = await self._observe_for_result(record, tab, page, lock_held=True)

        receipt = build_action_receipt(
            action,
            result,
            page_snapshot=page_snapshot,
            select_evidence=_SELECT_EVIDENCE.get(),
        )
        return enforce_receipt(result, receipt)

    async def _resolve_sequence_step(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        step: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Resolve label-only planned targets from one current observation.

        A later workflow page cannot supply refs at planning time. The sequence
        therefore accepts the same semantic identity used for stale-ref
        recovery, but dispatch still requires a unique current scoped ref.
        Ambiguity is terminal and no raw selector is manufactured.
        """
        action = _core.normalize_browser_action(step)
        if action.get("kind") == "sequence":
            return None, {"reason": "nested_sequence_not_supported"}
        semantic_snapshot = self._semantic_snapshot(
            owner, session_id, tab_id, snapshot,
        )

        async def resolve_one(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            if str(candidate.get("ref") or "").strip():
                return candidate, None
            target = SemanticTarget.from_action(candidate)
            if not target.signature():
                return None, {"reason": "missing_target_identity"}
            recovered, diagnostic = recover_action_from_snapshot(
                candidate, semantic_snapshot, target=target,
            )
            return recovered, diagnostic

        kind = str(action.get("kind") or "")
        if kind == "fill_form":
            resolved_fields: list[dict[str, Any]] = []
            for field_index, raw in enumerate(action.get("fields") or []):
                if not isinstance(raw, dict):
                    return None, {"reason": "invalid_field", "field_index": field_index}
                field, diagnostic = await resolve_one(_single_field_action(raw))
                if field is None:
                    return None, {
                        "reason": "field_target_not_resolved",
                        "field_index": field_index,
                        "diagnostic": diagnostic,
                    }
                resolved_fields.append(field)
            return {**action, "fields": resolved_fields}, None
        if kind in _SEQUENCE_TARGET_KINDS:
            return await resolve_one(action)
        return action, None

    async def _sequence_expectation_met(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        expectation: dict[str, Any] | None,
        *,
        actor: str,
        actor_id: str,
    ) -> tuple[bool, str, int]:
        """(met, "met" | "absent" | "unreadable", waited_ms) for one expectation."""
        if not isinstance(expectation, dict) or not expectation:
            return True, "met", 0
        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        self.assert_tab_access(
            owner, session_id, tab_id, actor=actor, actor_id=actor_id,
        )
        # The effect an action asks for lands after the action returns: a
        # sign-in redirects and renders, an async control enables itself a
        # second later. Wait for it like an assertion rather than sampling once.
        budget = max(0.0, min(15.0, float(
            get_setting("browser_workspace_expectation_seconds", 5.0) or 5.0
        )))
        effect_barrier = self._runtime_effect_barrier(record.owner)
        async with effect_barrier.shared(), self._action_lock(record.workspace_id, tab_id):
            evidence, state, waited_ms = await self._await_expectation(
                page, expectation, budget_s=budget,
            )
        return bool(evidence), state, waited_ms

    async def _run_guarded_sequence(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        action: dict[str, Any],
        *,
        actor: str,
        actor_id: str,
        parallel_tab: bool,
    ) -> dict[str, Any]:
        """Execute a bounded typed workflow with fresh-state guards per step."""
        steps = [dict(step) for step in action.get("steps") or [] if isinstance(step, dict)]
        if not steps:
            return {"success": False, "error": "sequence requires at least one step",
                    "completed": 0, "remaining": 0, "stopped_early": True}
        if len(steps) > 12:
            return {"success": False, "error": "sequence supports at most 12 steps",
                    "completed": 0, "remaining": len(steps), "stopped_early": True,
                    "unattempted_steps": [_unattempted_step(step, i) for i, step in enumerate(steps)]}

        results: list[dict[str, Any]] = []
        latest_page: dict[str, Any] | None = None
        stop_reason = ""
        for index, raw_step in enumerate(steps):
            started = time.monotonic()
            expect_before = raw_step.get("expect_before")
            before_met, _before_state, _before_ms = await self._sequence_expectation_met(
                owner, session_id, tab_id, expect_before,
                actor=actor, actor_id=actor_id,
            )
            if not before_met:
                stop_reason = f"step {index + 1} precondition was not present"
                results.append({
                    "success": False, "index": index,
                    "kind": str(raw_step.get("kind") or raw_step.get("type") or ""),
                    "status": "not_dispatched", "error": stop_reason,
                    "receipt": {"effect_state": "not_dispatched", "dispatch_status": "not_dispatched",
                                "verified": False, "retry_safe": True},
                    "duration_ms": round((time.monotonic() - started) * 1000),
                })
                break

            snapshot = latest_page or await self.observe(
                owner, session_id, tab_id, actor=actor, actor_id=actor_id,
            )
            resolved, diagnostic = await self._resolve_sequence_step(
                owner, session_id, tab_id, raw_step, snapshot,
            )
            if resolved is None:
                stop_reason = f"step {index + 1} target was missing or ambiguous"
                results.append({
                    "success": False, "index": index,
                    "kind": str(raw_step.get("kind") or raw_step.get("type") or ""),
                    "status": "not_dispatched", "error": stop_reason,
                    "resolution": diagnostic,
                    "receipt": {"effect_state": "not_dispatched", "dispatch_status": "not_dispatched",
                                "verified": False, "retry_safe": True},
                    "duration_ms": round((time.monotonic() - started) * 1000),
                })
                break

            if (
                str(resolved.get("kind") or "") == "click"
                and isinstance(raw_step.get("expect_after"), dict)
                and not resolved.get("expect")
            ):
                # The click primitive owns the event-driven wait for its effect.
                # Reuse the sequence transition guard there, then independently
                # recheck it before resolving the next step.
                resolved["expect"] = dict(raw_step["expect_after"])

            step_result = await self._act_batch_or_single(
                owner, session_id, tab_id, resolved,
                actor=actor, actor_id=actor_id, parallel_tab=parallel_tab, progress={},
            )
            step_result = {**step_result, "index": index,
                           "kind": str(resolved.get("kind") or ""),
                           "duration_ms": round((time.monotonic() - started) * 1000)}
            results.append(step_result)
            if isinstance(step_result.get("page"), dict):
                latest_page = step_result["page"]
            else:
                latest_page = None

            if step_result.get("blocked"):
                stop_reason = str(step_result.get("reason") or "browser blocker appeared")
                break
            if not _verified_result(step_result):
                stop_reason = str(step_result.get("error") or "step effect was not verified")
                break
            after_met, after_state, after_ms = await self._sequence_expectation_met(
                owner, session_id, tab_id, raw_step.get("expect_after"),
                actor=actor, actor_id=actor_id,
            )
            if not after_met:
                if after_state == "unreadable":
                    # The page never answered — mid-navigation, or busy past the
                    # bound. The step's own effect was verified, so stopping the
                    # sequence here would abandon work over a check that did not
                    # run. Say so and carry on.
                    step_result["postcondition_met"] = None
                    step_result["postcondition_note"] = (
                        "The page could not be read to check this step's expect_after; "
                        "the step's own effect was verified. Observe the tab to confirm."
                    )
                else:
                    stop_reason = (
                        f"step {index + 1} postcondition was not present after "
                        f"waiting {after_ms} ms"
                    )
                    step_result["success"] = False
                    step_result["postcondition_met"] = False
                    break

        completed = sum(bool(item.get("success")) for item in results)
        dispatched_any = any(
            str((item.get("receipt") or {}).get("dispatch_status") or "")
            in {"dispatched", "possible"}
            or str((item.get("receipt") or {}).get("effect_state") or "")
            in {"verified", "contradicted", "uncertain"}
            for item in results
        )
        remaining = max(0, len(steps) - len(results))
        success = len(results) == len(steps) and all(bool(item.get("success")) for item in results)
        output: dict[str, Any] = {
            "success": success,
            "results": results,
            "completed": completed,
            "attempted": len(results),
            "remaining": remaining,
            "stopped_early": not success,
            "receipt": {
                "effect_state": "verified" if success else (
                    (results[-1].get("receipt") or {}).get("effect_state", "uncertain") if results else "not_dispatched"
                ),
                "dispatch_status": "dispatched" if dispatched_any else "not_dispatched",
                "verified": success,
                "retry_safe": not dispatched_any,
            },
        }
        if latest_page is not None:
            output["page"] = latest_page
        if not success:
            output["error"] = stop_reason or "sequence stopped before every step verified"
            output["stop_reason"] = output["error"]
            output["partial_failure"] = bool(dispatched_any)
            output["unattempted_steps"] = [
                _unattempted_step(step, index)
                for index, step in enumerate(steps[len(results):], start=len(results))
            ]
        return output

    async def act(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        action: dict,
        *,
        actor: str = "parent",
        actor_id: str = "parent",
        parallel_tab: bool = False,
    ) -> dict:
        from ascended_browser._app.browser_deadline import browser_deadline

        progress: dict[str, Any] = {}
        try:
            async with browser_deadline(_core.action_budget_seconds(action)):
                return await self._act_batch_or_single(
                    owner, session_id, tab_id, action,
                    actor=actor, actor_id=actor_id, parallel_tab=parallel_tab, progress=progress,
                )
        except TimeoutError as exc:
            if progress:
                fields = list(progress["fields"])
                attempted = progress["attempted"]
                if attempted > len(fields):
                    index = len(fields)
                    fields.append({
                        "label": progress["labels"][index] if index < len(progress["labels"]) else "",
                        "status": "failed",
                        "error": "operation deadline exhausted while this field was active",
                        "verified": False,
                        "effect_state": "uncertain",
                        "dispatch_status": "possible",
                        "retry_safe": False,
                    })
                filled = sum(item.get("verified") is True for item in fields)
                return {"success": False, "fields": fields, "filled": filled,
                        "attempted": attempted, "remaining": progress["total"] - attempted,
                        "uncertain": sum(item.get("effect_state") == "uncertain" for item in fields),
                        "stopped_early": True, "partial_failure": bool(filled),
                        "skipped_fields": progress["labels"][attempted:],
                        "unattempted_fields": list(progress.get("unattempted") or [])[attempted:],
                        "error": "Form batch deadline exhausted; partial work is preserved, not rolled back.",
                        "stop_reason": "operation_deadline"}
            receipt = getattr(exc.__cause__, "browser_receipt", None)
            if (isinstance(receipt, dict) and receipt.get("failed_stage") == "queue"
                    and receipt.get("dispatch_status") == "not_dispatched"):
                # It never left the queue: nothing was sent, and the cause was
                # other work, not this action (2026-10-04: three sequences
                # "failed" this way while waiting on the same site's lock).
                return {"success": False,
                        "error": ("This action waited behind other browser actions on the same site "
                                  "and ran out of time before it started; nothing was sent. Retry it "
                                  "once those finish, or act on fewer tabs of this site at a time."),
                        "receipt": receipt,
                        "stop_reason": "queue_deadline"}
            return {"success": False, "error": "Browser operation deadline exhausted; inspect any uncertain effect before retrying.",
                    "receipt": receipt or {"effect_state": "uncertain", "retry_safe": False},
                    "stop_reason": "operation_deadline"}

    async def _act_batch_or_single(
        self, owner: str, session_id: str, tab_id: str, action: dict, *,
        actor: str, actor_id: str, parallel_tab: bool, progress: dict[str, Any],
    ) -> dict:
        normalized = _core.normalize_browser_action(action)
        if str(normalized.get("kind") or "") == "sequence":
            return await self._run_guarded_sequence(
                owner, session_id, tab_id, normalized,
                actor=actor, actor_id=actor_id, parallel_tab=parallel_tab,
            )
        if str(normalized.get("kind") or "") == "date":
            # Date is one semantic form mutation.  Route the convenient
            # top-level shape through the same batch executor/receipt owner as
            # date fields inside fill_form; this keeps deadlines, authority,
            # readback and stale-target recovery identical.
            field = {
                key: value for key, value in normalized.items()
                if key not in {"reasoning", "expect", "fields"}
            }
            normalized = {
                "kind": "fill_form",
                "fields": [field],
                "reasoning": normalized.get("reasoning"),
            }
        if str(normalized.get("kind") or "") != "fill_form":
            flow_pre = self._flow_capture(owner, session_id, tab_id, [normalized])
            result = await self._run_base_action(
                owner, session_id, tab_id, normalized,
                actor=actor, actor_id=actor_id, parallel_tab=parallel_tab,
            )
            self._flow_commit(owner, session_id, tab_id, flow_pre, [result])
            return result

        raw_fields = [entry for entry in normalized.get("fields") or [] if isinstance(entry, dict)]
        if not raw_fields:
            return await self._run_base_action(
                owner, session_id, tab_id, normalized,
                actor=actor, actor_id=actor_id, parallel_tab=parallel_tab,
            )

        # Shared control: which human-input generation the MODEL's last
        # observation saw. This must be read before the auto-observe below
        # refreshes the cache, so a person who edited or scrolled after the
        # model looked can still be detected and yielded to (cua's
        # snapshot-before invariant). A manager without the observation cache
        # (lightweight test double) has unknowable snapshot freshness (-1).
        prior = getattr(self, "_observation_cache", {}).get(
            (self.workspace_id(owner, session_id), tab_id)
        )
        prior_projection = prior[2] if isinstance(prior, tuple) and len(prior) == 3 else None
        model_observed_revision = (
            int(prior_projection.get("human_input_revision") or 0)
            if isinstance(prior_projection, dict) else -1
        )

        snapshot = await self.observe(
            owner, session_id, tab_id, actor=actor, actor_id=actor_id,
        )
        semantic_snapshot = self._semantic_snapshot(owner, session_id, tab_id, snapshot)
        fields = _hydrate_field_labels(raw_fields, semantic_snapshot)
        conflict = _conflicting_form_fields(fields, semantic_snapshot)
        if conflict is not None:
            first, second = conflict
            return {
                "success": False,
                "error": (
                    f"Fields {first + 1} and {second + 1} target the same observed control. "
                    "No field was changed. Observe the questions again and send one "
                    "value per control; use sequence for intentional later edits."
                ),
                "stop_reason": "conflicting_field_targets",
                "fields": [],
                "filled": 0,
                "attempted": 0,
                "remaining": len(fields),
                "unattempted_fields": [_unattempted_field(item) for item in fields],
                "receipt": {
                    "verified": False,
                    "effect_state": "not_dispatched",
                    "dispatch_status": "not_dispatched",
                    "retry_safe": True,
                },
            }
        observation_id = str(snapshot.get("observation_id") or normalized.get("observation_id") or "")
        normalized_fields = [_single_field_action(entry, observation_id) for entry in fields]
        progress.update(
            fields=[], total=len(normalized_fields), attempted=0, remaining=len(normalized_fields),
            labels=[str(item.get("label") or item.get("ref") or "")[:120] for item in normalized_fields],
            unattempted=[_unattempted_field(item) for item in normalized_fields],
        )
        flow_pre = self._flow_capture(owner, session_id, tab_id, normalized_fields)
        # The core workspace manager is the one execution owner. It acquires
        # authority once, executes and verifies all fields under one deadline,
        # persists one action outcome, and returns one final page snapshot.
        result = await self._run_base_action(
            owner, session_id, tab_id,
            {**normalized, "fields": normalized_fields, "observation_id": observation_id,
             "_batch_progress": progress,
             "_model_observed_revision": model_observed_revision,
             "_model_observed_controls": _core._observed_control_states(prior_projection)},
            actor=actor, actor_id=actor_id, parallel_tab=parallel_tab,
        )
        field_results = [item for item in result.get("fields") or [] if isinstance(item, dict)]
        self._flow_commit(owner, session_id, tab_id, flow_pre, [
            {"success": item.get("verified") is True, "receipt": {"verified": item.get("verified")}}
            for item in field_results
        ])
        attempted = len(field_results)
        remaining = max(0, len(normalized_fields) - attempted)
        succeeded = sum(item.get("verified") is True for item in field_results)
        progress.update(fields=field_results, attempted=attempted, remaining=remaining)
        output = {
            **result,
            "success": bool(result.get("success")) and not remaining and not result.get("blocked"),
            "filled": succeeded,
            "attempted": attempted,
            "remaining": remaining,
            "partial_failure": bool(result.get("success") is False and succeeded),
            "stopped_early": bool(remaining),
        }
        if remaining:
            output["skipped_fields"] = progress["labels"][attempted:]
            output["unattempted_fields"] = [
                _unattempted_field(item) for item in normalized_fields[attempted:]
            ]
        if result.get("blocked"):
            output["stop_reason"] = str(result.get("reason") or "browser blocker appeared")
        if result.get("success") is False:
            output.setdefault("stop_reason", str(result.get("error") or "field action failed"))
        return output

    # -- Recorded flows (browser_flow) ------------------------------------
    #
    # Every verified action is journaled per tab with the semantic identity
    # of its target; a saved flow replays those steps through this same
    # sequence executor, with no model call between them.

    def _flow_journal(self) -> FlowJournal:
        journal = getattr(self, "_browser_flow_journal", None)
        if journal is None:
            journal = FlowJournal()
            self._browser_flow_journal = journal
        return journal

    def _flow_store(self) -> FlowStore:
        store = getattr(self, "_browser_flow_store", None)
        if store is None:
            root = getattr(getattr(self, "store", None), "root", None)
            if root:
                store = FlowStore(Path(root) / "_flows")
            else:
                from ascended_browser.runtime.constants import DATA_DIR
                store = FlowStore(Path(DATA_DIR) / "browser_workspaces" / "_flows")
            self._browser_flow_store = store
        return store

    def _flow_capture(
        self, owner: str, session_id: str, tab_id: str, actions: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Before an action runs: its page and each target's identity.

        Refs are only valid until the action navigates, so identity is read
        now. Best-effort: recording never gets in the way of acting.
        """
        try:
            workspace_id = self.workspace_id(owner, session_id)
            if not any(recordable(str(action.get("kind") or ""), action) for action in actions):
                return None
            # The tab record's URL, from memory: this runs inside the action's
            # deadline and must never wake or relaunch the browser.
            record = (getattr(self, "_records", None) or {}).get(workspace_id)
            tab = getattr(record, "tabs", {}).get(tab_id) if record is not None else None
            url = str(getattr(tab, "url", "") or "")
            items = []
            for action in actions:
                kind = str(action.get("kind") or "")
                if not recordable(kind, action):
                    items.append(None)
                    continue
                target = None
                ref = str(action.get("ref") or "").strip()
                if ref:
                    identity = ref_identity(
                        ref, workspace_id=workspace_id, tab_id=tab_id,
                        observation_id=str(action.get("observation_id") or "") or None,
                    ) or {}
                    semantic = SemanticTarget.from_action(action, identity).to_dict()
                    for key in ("aria_label", "nearby", "sensitive", "aria_expanded", "haspopup"):
                        if identity.get(key) not in (None, ""):
                            semantic[key] = identity[key]
                    target = semantic
                items.append({"kind": kind, "action": dict(action), "target": target})
            return {"workspace_id": workspace_id, "url": url, "items": items}
        except Exception:
            log.debug("Flow capture failed", exc_info=True)
            return None

    def _flow_commit(
        self, owner: str, session_id: str, tab_id: str,
        captured: dict[str, Any] | None, results: list[dict[str, Any]],
    ) -> None:
        if not captured:
            return
        source = "replay" if _FLOW_REPLAY.get() else "agent"
        for item, result in zip(captured["items"], results):
            if not item or not isinstance(result, dict) or result.get("blocked"):
                continue
            if not _verified_result(result):
                continue
            entry = journal_entry(
                item["kind"], item["action"], target=item["target"],
                url=captured["url"], source=source,
            )
            if entry:
                self._flow_journal().record(captured["workspace_id"], tab_id, entry)

    def flow_recorded(self, owner: str, session_id: str, tab_id: str) -> dict[str, Any]:
        """The verified steps this tab has journaled, numbered for save."""
        entries = self._flow_journal().entries(self.workspace_id(owner, session_id), tab_id)
        lines = []
        for number, entry in enumerate(entries, start=1):
            if entry.get("sensitive"):
                lines.append(f"{number}. [secret field, not recorded] {entry.get('label') or ''}".rstrip())
                continue
            step = {"kind": entry.get("kind"), **(entry.get("params") or {}), "target": entry.get("target") or {}}
            lines.append(f"{number}. {describe_step(step)}  @ {page_route(entry.get('url') or '')}")
        return {"success": True, "tab_id": tab_id, "recorded": len(entries), "steps": lines}

    def flow_save(
        self, owner: str, session_id: str, tab_id: str, *, name: str = "",
        from_step: Any = None, to_step: Any = None,
    ) -> dict[str, Any]:
        entries = self._flow_journal().entries(self.workspace_id(owner, session_id), tab_id)
        try:
            first = max(1, int(from_step or 1))
            last = int(to_step) if to_step not in (None, "") else len(entries)
        except (TypeError, ValueError):
            return {"success": False, "error": "from_step and to_step are 1-based step numbers from action=recorded."}
        try:
            flow = compile_flow(entries[first - 1:last], name=name)
            saved = self._flow_store().save(owner, flow)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "recorded": len(entries)}
        return {"success": True, "saved": True, **flow_summary(saved, with_steps=True)}

    def flow_list(self, owner: str) -> dict[str, Any]:
        flows = [flow_summary(flow) for flow in self._flow_store().list(owner)]
        return {"success": True, "flows": flows, "count": len(flows)}

    def flow_show(self, owner: str, flow_id: str) -> dict[str, Any]:
        flow = self._flow_store().get(owner, flow_id)
        if not flow:
            return self._flow_missing(owner, flow_id)
        return {"success": True, **flow_summary(flow, with_steps=True)}

    def flow_delete(self, owner: str, flow_id: str) -> dict[str, Any]:
        if not self._flow_store().delete(owner, flow_id):
            return self._flow_missing(owner, flow_id)
        return {"success": True, "deleted": flow_id}

    def _flow_missing(self, owner: str, flow_id: str) -> dict[str, Any]:
        known = [f"{flow.get('flow_id')} ({flow.get('name')})" for flow in self._flow_store().list(owner)[:20]]
        return {"success": False, "error": f"No saved flow {flow_id!r}.",
                "available": known or "none saved yet"}

    async def flow_run(
        self, owner: str, session_id: str, tab_id: str, flow_id: str, *,
        variables: dict[str, Any] | None = None, start_url: str = "", from_step: Any = None,
        actor: str = "parent", actor_id: str = "parent",
    ) -> dict[str, Any]:
        """Replay a saved flow on this tab; stop at the first step that fails."""
        flow = self._flow_store().get(owner, flow_id)
        if not flow:
            return self._flow_missing(owner, flow_id)
        if variables is not None and not isinstance(variables, dict):
            return {"success": False, "error": "variables must be an object of name -> value."}
        try:
            steps = plan_steps(flow, variables)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "flow_id": flow_id}
        try:
            begin = max(0, int(from_step or 1) - 1)
        except (TypeError, ValueError):
            begin = 0
        started = time.monotonic()
        outcomes: list[dict[str, Any]] = []
        last: dict[str, Any] = {}

        site = str(flow.get("site") or "").lower()
        requested = urlparse(str(start_url or "").strip()).hostname or ""
        if start_url and site and requested.lower() != site:
            return {"success": False, "status": "start_failed", "flow_id": flow_id,
                    "error": f"This flow was recorded on {site}; start_url is on {requested or 'no host'}. "
                             "A flow only replays on the site it was recorded on.",
                    "completed": 0, "total": len(steps)}

        # Where the flow starts: the caller's page (the next job, say), else
        # the recorded one — unless the tab is already on the first step's page.
        destination = str(start_url or "").strip()
        if not destination and begin == 0 and flow.get("start_url"):
            record = await self.ensure_awake(owner, session_id)
            _tab, page = await self._get_tab(record, tab_id)
            first_page = str((flow.get("steps") or [{}])[0].get("page") or "")
            if page_route(str(getattr(page, "url", "") or "")) != first_page:
                destination = str(flow["start_url"])
        if destination:
            navigated = await self.act(
                owner, session_id, tab_id, {"kind": "navigate", "url": destination},
                actor=actor, actor_id=actor_id,
            )
            if not _verified_result(navigated):
                return {"success": False, "status": "start_failed", "flow_id": flow_id,
                        "error": f"Could not open the flow's start page {destination}: "
                                 f"{navigated.get('error') or 'navigation was not verified'}",
                        "completed": 0, "total": len(steps)}

        token = _FLOW_REPLAY.set(flow_id)
        status = "completed"
        failure: dict[str, Any] | None = None

        def mark_done(position: int, detail: dict[str, Any]) -> None:
            nonlocal previous, last
            outcomes.append({"step": position + 1, "description": describe_step(steps[position]),
                             "status": "done"})
            previous, last = steps[position], detail

        try:
            previous: dict[str, Any] | None = None
            units = _flow_units(steps, begin)
            position = 0
            while position < len(units):
                # Consecutive units run as one guarded sequence, whose each
                # verified page is the next unit's observation; consecutive
                # fields on one page run as one form batch.
                chunk = units[position:position + _FLOW_CHUNK]
                recorded_host = str(steps[chunk[0][0]].get("_page") or "").split("/", 1)[0]
                if recorded_host:
                    record = await self.ensure_awake(owner, session_id)
                    _tab, live_page = await self._get_tab(record, tab_id)
                    live_host = (urlparse(str(getattr(live_page, "url", "") or "")).hostname or "").lower()
                    if live_host != recorded_host:
                        index = chunk[0][0]
                        outcome = {
                            "step": index + 1, "description": describe_step(steps[index]), "status": "failed",
                            "error": (f"The tab is on {live_host or 'no site'}, but this step was recorded on "
                                      f"{recorded_host}. Replay does not act on another site."),
                        }
                        status, failure = "stopped", outcome
                        outcomes.append(outcome)
                        break
                blocked = next((k for k, unit in enumerate(chunk) if _unnamed_unit(steps, unit) is not None), None)
                if blocked == 0:
                    index = _unnamed_unit(steps, chunk[0])
                    outcome = {
                        "step": index + 1, "description": describe_step(steps[index]), "status": "failed",
                        "error": ("This step's control had no label, name or id when it was recorded, so "
                                  "replay cannot tell it from others of its kind and will not guess. "
                                  "Do this step yourself."),
                    }
                    status, failure = "stopped", outcome
                    outcomes.append(outcome)
                    break
                if blocked is not None:
                    chunk = chunk[:blocked]
                result = await self.act(
                    owner, session_id, tab_id,
                    {"kind": "sequence", "steps": [_unit_action(steps, unit) for unit in chunk]},
                    actor=actor, actor_id=actor_id,
                )
                details = [item for item in result.get("results") or [] if isinstance(item, dict)]
                stopped = False
                for unit, detail in zip(chunk, details):
                    if not _verified_result(detail):
                        stopped = True
                        break
                    for index in range(unit[0], unit[1]):
                        mark_done(index, detail)
                    position += 1
                if not stopped and len(details) == len(chunk):
                    continue
                if position >= len(units):
                    break
                unit = units[position]
                failed = details[len(details) - 1] if stopped and details else {}
                if unit[1] - unit[0] > 1:
                    if _target_missing(failed) or not failed:
                        # A field of the batch is not on the page yet (it
                        # appears after an earlier choice): run them one by one.
                        units[position:position + 1] = [(index, index + 1) for index in range(unit[0], unit[1])]
                        continue
                    # The batch ran; mark what it verified and stop at the
                    # first field it did not.
                    fields = [item for item in failed.get("fields") or [] if isinstance(item, dict)]
                    bad, detail = None, failed
                    for offset in range(unit[1] - unit[0]):
                        field = fields[offset] if offset < len(fields) else {}
                        if field.get("verified") is True:
                            mark_done(unit[0] + offset, failed)
                        elif bad is None:
                            bad, detail = unit[0] + offset, field or failed
                    bad = unit[0] if bad is None else bad
                    step = steps[bad]
                    outcome = {"step": bad + 1, "description": describe_step(step), "status": "failed",
                               "error": str(detail.get("error") or failed.get("error") or "not verified")[:400]}
                    # The form executor may have applied fields after the one
                    # that failed. Resuming right after it would apply them
                    # again, so hand back every unverified field of the batch
                    # and resume after the whole batch.
                    done_steps = {item["step"] for item in outcomes if item["status"] == "done"}
                    hand_back = [index + 1 for index in range(*unit) if index + 1 not in done_steps]
                    outcome["hand_back"] = hand_back
                    outcome["resume_from"] = unit[1] + 1
                    status, failure = "stopped", outcome
                    outcomes.append(outcome)
                    break
                step = steps[unit[0]]
                missing = _target_missing(failed)
                if missing or not failed:
                    failed, missing = await self._flow_run_step(
                        owner, session_id, tab_id, step, actor=actor, actor_id=actor_id,
                    )
                    failed = _first_result(failed)
                if missing and previous is not None and same_target(previous, step):
                    # The original run clicked the same thing twice (a slow
                    # page); the first click already moved on. Upstream's
                    # redundant-retry rule, applied only when the target is gone.
                    outcomes.append({"step": unit[0] + 1, "status": "skipped",
                                     "reason": "repeat of the previous step; its target is gone",
                                     "description": describe_step(step)})
                    position += 1
                    continue
                if missing and previous is not None and opens_menu(previous) and is_menu_item(step):
                    # The menu the previous step opened closed again before
                    # its item could be chosen. Re-open it once.
                    again, _gone = await self._flow_run_step(
                        owner, session_id, tab_id, previous, actor=actor, actor_id=actor_id,
                    )
                    if _verified_result(_first_result(again)):
                        failed, missing = await self._flow_run_step(
                            owner, session_id, tab_id, step, actor=actor, actor_id=actor_id,
                        )
                        failed = _first_result(failed)
                if _verified_result(failed):
                    mark_done(unit[0], failed)
                    position += 1
                    continue
                outcome = {"step": unit[0] + 1, "description": describe_step(step), "status": "failed",
                           "error": str(failed.get("error") or "not verified")[:400]}
                if failed.get("resolution"):
                    outcome["resolution"] = failed["resolution"]
                if failed.get("blocked"):
                    outcome["blocked"] = True
                status, failure = "stopped", outcome
                outcomes.append(outcome)
                break
        finally:
            _FLOW_REPLAY.reset(token)
        outcomes.sort(key=lambda item: item["step"])

        done = sum(1 for item in outcomes if item["status"] == "done")
        skipped = sum(1 for item in outcomes if item["status"] == "skipped")
        stopped_at = failure["step"] if failure else None
        output: dict[str, Any] = {
            "success": failure is None,
            "status": status,
            "flow_id": flow_id,
            "name": flow.get("name"),
            "total": len(steps),
            "completed": done,
            "skipped": skipped,
            "steps": outcomes,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "model_calls": 0,
        }
        if failure:
            output["error"] = f"Step {stopped_at} ({failure['description']}) failed: {failure.get('error')}"
            resume_from = int(failure.get("resume_from") or stopped_at + 1)
            hand_back = list(failure.get("hand_back") or [stopped_at])
            done_steps = {item["step"] for item in outcomes if item["status"] == "done"}
            output["remaining_steps"] = [
                f"{i + 1}. {describe_step(step)}" for i, step in enumerate(steps)
                if i + 1 >= stopped_at and i + 1 not in done_steps
            ]
            listed = ", ".join(str(number) for number in hand_back)
            output["next"] = (
                f"Steps marked done are verified; do not redo them. Observe the tab and do step(s) {listed} "
                "yourself, then continue with browser_flow action=run and from_step="
                f"{resume_from} (or do the remaining steps directly)."
            )
        if isinstance(last.get("page"), dict):
            output["page"] = last["page"]
        try:
            self._flow_store().note_run(owner, flow_id, output)
        except Exception:
            log.debug("Flow run bookkeeping failed", exc_info=True)
        return output

    async def _flow_run_step(
        self, owner: str, session_id: str, tab_id: str, step: dict[str, Any], *,
        actor: str, actor_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """(result, target_missing) for one step, waiting briefly for its target.

        Upstream waits for elements with exponential backoff (5 s base) before
        matching; a step here re-observes on each try, so the waits are short.
        """
        result: dict[str, Any] = {}
        for delay in _FLOW_TARGET_WAITS:
            if delay:
                await asyncio.sleep(delay)
            result = await self.act(
                owner, session_id, tab_id, {"kind": "sequence", "steps": [_runnable(step)]},
                actor=actor, actor_id=actor_id,
            )
            if not _target_missing(_first_result(result)):
                return result, False
        return result, True
