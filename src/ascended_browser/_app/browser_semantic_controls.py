"""Semantic form-control primitives for browser automation.

The selector engine follows the battle-tested shape used by Browser Use and
Playwright while staying Firefox/Camoufox friendly: native controls use native
Playwright APIs; composite widgets are opened only when needed, resolved to an
owned/unique popup, matched exactly, interacted with normally, and verified by
fresh control-local readback.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextvars import ContextVar
import re
import time
import unicodedata
from typing import Any

from ascended_browser._app.browser_deadline import browser_deadline

# Playwright's transport waits on a protocol future which is not cancelled
# with its caller. Read-only probes may finish after the action deadline: keep
# them alive solely to receive/discard the reply, never to dispatch a recovery.
_DRAINING_READS: set[asyncio.Task] = set()


class SemanticControlError(RuntimeError):
    """Raised when a semantic control action cannot be completed and verified."""


class SemanticOptionNotFound(SemanticControlError):
    """Raised when an opened custom control has no unique visible option match.

    Carries the options that were actually visible when the match failed. That
    evidence exists only while the popup is open, and the popup closes with the
    failure, so no later observation can recover it — a caller left without it
    can only guess again. ``observed_options`` is the list; ``for_ref`` and
    ``popup_strategy`` say which control and which popup it came from.

    Nothing was committed: this is raised before any option is clicked, and a
    typed filter is cleared on the way out. A batch may therefore carry on with
    the fields that do not depend on this one.
    """

    dispatched = False

    def __init__(
        self,
        message: str,
        *,
        observed_options: list[str] | None = None,
        popup_strategy: str = "",
        for_ref: str = "",
    ) -> None:
        super().__init__(message)
        self.observed_options = list(observed_options or [])
        self.popup_strategy = popup_strategy
        self.for_ref = for_ref


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fold(value: Any) -> str:
    return _norm(value).casefold()


def _first(locator: Any) -> Any:
    value = getattr(locator, "first", locator)
    return value() if callable(value) else value


async def _safe_call(obj: Any, name: str, *args: Any, default: Any = None,
                     read_only: bool = False, **kwargs: Any) -> Any:
    fn = getattr(obj, name, None)
    if not callable(fn):
        return default
    try:
        if read_only or name in {"count", "get_attribute", "input_value", "inner_text", "text_content", "is_visible"}:
            task = asyncio.create_task(fn(*args, **kwargs))
            _DRAINING_READS.add(task)
            def complete(done):
                _DRAINING_READS.discard(done)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(complete)
            return await asyncio.shield(task)
        return await fn(*args, **kwargs)
    except Exception:
        return default


async def _locator_tag(locator: Any, candidate: dict[str, Any]) -> str:
    tag = _fold(candidate.get("tag"))
    if tag:
        return tag
    tag = await _safe_call(locator, "evaluate", "el => el.tagName.toLowerCase()", default="", read_only=True)
    return _fold(tag)


def is_native_select(candidate: dict[str, Any]) -> bool:
    return _fold(candidate.get("tag")) == "select"


def _css_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


async def _visible_exact_matches(locator: Any, value: str) -> list[Any]:
    if locator is None:
        return []
    count = await _safe_call(locator, "count", default=None)
    if count is None:
        count = 1
    matches: list[Any] = []
    for index in range(int(count or 0)):
        nth = getattr(locator, "nth", None)
        item = nth(index) if callable(nth) else locator
        if not await _safe_call(item, "is_visible", default=True):
            continue
        text = await _safe_call(item, "inner_text", default=None)
        if text is None:
            text = await _safe_call(item, "text_content", default="")
        if _fold(text) == _fold(value):
            matches.append(item)
    return matches


async def _visible_exact(locator: Any, value: str) -> Any | None:
    matches = await _visible_exact_matches(locator, value)
    return matches[0] if len(matches) == 1 else None


async def _option_from_role(scope: Any, role: str, value: str) -> Any | None:
    getter = getattr(scope, "get_by_role", None)
    if not callable(getter):
        return None
    try:
        locator = getter(role, name=value, exact=True)
    except TypeError:
        locator = getter(role, name=value)
    return await _visible_exact(locator, value)


async def _controlled_scopes(
    page: Any, candidate: dict[str, Any], *, resolve_proxy: bool = False,
) -> list[Any]:
    """Resolve aria-controls without assuming the ID is a CSS-safe token."""
    scopes: list[Any] = []
    controls = _norm(candidate.get("aria_controls"))
    for control_id in controls.split():
        try:
            scope = _first(page.locator(f'[id="{_css_string(control_id)}"]'))
            if await _safe_call(scope, "count", default=1):
                scopes.append(await _rendered_popup_for(page, scope) if resolve_proxy else scope)
        except Exception:
            continue
    return scopes


# Some widgets point aria-controls at an accessibility proxy: a zero-size
# listbox whose role=option items carry values ("jack"), while the options a
# person sees ("Jack") are rendered beside it without a role (Ant Design 5).
# The popup that owns both is the proxy's nearest rendered ancestor.
_MARK_RENDERED_POPUP = """el => new Promise(resolve => {
  const rendered = node => {
    const rect = node.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  // A popup that animates open is not rendered in its first frames; wait a
  // bounded moment for it rather than reading the proxy and finding nothing.
  const started = performance.now();
  const look = () => {
    if (rendered(el)) return resolve(null);  // a real popup: use it as is
    // A proxy holds options. An empty list is just loading (or has no
    // matches) and must be read as it is, at once.
    if (!el.querySelector('[role="option"]')) return resolve(null);
    // Only a displayed zero-size box is a proxy. A display:none listbox is a
    // closed popup, and its rendered ancestor could be the whole page.
    if (el.getClientRects().length) {
      for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
        if (!rendered(node)) continue;
        const mark = el.id || 'proxy';
        node.setAttribute('data-odysseus-proxy-popup', mark);
        return resolve({proxy_popup: mark});
      }
    }
    if (!el.isConnected || performance.now() - started > 1200) return resolve(null);
    setTimeout(look, 50);
  };
  look();
})"""


async def _rendered_popup_for(page: Any, scope: Any) -> Any:
    found = await _safe_call(scope, "evaluate", _MARK_RENDERED_POPUP, default=None, read_only=True)
    mark = found.get("proxy_popup") if isinstance(found, dict) else None
    if not isinstance(mark, str) or not mark:
        return scope
    return _first(page.locator(f'[data-odysseus-proxy-popup="{_css_string(mark)}"]'))


async def _popup_scopes(page: Any, candidate: dict[str, Any]) -> tuple[list[Any], str]:
    # The popup is open (or opening) here, so an accessibility proxy can be
    # resolved to the popup that renders the options beside it.
    controlled = await _controlled_scopes(page, candidate, resolve_proxy=True)
    if controlled:
        return controlled, "aria_controls"

    lists = '[role="listbox"], [role="menu"], [role="tree"], [role="grid"]'
    if candidate.get("_fresh_lists_only"):
        # Only lists that appeared after this control was opened: a chip list,
        # another field's selections or an inline list was there before.
        lists = ", ".join(f"{part}:not([data-odysseus-preopen-list])" for part in lists.split(", "))
    try:
        popups = page.locator(lists)
    except Exception:
        return [], "none"
    count = int(await _safe_call(popups, "count", default=0) or 0)
    visible: list[Any] = []
    for index in range(min(count, 16)):
        popup = popups.nth(index)
        if await _safe_call(popup, "is_visible", default=True):
            visible.append(popup)
    if not visible and candidate.get("_fresh_lists_only"):
        plain = await _plain_fresh_popups(page)
        if plain:
            return plain, "plain_popup"
    return visible, "visible_popup"


# jQuery UI's autocomplete (and many hand-rolled suggestion lists) renders a
# bare <ul><li> with no role at all. Only for a typed control, and only when
# no ARIA popup exists: a list that was not showing before the control was
# opened and shows items now is its popup. Pre-existing lists were marked by
# _MARK_PREOPEN_SCRIPT, so navigation menus never qualify.
_MARK_PLAIN_POPUPS = """() => {
  document.querySelectorAll('[data-odysseus-plain-popup], [data-odysseus-plain-option]').forEach(node => {
    node.removeAttribute('data-odysseus-plain-popup');
    node.removeAttribute('data-odysseus-plain-option');
  });
  let count = 0;
  document.querySelectorAll('ul:not([data-odysseus-preopen-list]), ol:not([data-odysseus-preopen-list])')
    .forEach(list => {
      if (!list.getClientRects().length) return;
      if (list.querySelector('input, select, textarea')) return;
      const items = Array.from(list.children).filter(li => li.tagName === 'LI' && li.getClientRects().length);
      if (!items.length) return;
      list.setAttribute('data-odysseus-plain-popup', String(count++));
      items.forEach(li => li.setAttribute('data-odysseus-plain-option', ''));
    });
  return count;
}"""


async def _plain_fresh_popups(page: Any) -> list[Any]:
    count = await _safe_call(page, "evaluate", _MARK_PLAIN_POPUPS, default=0, read_only=True)
    if not isinstance(count, int) or count < 1 or count > 3:
        return []  # none, or too many new lists to claim any one of them
    return [_first(page.locator(f'[data-odysseus-plain-popup="{index}"]')) for index in range(count)]


_OPTION_SELECTOR = (
    '[role="option"], [role="menuitem"], [role="menuitemradio"], '
    '[role="radio"], [role="treeitem"], option, '
    # Role-less items that still state their selection (Ant Design 5's visible
    # options). Only ever read inside a popup scope, never page-wide.
    '[aria-selected]:not([role]), '
    # Items of a bare list claimed as a typed control's popup.
    '[data-odysseus-plain-option]'
)


# A deliberate listing is the model's only view of a closed popup, so it reads
# further than the failure-path hint does (a Canadian province list is 13).
_LIST_OPTIONS_LIMIT = 40


_POPUP_BUSY = """(el) => {
  if (!el) return false;
  if (el.getAttribute('aria-busy') === 'true') return true;
  if (el.querySelector('[aria-busy="true"], [role="progressbar"], [class*="loading" i], [class*="spinner" i]')) return true;
  return false;
}"""


async def _popup_is_loading(scopes: list[Any]) -> bool:
    """Whether the popup says it is still fetching what to show.

    A list that has not arrived is not a list that lacks the option, and
    quoting a half-filled popup as "the visible options" sent a live run
    hunting through choices the page was about to replace.
    """
    for scope in scopes or []:
        if await _safe_call(scope, "evaluate", _POPUP_BUSY, default=False, read_only=True):
            return True
    return False


async def _settled_option_texts(scopes: list[Any], *, limit: int = 12,
                                settle_ms: int = 700) -> tuple[list[str], bool]:
    """The popup's options once they stop changing, and whether it is loading.

    Read twice with a pause: a list mid-fetch reports different contents each
    time, and the caller is about to be told these are its choices.
    """
    from ascended_browser._app.browser_deadline import remaining_seconds

    def pause() -> float:
        # Never wait past the operation's own deadline: a caller that asked for
        # a fast failure must still get one.
        left = remaining_seconds(default=settle_ms / 1000)
        budget = min(settle_ms / 1000, max(0.0, (left or 0.0) - 0.15))
        return budget

    first = await _visible_option_texts(scopes, limit=limit)
    loading = await _popup_is_loading(scopes)
    wait = pause()
    if wait <= 0:
        return first, loading
    await asyncio.sleep(wait)
    second = await _visible_option_texts(scopes, limit=limit)
    loading = await _popup_is_loading(scopes)
    if first != second and pause() > 0:
        # Still moving: give it one more turn, then report what it settled on.
        await asyncio.sleep(pause())
        second = await _visible_option_texts(scopes, limit=limit)
    return second, loading


async def _visible_option_texts(scopes: list[Any], *, limit: int = 12) -> list[str]:
    """Enumerate the option labels currently visible in the open popup(s).

    Best effort and never raising: this runs on the failure path, where the
    caller already has nothing. A partial list still tells the caller whether
    it had the wrong control, the wrong spelling, or an empty popup.
    """
    texts: list[str] = []
    seen: set[str] = set()
    for scope in scopes or []:
        try:
            options = scope.locator(_OPTION_SELECTOR)
        except Exception:
            continue
        count = int(await _safe_call(options, "count", default=0) or 0)
        for index in range(min(count, limit * 2)):
            if len(texts) >= limit:
                return texts
            item = options.nth(index)
            if not await _safe_call(item, "is_visible", default=True):
                continue
            text = await _safe_call(item, "inner_text", default=None)
            if text is None:
                text = await _safe_call(item, "text_content", default="")
            label = _norm(text)[:80]
            if label and label.casefold() not in seen:
                seen.add(label.casefold())
                texts.append(label)
    return texts


async def _filter_with_query(locator: Any, text: str, *, timeout_ms: int) -> bool:
    """Type a filter into a combobox without spending the whole budget on it.

    Typing is an optimisation — it narrows a long option list. Two real widget
    families (react-select's single input, flatpickr's calendar input) expose a
    control that looks fillable but is ``readonly``, and Playwright's ``fill``
    retries "element is not editable" until the deadline. That consumed the
    entire operation budget before the popup was ever read, so a control that
    was merely un-typeable timed out as if it were broken.

    Skip the fill when the element reports itself non-editable, and cap what a
    fill may cost so the budget that matters — opening, reading and committing —
    survives. Returns whether the filter text was applied.
    """
    fill = getattr(locator, "fill", None)
    if not callable(fill) or text is None:
        return False
    if not await _safe_call(locator, "is_editable", default=True, read_only=True):
        return False
    budget = max(250, min(int(timeout_ms), 1500))
    try:
        await fill(text, timeout=budget)
        return True
    except Exception:
        return False


async def _open_control(locator: Any, *, timeout_ms: int, page: Any = None) -> bool:
    """Open a composite control, without letting the open consume the budget.

    Opening is a means, not the goal. react-select renders its single-value
    combobox as an off-viewport ``dummyInput``; Playwright's click retries
    "element is outside of the viewport" until the deadline, so the whole
    operation timed out before any option could be read.

    Cap the click, then fall back to the keyboard. ArrowDown is the ARIA
    combobox contract for opening a listbox, so this stays a semantic
    interaction rather than a widget-specific hack.
    """
    async def opened() -> bool:
        if _fold(await _safe_call(locator, "get_attribute", "aria-expanded", default="")) == "true":
            return True
        # A control that never sets aria-expanded (Workday's search box) has
        # opened when a list that was not there before now shows items.
        return page is not None and await _safe_call(
            page, "evaluate", _FRESH_LIST_SHOWN, default=False, read_only=True,
        ) is True

    if await opened():
        return True
    budget = max(250, min(int(timeout_ms), 2000))
    await _safe_call(locator, "click", timeout=budget)
    if await opened():
        return True
    # Focus then ArrowDown: works on controls whose hit target is a wrapper.
    await _safe_call(locator, "focus", timeout=budget)
    await _safe_call(locator, "press", "ArrowDown", timeout=budget)
    if await opened():
        return True
    # Alt+ArrowDown is the ARIA combobox key for "open the list".
    await _safe_call(locator, "press", "Alt+ArrowDown", timeout=budget)
    if await opened():
        return True
    # A search box that declares Enter runs its search, with no form to submit,
    # lists everything for an empty search (Workday's "How did you hear").
    if await _safe_call(locator, "evaluate", _ENTER_RUNS_SEARCH, default=False, read_only=True) is True:
        await _safe_call(locator, "press", "Enter", timeout=budget)
        await asyncio.sleep(0.3)
    return await opened()


def _near_matches(observed: list[str], value: str) -> list[str]:
    """Visible option labels that read like the requested text.

    Real option lists decorate their labels: a country list reads "Canada +1",
    a pronouns list "they/them/theirs". Selection stays exact — a form must
    never be given a value nobody asked for — so these are for the refusal to
    name, turning a guess from a truncated list into one precise retry.
    Prefix first, then containment, so the closest reading is offered first.
    """
    wanted = _fold(value)
    if not wanted:
        return []
    for rule in (
        lambda item: _fold(item).startswith(wanted),
        lambda item: wanted in _fold(item),
    ):
        hits = [item for item in observed if rule(item)]
        if hits:
            return hits
    return []


async def _find_option_in_scope(scope: Any, value: str) -> Any | None:
    for role in ("option", "menuitem", "menuitemradio", "radio", "treeitem", "row"):
        option = await _option_from_role(scope, role, value)
        if option is not None:
            return option
    try:
        options = scope.locator(_OPTION_SELECTOR)
    except Exception:
        return None
    return await _visible_exact(options, value)


async def _find_option(page: Any, candidate: dict[str, Any], value: str) -> tuple[Any | None, Any | None, str]:
    """Find one exact option inside exactly one plausible popup.

    A global same-valued option elsewhere on the page must never prove popup
    ownership. aria-controls wins; otherwise exactly one visible popup must
    contain the exact option. Only when there are no popup containers at all do
    we allow one globally unique visible ARIA option (common portal shape).
    """
    scopes, strategy = await _popup_scopes(page, candidate)
    if scopes:
        hits: list[tuple[Any, Any]] = []
        for scope in scopes:
            option = await _find_option_in_scope(scope, value)
            if option is not None:
                hits.append((scope, option))
        if len(hits) == 1:
            scope, option = hits[0]
            return option, scope, strategy
        if len(hits) > 1:
            return None, None, "ambiguous_popup"
        # No match yet, but one popup owns this control: hand it back so a
        # virtualized list can be scrolled rather than rediscovered. Without
        # this, an option below the mounted window was simply unreachable.
        return None, (scopes[0] if len(scopes) == 1 else None), strategy

    for role in ("option", "menuitem", "menuitemradio", "radio", "treeitem"):
        option = await _option_from_role(page, role, value)
        if option is not None:
            return option, None, "unique_portal_option"
    try:
        global_options = page.locator(
            '[role="option"], [role="menuitem"], [role="menuitemradio"], [role="radio"], [role="treeitem"]'
        )
    except Exception:
        return None, None, "none"
    option = await _visible_exact(global_options, value)
    return (option, None, "unique_portal_option") if option is not None else (None, None, "none")


def _ladder_page(page: Any) -> Any:
    """The Page behind ``page`` (a Page, or a Frame of one) when it can take input."""
    owner = getattr(page, "page", None) if not hasattr(page, "keyboard") else page
    owner = owner if owner is not None else page
    return owner if hasattr(owner, "keyboard") and hasattr(owner, "mouse") else None


async def _click_option(option: Any, timeout_ms: int, *, page: Any = None) -> None:
    ladder_page = _ladder_page(page) if page is not None else None
    if ladder_page is not None:
        from ascended_browser._app.browser_click_helpers import _is_timeout_error, activate_locator, is_click_intercept_error
        from ascended_browser._app.browser_presented_tab import presented_state

        started = time.monotonic()
        if presented_state(ladder_page) != "background":
            # The direct click first: an open list can re-render under the
            # ladder's preparatory reads (a debounced search rebuilt it), and
            # when the direct click lands it is the fastest commit there is.
            try:
                await option.click(timeout=min(timeout_ms, 1500), no_wait_after=True)
                return
            except Exception as exc:
                if not (_is_timeout_error(exc) or is_click_intercept_error(exc)):
                    raise
        # The shared click ladder (owned-point mouse, keyboard, background-tab
        # rule). A bare option.click stalled where 74% of clicks needed a
        # fallback rung, and the stalled commit ate the select's deadline.
        remaining = timeout_ms / 1000 - (time.monotonic() - started)
        await activate_locator(
            ladder_page, page, option, target="the option",
            budget_seconds=max(1.0, remaining),
        )
        return
    try:
        await option.click(timeout=timeout_ms, no_wait_after=True)
    except TypeError:
        # Lightweight test adapters and older Playwright bindings may not
        # expose no_wait_after. The ordinary actionability-checked click is
        # still the compatibility path; never force or mutate through JS.
        await option.click(timeout=timeout_ms)


def _click_failure_reason(exc: BaseException) -> str:
    """The actionability line Playwright logged last, not its whole call log."""
    text = str(exc or "")
    for marker in ("intercepts pointer events", "not stable", "not visible",
                   "detached", "not enabled", "outside of the viewport"):
        for line in reversed(text.splitlines()):
            if marker in line:
                return line.strip(" -")[:160]
    first = text.splitlines()[0] if text else type(exc).__name__
    return first[:160]


async def _wait_for_option(
    page: Any,
    candidate: dict[str, Any],
    value: str,
    *,
    timeout_ms: int,
) -> tuple[Any | None, Any | None, str]:
    option, popup, strategy = await _find_option(page, candidate, value)
    if option is not None:
        return option, popup, strategy

    # Let the DOM wake us when an asynchronous option arrives. This is both
    # faster than a fixed polling loop on responsive widgets and cheaper on
    # large pages. The exact ownership check above/below remains authoritative;
    # the observer is only a readiness signal.
    #
    # The ceiling used to be 1.5s, which is a local widget's budget, not a
    # server's. Workday's "Degree" and "How did you hear about us" search a
    # backend on every keystroke: the wait expired before the answer arrived,
    # and the refusal then quoted whichever options were still on screen —
    # 'Web' came back offered "Conference/Professional Organization" and
    # "University". This is the failure path, so spending more of the caller's
    # own budget here costs nothing when the option does arrive early.
    # A share of the budget, not all of it: the refusal still has to read the
    # popup and say what it offers, and a wait that consumes everything turns a
    # precise "did you mean" into a bare deadline.
    wait_ms = max(1, min(int(timeout_ms * 0.6), 6000))
    ids = [
        part for part in str(candidate.get("aria_controls") or "").split()
        if part
    ]
    signalled = await _safe_call(
        page,
        "evaluate",
        r"""({ids, value, timeout}) => new Promise(resolve => {
          const fold = text => String(text || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase();
          const wanted = fold(value);
          const scopes = ids.map(id => document.getElementById(id)).filter(Boolean);
          const roots = scopes.length ? scopes : [document];
          const found = () => roots.some(root => Array.from(root.querySelectorAll(
            '[role="option"], [role="menuitem"], [role="menuitemradio"], [role="radio"], [role="treeitem"]'
          )).some(el => el.getClientRects().length && fold(el.textContent) === wanted));
          if (found()) return resolve(true);
          let done = false;
          const finish = value => {
            if (done) return;
            done = true;
            observer.disconnect();
            clearTimeout(timer);
            resolve(value);
          };
          const observer = new MutationObserver(() => { if (found()) finish(true); });
          for (const root of roots) observer.observe(root, {
            subtree: true, childList: true, attributes: true,
            attributeFilter: ['hidden', 'style', 'aria-hidden', 'aria-busy']
          });
          const timer = setTimeout(() => finish(false), timeout);
        })""",
        {"ids": ids, "value": value, "timeout": wait_ms},
        default=None,
        read_only=True,
    )
    if signalled is None:
        # Lightweight adapters used by tests and older runtimes may not expose
        # evaluate. Preserve compatibility with one bounded scheduler wait.
        await _safe_call(page, "wait_for_timeout", min(wait_ms, 100))
        await asyncio.sleep(0)
    return await _find_option(page, candidate, value)


async def _scroll_virtualized_popup_for_option(
    page: Any,
    candidate: dict[str, Any],
    value: str,
    popup: Any | None,
) -> tuple[Any | None, Any | None, str]:
    """Bounded incremental scroll for virtualized menus whose option is not mounted yet."""
    if popup is None:
        scopes, strategy = await _popup_scopes(page, candidate)
        if len(scopes) != 1:
            return None, None, strategy
        popup = scopes[0]
    for _ in range(20):
        option = await _find_option_in_scope(popup, value)
        if option is not None:
            return option, popup, "virtualized_popup"
        movement = await _safe_call(
            popup,
            "evaluate",
            """el => {
              const before = Number(el.scrollTop || 0);
              const step = Math.max(80, Math.floor((el.clientHeight || 240) * 0.8));
              el.scrollTop = Math.min(el.scrollHeight || before + step, before + step);
              return {before, after: Number(el.scrollTop || 0), max: Number((el.scrollHeight || 0) - (el.clientHeight || 0))};
            }""",
            default=None,
        )
        if not isinstance(movement, dict) or movement.get("after") == movement.get("before"):
            break
        await _safe_call(page, "wait_for_timeout", 40)
    return None, popup, "virtualized_popup"


async def _append_selected_values(scope: Any, values: list[str]) -> None:
    try:
        selected = scope.locator(
            '[role="option"][aria-selected="true"], '
            '[role="menuitemradio"][aria-checked="true"], '
            '[role="radio"][aria-checked="true"]'
        )
        count = await _safe_call(selected, "count", default=0)
        for index in range(min(int(count or 0), 8)):
            item = selected.nth(index)
            if not await _safe_call(item, "is_visible", default=True):
                continue
            selected_text = await _safe_call(item, "inner_text", default="")
            if selected_text:
                values.append(_norm(selected_text))
    except Exception:
        pass


async def _append_composite_chip_values(locator: Any, values: list[str]) -> None:
    """Read selected chips from composite multi-selects such as React Select.

    React Select clears the search input after committing and often removes the
    chosen option from the listbox. Its durable evidence is a nearby chip with
    an accessible remove control (for example ``aria-label=\"Remove Blue\"``).
    Read only the nearest ancestor that exposes such chips so unrelated Remove
    buttons elsewhere on the page cannot prove selection.
    """
    chips = await _safe_call(
        locator,
        "evaluate",
        """el => {
          let node = el;
          for (let depth = 0; node && depth < 7; depth++, node = node.parentElement) {
            if (node.matches('form, body, html')) break;
            const controls = node.querySelectorAll('[role="combobox"], select');
            if (controls.length > 1 || (controls.length === 1 && controls[0] !== el)) break;
            // Items the field itself reports as chosen, by ARIA state. Only
            // inside the field's own container: stop before the walk reaches
            // a sibling field and its selections.
            const typed = Array.from(node.querySelectorAll('input:not([type]), input[type="text"], input[type="search"]'));
            if (typed.some(input => input !== el)) break;
            const picked = Array.from(node.querySelectorAll(
              '[role="option"][aria-selected="true"], [role="option"][aria-checked="true"]'))
              .filter(item => item.getClientRects().length)
              .map(item => String(item.innerText || item.getAttribute('aria-label') || '').trim())
              .filter(Boolean);
            if (picked.length) return picked;
            const labels = Array.from(node.querySelectorAll('[aria-label]'))
              .map(candidate => String(candidate.getAttribute('aria-label') || '').trim())
              .filter(label => /^remove\\s+/i.test(label));
            if (labels.length) {
              return labels.map(label => label.replace(/^remove\\s+/i, '').trim()).filter(Boolean);
            }
          }
          return [];
        }""",
        default=[], read_only=True,
    )
    if isinstance(chips, list):
        values.extend(_norm(item) for item in chips if _norm(item))
    baseline = _FIELD_ITEMS_BEFORE.get()
    if baseline is not None:
        # Chips with no ARIA state (Workday's, React Select's): an item that
        # appeared in the field's own container after the commit click. The
        # popup's options were there before the click, so they never count.
        after = Counter(await _field_item_texts(locator))
        values.extend(text for text, count in after.items() if count > baseline.get(text, 0))


# Item texts inside the field's own container, taken just before a commit
# click, so readback can tell an added chip from an option that was already
# showing. None outside a select operation.
_FIELD_ITEMS_BEFORE: ContextVar[Counter | None] = ContextVar("field_items_before", default=None)

_FIELD_ITEMS_SCRIPT = """el => {
  let container = null;
  for (let node = el.parentElement, depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
    if (node.matches('form, body, html')) break;
    const others = Array.from(node.querySelectorAll(
      'input:not([type]), input[type="text"], input[type="search"], select, [role="combobox"]'))
      .filter(other => other !== el && !other.contains(el));
    if (others.length) break;
    container = node;
  }
  if (!container) return [];
  // The list the choice was clicked in is not evidence of the choice: the
  // one marked before the click, its re-render (same id), or any list still
  // offering unselected options.
  const marked = document.querySelector('[data-odysseus-select-popup]');
  const markedId = marked ? marked.getAttribute('data-odysseus-select-popup') : '';
  const inPopup = item => {
    const list = item.closest('[role="listbox"], [role="menu"], [role="tree"], [role="grid"]');
    if (!list) return false;
    if (list.hasAttribute('data-odysseus-select-popup')) return true;
    if (markedId && markedId !== '1' && list.id === markedId) return true;
    return !!list.querySelector('[aria-selected="false"]');
  };
  // Never the control's own contents or the popup it names: a control that
  // is itself a list would otherwise read its options as chosen items.
  const owned = (el.getAttribute('aria-controls') || '').split(/\\s+/)
    .map(id => id && document.getElementById(id)).filter(Boolean);
  return Array.from(container.querySelectorAll('[role="option"], [role="listitem"], li, [role="button"]'))
    .filter(item => item.getClientRects().length && !inPopup(item)
      && !el.contains(item) && !owned.some(list => list.contains(item)))
    .map(item => String(item.innerText || '').split('\\n')[0].replace(/\\s+/g, ' ').trim())
    .filter(Boolean);
}"""


_ENTER_RUNS_SEARCH = """el => !el.closest('form') && (
  (el.getAttribute('enterkeyhint') || '').toLowerCase() === 'search'
  || el.getAttribute('role') === 'searchbox'
)"""


_MARK_POPUP_SCRIPT = """el => {
  document.querySelectorAll('[data-odysseus-select-popup]')
    .forEach(node => node.removeAttribute('data-odysseus-select-popup'));
  const list = el.closest('[role="listbox"], [role="menu"], [role="tree"], [role="grid"]');
  if (list) list.setAttribute('data-odysseus-select-popup', list.id || '1');
}"""


_CLEAR_POPUP_MARK_SCRIPT = """() => document.querySelectorAll('[data-odysseus-select-popup]')
  .forEach(node => node.removeAttribute('data-odysseus-select-popup'))"""


_MARK_PREOPEN_SCRIPT = """() => document.querySelectorAll(
  '[role="listbox"], [role="menu"], [role="tree"], [role="grid"], ul, ol').forEach(list => {
    // A list already showing items. An always-rendered but empty popup
    // container is not one: it fills when opened and must stay eligible,
    // and neither is the bare list an earlier read claimed as this control's
    // popup (listing, then selecting, leaves it open).
    if (list.hasAttribute('data-odysseus-plain-popup')) return;
    if (list.getClientRects().length && list.querySelector('[role="option"], [role="menuitem"], [role="treeitem"], [role="row"], li'))
      list.setAttribute('data-odysseus-preopen-list', '');
  })"""
_CLEAR_PREOPEN_SCRIPT = """() => document.querySelectorAll('[data-odysseus-preopen-list]')
  .forEach(node => node.removeAttribute('data-odysseus-preopen-list'))"""


async def _scope_to_fresh_lists(page: Any, locator: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    """Before opening a closed control, remember which lists were already there.

    A control that names no popup (no aria-controls) had "any visible list on
    the page" as its popup, so Workday's chip lists — this field's and the
    phone field's — were read as its options and clicked as if they were.
    """
    if _fold(await _safe_call(locator, "get_attribute", "aria-expanded", default="")) == "true":
        return candidate  # already open: its list is legitimately visible
    marked = await _safe_call(page, "evaluate", _MARK_PREOPEN_SCRIPT, default=_FAILED_MARK)
    return candidate if marked is _FAILED_MARK else {**candidate, "_fresh_lists_only": True}


_FAILED_MARK = object()


_FRESH_LIST_SHOWN = """() => Array.from(document.querySelectorAll(
  '[role="listbox"], [role="menu"], [role="tree"], [role="grid"], [data-odysseus-plain-popup]'))
  .some(list => !list.hasAttribute('data-odysseus-preopen-list') && list.getClientRects().length
    && list.querySelector('[role="option"], [role="menuitem"], [role="treeitem"], [role="row"], li'))"""


async def _wait_for_any_option(page: Any, candidate: dict[str, Any], *, timeout_ms: int) -> None:
    """Return once the control's popup shows at least one option, or time out."""
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
    while True:
        scopes, _strategy = await _popup_scopes(page, candidate)
        for scope in scopes:
            try:
                options = scope.locator(_OPTION_SELECTOR)
            except Exception:
                continue
            if int(await _safe_call(options, "count", default=0) or 0):
                return
        left = deadline - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(0.15, left))


async def _field_item_texts(locator: Any) -> list[str]:
    items = await _safe_call(locator, "evaluate", _FIELD_ITEMS_SCRIPT, default=[], read_only=True)
    return [_norm(item) for item in items if isinstance(item, str) and _norm(item)] if isinstance(items, list) else []


async def _control_readback(page: Any, selector: str, candidate: dict[str, Any]) -> list[str]:
    try:
        matches = page.locator(selector)
        if await _safe_call(matches, "count", default=0) != 1:
            return []
        locator = _first(matches)
    except Exception:
        return []
    values: list[str] = []
    for attr in ("aria-valuetext", "data-value"):
        value = await _safe_call(locator, "get_attribute", attr, default="")
        if value:
            values.append(_norm(value))
    tag = await _locator_tag(locator, candidate)
    # An open composite input contains a search query, not a selection, and
    # reading it as evidence would accept the typing as the answer. Once the
    # control reports itself closed, what it holds is what it committed — which
    # is how plain comboboxes and Workday's own fields state a selection. A
    # live run read back nothing at all for those and called a committed value
    # unverified ("expected 'Job Sites', observed []").
    if tag == "select":
        input_value = await _safe_call(locator, "input_value", default="")
        if input_value:
            values.append(_norm(input_value))
    elif tag == "input":
        expanded = _fold(await _safe_call(
            locator, "get_attribute", "aria-expanded", default="", read_only=True,
        ))
        # Only an explicit "closed" counts. A control that says nothing about
        # being open could be mid-search, and its text is then the query.
        # A role-less typeahead states nothing about being open; once the bare
        # list claimed as its popup has closed, what it holds is its value.
        # Some reopen it on the committed text (jQuery UI searches it again):
        # a value that is exactly one of the offered options is a choice, not
        # the partial query this tool typed.
        plain_closed = not expanded and await _safe_call(locator, "evaluate", """el => {
          const claimed = Array.from(document.querySelectorAll('[data-odysseus-plain-popup]'));
          if (!claimed.length) return false;
          const open = claimed.filter(list => list.getClientRects().length);
          if (!open.length) return true;
          const fold = text => String(text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const value = fold(el.value);
          return !!value && open.some(list => Array.from(list.children)
            .some(item => fold(item.textContent) === value));
        }""", default=False, read_only=True) is True
        if expanded == "false" or plain_closed:
            input_value = await _safe_call(locator, "input_value", default="")
            if input_value:
                values.append(_norm(input_value))

    native_selected = await _safe_call(
        locator,
        "evaluate",
        """el => {
          if (el.tagName !== 'SELECT' || !el.selectedOptions) return [];
          return Array.from(el.selectedOptions).map(option => ({
            value: option.value || '', text: (option.textContent || '').trim()
          }));
        }""",
        default=[], read_only=True,
    )
    if isinstance(native_selected, list):
        for option in native_selected:
            if not isinstance(option, dict):
                continue
            for key in ("value", "text"):
                if option.get(key):
                    values.append(_norm(option[key]))

    if tag not in {"select", "input", "textarea"}:
        for method in ("inner_text", "text_content"):
            text = await _safe_call(locator, method, default="")
            if text:
                values.append(_norm(text))
        # A trigger's text can include the text that names it (Radix: an
        # aria-labelledby span inside the button), so "Expiration7 days" was
        # read for a committed "7 days" and the select reported failure
        # (2026-10-04). Its value is the text left once naming nodes are set
        # aside: labelling descendants skipped, an outside label stripped as a
        # prefix. Semantic (aria-labelledby, <label>), never a site's markup.
        unnamed = await _safe_call(locator, "evaluate", """el => {
          const fold = t => String(t || '').replace(/\\s+/g, ' ').trim();
          const names = [
            ...Array.from(el.labels || []),
            ...(el.getAttribute('aria-labelledby') || '').split(/\\s+/)
              .map(id => id && document.getElementById(id)).filter(n => n && n !== el),
          ];
          const parts = [];
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const node = walker.currentNode;
            if (names.some(name => el.contains(name) && name.contains(node))) continue;
            parts.push(node.textContent);
          }
          let rest = fold(parts.join(' '));
          for (const name of names) {
            const text = fold(name.textContent);
            if (text && !el.contains(name) && rest.toLowerCase().startsWith(text.toLowerCase()))
              rest = fold(rest.slice(text.length));
          }
          return rest !== fold(el.innerText || el.textContent) ? rest : '';
        }""", default="", read_only=True)
        if isinstance(unnamed, str) and unnamed:
            values.append(_norm(unnamed))

    # React Select single-values are siblings of the search input. Recognize
    # that component's value slot, not arbitrary nearby matching text. Stop at
    # a second control or a form boundary rather than claiming shared ownership.
    # ARIA names the committed option directly. This is the standard path and
    # works regardless of how a library renders its value slot, so try it
    # before falling back to recognizing component markup.
    active = await _safe_call(locator, "evaluate", """el => {
      const id = el.getAttribute('aria-activedescendant');
      if (!id) return [];
      const option = document.getElementById(id);
      if (!option) return [];
      if (option.getAttribute('aria-selected') === 'false') return [];
      return [(option.textContent || '').trim()];
    }""", default=[], read_only=True)
    if isinstance(active, list):
        values.extend(_norm(item) for item in active if isinstance(item, str))

    if tag == "input":
        committed = await _safe_call(locator, "evaluate", """el => {
          // Value slots of the widget libraries that render the committed
          // selection outside the input: react-select, Ant Design, and the
          // common "-selectedValue"/"selection-item" naming. Matching is
          // scoped to the container that owns exactly this one control, so a
          // neighbouring control's value can never be claimed as evidence.
          const SLOTS = [
            '[class*="-singleValue"]', '[class*="__single-value"]',
            '[class*="selection-item"]', '[class*="selectionItem"]',
            '[class*="-selectedValue"]', '[class*="__selected-value"]',
          ].join(',');
          for (let node = el.parentElement, depth = 0; node && depth < 6;
               node = node.parentElement, depth++) {
            if (node.matches('form, body, html')) break;
            const controls = node.querySelectorAll('[role="combobox"], select');
            // Several libraries put role="combobox" on a wrapper rather than
            // on the input itself (Ant Design does). Ownership still means
            // exactly one control in this container — it just need not be the
            // same node we were handed.
            if (controls.length !== 1) break;
            const owner = controls[0];
            if (!(owner === el || owner.contains(el) || el.contains(owner))) break;
            const values = node.querySelectorAll(SLOTS);
            if (values.length > 1) return [];
            if (values.length === 1 && values[0].getClientRects().length)
              return [values[0].textContent.trim()];
            // No recognised value slot. Libraries rename these classes freely,
            // so fall back to what the owning container actually renders:
            // its visible text minus this input's own query text and any
            // placeholder. Ownership is already proven above (exactly one
            // control in this container), so the text cannot belong to a
            // neighbouring field.
            const placeholder = Array.from(
              node.querySelectorAll('[class*="placeholder" i],[data-placeholder]')
            );
            // The field's own name is not its value: a <label for> beside the
            // input read back as the committed option ("Framework").
            const names = [
              ...Array.from(el.labels || []),
              ...(el.getAttribute('aria-labelledby') || '').split(/\\s+/)
                .map(id => id && document.getElementById(id)).filter(Boolean),
            ];
            const parts = [];
            node.childNodes.forEach(child => {
              if (child === el || (child.contains && child.contains(el))) return;
              if (placeholder.some(p => p === child || (child.contains && child.contains(p)))) return;
              if (names.some(n => n === child || (child.contains && child.contains(n)))) return;
              if (child.getClientRects && !child.getClientRects().length) return;
              const text = (child.textContent || '').trim();
              if (text) parts.push(text);
            });
            if (parts.length === 1) return [parts[0]];
          }
          return [];
        }""", default=[], read_only=True)
        if isinstance(committed, list):
            values.extend(_norm(value) for value in committed if isinstance(value, str))

    for scope in await _controlled_scopes(page, candidate):
        await _append_selected_values(scope, values)
    await _append_composite_chip_values(locator, values)
    return list(dict.fromkeys(value for value in values if value))


def _value_matches(expected: str, observed: list[str]) -> bool:
    wanted = _fold(expected)
    for item in observed:
        folded = _fold(item)
        if folded == wanted:
            return True
    return False


async def _wait_for_value(page: Any, selector: str, candidate: dict[str, Any], value: str, timeout_ms: int) -> list[str]:
    last: list[str] = []
    try:
        async with browser_deadline(max(1, int(timeout_ms)) / 1000):
            last = await _control_readback(page, selector, candidate)
            if _value_matches(value, last):
                return last
            locator_factory = getattr(page, "locator", None)
            if callable(locator_factory):
                locator = _first(locator_factory(selector))
                wait_ms = max(1, min(int(timeout_ms), 1500))
                await _safe_call(
                    locator,
                    "evaluate",
                    """(el, {timeout, expected}) => new Promise(resolve => {
                      const fold = value => String(value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
                      const wanted = fold(expected);
                      let done = false;
                      let root = el;
                      for (let node = el.parentElement, depth = 0; node && depth < 7;
                           node = node.parentElement, depth++) {
                        if (node.matches('form, body, html')) break;
                        const controls = node.querySelectorAll('[role="combobox"], select');
                        if (controls.length > 1 || (controls.length === 1 && controls[0] !== el)) break;
                        root = node;
                      }
                      const matches = () => {
                        const values = [];
                        for (const name of ['aria-valuetext', 'data-value']) {
                          const current = el.getAttribute(name);
                          if (current) values.push(current);
                        }
                        if (el.tagName === 'SELECT' && el.selectedOptions) {
                          for (const option of el.selectedOptions) {
                            values.push(option.value || '');
                            values.push(option.textContent || '');
                          }
                        }
                        for (const node of root.querySelectorAll(
                          '[class*="-singleValue"], [class*="__single-value"]'
                        )) values.push(node.textContent || '');
                        for (const node of root.querySelectorAll('[aria-label]')) {
                          const label = String(node.getAttribute('aria-label') || '');
                          if (/^remove\\s+/i.test(label)) values.push(label.replace(/^remove\\s+/i, ''));
                        }
                        return values.some(current => fold(current) === wanted);
                      };
                      const finish = () => {
                        if (done) return;
                        done = true;
                        observer.disconnect();
                        clearTimeout(timer);
                        el.removeEventListener('input', changed);
                        el.removeEventListener('change', changed);
                        resolve(true);
                      };
                      if (matches()) return resolve(true);
                      const changed = () => { if (matches()) finish(); };
                      el.addEventListener('input', changed);
                      el.addEventListener('change', changed);
                      const observer = new MutationObserver(changed);
                      observer.observe(root, {subtree: true, childList: true, characterData: true, attributes: true});
                      const timer = setTimeout(finish, timeout);
                    })""",
                    {"timeout": wait_ms, "expected": value},
                    default=None,
                    read_only=True,
                )
            last = await _control_readback(page, selector, candidate)
            if _value_matches(value, last):
                return last
    except TimeoutError:
        pass
    if _displays_part_of_option(value, last):
        raise _PartialDisplay(last)
    raise SemanticControlError(f"option click did not verify: expected {value!r}, observed {last!r}")


async def _opened_submenu(page: Any, candidate: dict[str, Any], chosen: str) -> list[str]:
    """Options a clicked entry revealed in place of committing, if it did that.

    The popup is still open, the entry clicked is no longer among its options
    and other options are: the entry opened the next level of a menu.
    """
    try:
        scopes, _strategy = await _popup_scopes(page, candidate)
        options, _loading = await _settled_option_texts(scopes)
    except Exception:
        return []
    texts = [_norm(text) for text in options or [] if _norm(text)]
    if not texts or _fold(chosen) in {_fold(text) for text in texts}:
        return []
    return texts


class _PartialDisplay(Exception):
    """The control committed and shows a derived part of the option's label."""

    def __init__(self, observed: list[str]) -> None:
        super().__init__("option committed with a derived display value")
        self.observed = observed


def _displays_part_of_option(expected: str, observed: list[str]) -> bool:
    """Whether the control shows one whole piece of the option it was given.

    A composite control renders what it stores, not the label that was
    clicked: a country field with its own dial-code input shows "+1" after
    "Canada +1" is chosen, and a strict readback called that real selection a
    failure. One whole segment of the chosen label is evidence of commit; an
    arbitrary substring ("a" of "Canada") is not, so segments are compared
    whole.
    """
    wanted = _fold(expected)
    if not wanted:
        return False
    segments = {piece for piece in re.split(r"[\s,;/()|]+", wanted) if len(piece) >= 2}
    return any(_fold(item) in segments for item in observed if _fold(item))


def _accessible_text(value: Any) -> str:
    """Text as an assistive technology would announce it.

    Decorative characters — emoji, variation selectors, zero-width joiners —
    carry no accessible name, so libraries drop them when they announce a
    selection: react-select announces "option Written in TypeScript, selected."
    for an option labelled "\N{HAMMER AND WRENCH} Written in TypeScript".
    Comparing raw label text against an announcement therefore fails on
    decoration alone.

    This drops exactly the Unicode categories that are decoration (So/Sk
    symbols, Cf format characters) and keeps every letter, digit and mark, so
    it is not a fuzzy match: the words still have to be identical.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    kept = [
        char for char in text
        # So/Sk: symbols (emoji, modifiers). Cf: format characters (ZWJ).
        # U+FE00-FE0F are variation selectors and are category Mn, which is
        # otherwise meaningful (accents), so exclude them by codepoint.
        if unicodedata.category(char) not in {"So", "Sk", "Cf"}
        and not (0xFE00 <= ord(char) <= 0xFE0F)
    ]
    return " ".join("".join(kept).split()).casefold()


async def _announced_selection(page: Any, selector: str, value: str) -> str | None:
    """Did the control's own live region announce committing *value*?

    Scoped to a live region inside the container that owns exactly this one
    control, so an unrelated page announcement can never stand in as evidence.
    """
    try:
        matched = await _safe_call(
            _first(page.locator(selector)), "evaluate",
            r"""(el, wanted) => {
              // `wanted` arrives already reduced to accessible text; reduce
              // each announcement the same way before comparing.
              const strip = t => String(t || '')
                .normalize('NFKC')
                .replace(/[\p{So}\p{Sk}\p{Cf}\uFE00-\uFE0F]/gu, '')
                .replace(/\s+/g, ' ')
                .trim()
                .toLowerCase();
              const target = wanted;
              if (!target) return false;
              // Whole-phrase containment: "Ten" must not match "Tenth".
              const holds = (haystack) => {
                let from = 0;
                for (;;) {
                  const at = haystack.indexOf(target, from);
                  if (at < 0) return false;
                  const before = at === 0 ? '' : haystack[at - 1];
                  const after = haystack[at + target.length] || '';
                  if (!/[\p{L}\p{N}]/u.test(before) && !/[\p{L}\p{N}]/u.test(after))
                    return true;
                  from = at + 1;
                }
              };
              for (let node = el.parentElement, depth = 0; node && depth < 6;
                   node = node.parentElement, depth++) {
                if (node.matches('form, body, html')) break;
                const controls = node.querySelectorAll('[role="combobox"], select');
                if (controls.length !== 1) break;
                const owner = controls[0];
                if (!(owner === el || owner.contains(el) || el.contains(owner))) break;
                for (const live of node.querySelectorAll('[aria-live]')) {
                  const said = strip(live.textContent);
                  if (holds(said) && /\bselected\b/.test(said)) return true;
                }
              }
              return false;
            }""",
            _accessible_text(value), default=False, read_only=True,
        )
    except Exception:
        return None
    return _norm(value) if matched else None


async def close_composite_popup(page: Any, selector: str, candidate: dict[str, Any]) -> None:
    """Best-effort cleanup after a verified composite selection.

    Multi-selects commonly stay open after one chip is added. Close them so the
    next high-level form action does not inherit an open listbox/keyboard focus
    state. Never do this for native ``select`` elements.
    """
    if _fold(candidate.get("tag")) == "select":
        return
    try:
        locator = _first(page.locator(selector))
    except Exception:
        return
    expanded = _fold(await _safe_call(locator, "get_attribute", "aria-expanded", default=""))
    if expanded != "true":
        # A role-less typeahead can reopen its bare list after the commit
        # (jQuery UI searches the chosen text again), and the open list then
        # covers the next field. Leave the field, as a person would; Escape
        # would make jQuery UI restore the typed search term.
        # Blur even when the list looks closed: the typed query scheduled a
        # debounced search that fires after the commit and reopens it over
        # the next field. Leaving the field cancels it (jQuery UI clears its
        # search timer on blur).
        claimed = await _safe_call(page, "evaluate", """() =>
          document.querySelectorAll('[data-odysseus-plain-popup]').length > 0""",
            default=False, read_only=True)
        if claimed is True:
            await _safe_call(locator, "evaluate", "el => el.blur()", default=None)
        return
    press = getattr(locator, "press", None)
    if callable(press):
        try:
            await press("Escape", timeout=1200)
        except Exception:
            pass


async def verify_selection_commit(
    page: Any,
    *,
    selector: str,
    candidate: dict[str, Any],
    value: str,
    timeout_ms: int = 3000,
    close_popup: bool = True,
) -> dict[str, Any]:
    """Prove a selection from live control-local state without model JS fallback."""
    from ascended_browser._app.browser_execution import action_stage
    action_stage("verify")
    strategy = "control_local_readback"
    try:
        observed = await _wait_for_value(page, selector, candidate, _norm(value), timeout_ms)
    except _PartialDisplay as partial:
        # Name the weaker evidence rather than claiming a full readback: the
        # control committed, and what it displays is derived from the label.
        observed, strategy = partial.observed, "control_displays_option_part"
    if close_popup:
        await close_composite_popup(page, selector, candidate)
    return {
        "strategy": strategy,
        "value": _norm(value),
        "observed": observed,
        "verified": True,
    }


async def list_options_semantic(
    page: Any,
    *,
    selector: str,
    candidate: dict[str, Any],
    query: str | None = None,
    timeout_ms: int = 8000,
) -> dict[str, Any]:
    """Open a control and report its visible options without committing one.

    Guessing an exact option label and learning from the failure costs a round
    trip per guess, and on a filtered combobox the guess may never converge.
    Reading first is the reliable order. This never selects: it is safe on a
    control whose value must not change yet.
    """
    if not selector:
        raise SemanticControlError("select selector is required")
    started = time.monotonic()
    try:
        async with browser_deadline(timeout_ms / 1000):
            locator = _first(page.locator(selector))
            if await _locator_tag(locator, candidate) == "select":
                # Native options live on the control itself; nothing to open.
                # Read them from the element, not by visibility: Playwright
                # reports every <option> inside a closed <select> as hidden, so
                # a visibility filter returned no options for any native select.
                raw = await _safe_call(
                    locator, "evaluate",
                    "el => Array.from(el.options || []).map(o => (o.textContent || '').trim())",
                    default=[], read_only=True,
                )
                options = list(dict.fromkeys(
                    _norm(text)[:80] for text in (raw if isinstance(raw, list) else []) if _norm(text)
                ))[:40]
                popup_strategy = "native"
            else:
                candidate = await _scope_to_fresh_lists(page, locator, candidate)
                await _open_control(
                    locator, timeout_ms=timeout_ms,
                    page=page if candidate.get("_fresh_lists_only") else None,
                )
                live_controls = _norm(await _safe_call(
                    locator, "get_attribute", "aria-controls", default="", read_only=True,
                ))
                if live_controls:
                    candidate = {**candidate, "aria_controls": live_controls}
                if query:
                    await _filter_with_query(locator, query, timeout_ms=timeout_ms)
                # Suggestions often arrive after the typing that asked for
                # them (Google Places): wait for the first, as select does.
                await _wait_for_any_option(page, candidate, timeout_ms=min(2500, timeout_ms // 3))
                scopes, popup_strategy = await _popup_scopes(page, candidate)
                # The popup stays open: the select that follows reuses it
                # (closing raced Ant Design's reopen animation, and the commit
                # click timed out).
                options = await _visible_option_texts(scopes, limit=_LIST_OPTIONS_LIMIT)
    except TimeoutError as exc:
        raise SemanticControlError(
            "the control's options could not be read before its operation deadline"
        ) from exc
    finally:
        await _safe_call(page, "evaluate", _CLEAR_PREOPEN_SCRIPT, default=None)
    return {
        "options": options,
        "committed": False,
        "popup_strategy": str(popup_strategy or ""),
        "query": query or "",
        "truncated": len(options) >= (40 if popup_strategy == "native" else _LIST_OPTIONS_LIMIT),
        "timing_ms": {"total": round((time.monotonic() - started) * 1000, 3)},
    }


async def select_option_semantic(
    page: Any,
    *,
    selector: str,
    candidate: dict[str, Any],
    value: str,
    query: str | None = None,
    timeout_ms: int = 8000,
    readback_selector: str | None = None,
) -> dict[str, Any]:
    """All discovery, dispatch and readback share the same deadline.

    ``readback_selector`` is the control's stable author locator (its id or
    name). A framework that re-renders the control on commit (Workday does)
    leaves the observed-node selector matching nothing, and the read-back then
    saw no value at all.
    """
    token = _FIELD_ITEMS_BEFORE.set(None)
    try:
        async with browser_deadline(timeout_ms / 1000):
            return await _select_option_semantic(
                page, selector=selector, candidate=candidate, value=value, query=query,
                timeout_ms=timeout_ms, readback_selector=readback_selector,
            )
    except TimeoutError as exc:
        raise SemanticControlError("selection did not verify before its operation deadline; effect may be uncertain") from exc
    finally:
        _FIELD_ITEMS_BEFORE.reset(token)
        # The marker belongs to this operation only; left behind, it hid a
        # field's chips from every later read-back on the page.
        await _safe_call(page, "evaluate", _CLEAR_POPUP_MARK_SCRIPT, default=None)
        await _safe_call(page, "evaluate", _CLEAR_PREOPEN_SCRIPT, default=None)


async def _select_option_semantic(
    page: Any,
    *,
    selector: str,
    candidate: dict[str, Any],
    value: str,
    query: str | None = None,
    timeout_ms: int = 8000,
    readback_selector: str | None = None,
) -> dict[str, Any]:
    """Select ``value`` from a native or composite control and prove the commit."""
    value = _norm(value)
    if not value:
        raise SemanticControlError("select value is required")
    if not selector:
        raise SemanticControlError("select selector is required")

    started = time.monotonic()
    timing: dict[str, float] = {}
    locator = _first(page.locator(selector))
    tag = await _locator_tag(locator, candidate)
    if tag == "select":
        try:
            await locator.select_option(label=value, timeout=timeout_ms)
        except Exception:
            await locator.select_option(value=value, timeout=timeout_ms)
        timing["dispatch"] = round((time.monotonic() - started) * 1000, 3)
        evidence = await verify_selection_commit(
            page, selector=selector, candidate=candidate, value=value, timeout_ms=timeout_ms,
        )
        return {
            **evidence,
            "strategy": "native_select",
            "popup_strategy": "native",
            "timing_ms": {**timing, "total": round((time.monotonic() - started) * 1000, 3)},
        }

    editable = tag == "input" or _fold(candidate.get("aria_autocomplete")) in {"list", "both", "inline"}
    expanded = _fold(await _safe_call(locator, "get_attribute", "aria-expanded", default=""))
    # Live state only: the observation's copy of aria-expanded can be stale.
    if expanded != "true" and _fold(value) in {_fold(item) for item in await _field_item_texts(locator)}:
        # Already chosen: with the popup closed, an item in the field's own
        # container is a chip. Opening and clicking would find that chip as
        # "the option" (live, Workday: a saved choice from an earlier visit)
        # and toggle or re-add it.
        return {
            "strategy": "already_selected", "popup_strategy": "none",
            "value": value, "observed": [value], "verified": True,
            "timing_ms": {"total": round((time.monotonic() - started) * 1000, 3)},
        }
    if not expanded:
        expanded = _fold(candidate.get("aria_expanded"))
    if expanded != "true":
        candidate = await _scope_to_fresh_lists(page, locator, candidate)
        # Try to open regardless of whether the control also accepts typing:
        # react-select's single combobox is an input that must still be opened.
        await _open_control(
            locator, timeout_ms=timeout_ms,
            page=page if candidate.get("_fresh_lists_only") else None,
        )
    timing["open"] = round((time.monotonic() - started) * 1000, 3)

    # ARIA ownership is often created only while a composite popup is open.
    # Refresh it from the live control so option discovery stays inside the
    # control's actual popup instead of searching unrelated page content.
    live_controls = _norm(await _safe_call(
        locator, "get_attribute", "aria-controls", default="", read_only=True,
    ))
    if live_controls and live_controls != _norm(candidate.get("aria_controls")):
        candidate = {**candidate, "aria_controls": live_controls}

    text_before = ""
    if editable:
        text_before = _norm(await _safe_call(locator, "input_value", default="", read_only=True))
        await _filter_with_query(
            locator, query if query is not None else value, timeout_ms=timeout_ms,
        )
    timing["filter"] = round((time.monotonic() - started) * 1000, 3)

    option, popup, popup_strategy = await _wait_for_option(page, candidate, value, timeout_ms=timeout_ms)
    if option is None and editable and popup_strategy != "ambiguous_popup" and await _safe_call(
        locator, "evaluate", _ENTER_RUNS_SEARCH, default=False, read_only=True,
    ) is True:
        # The field declares that Enter runs its search (enterkeyhint=search)
        # and no form owns it, so Enter cannot submit anything. Search boxes
        # that fetch results only on Enter (Workday's) showed nothing to pick.
        await _safe_call(locator, "press", "Enter", timeout=1000)
        await asyncio.sleep(0.3)
        if not _norm(await _safe_call(locator, "input_value", default="", read_only=True)) and _fold(value) in {
            _fold(item) for item in await _field_item_texts(locator)
        }:
            # Enter committed the match itself (Workday): the filter text was
            # consumed and the value is now one of the field's items. Looking
            # for an option to click would find that new item and toggle it.
            return {
                "strategy": "committed_on_enter", "popup_strategy": "none",
                "value": value, "observed": [value], "verified": True,
                "timing_ms": {**timing, "total": round((time.monotonic() - started) * 1000, 3)},
            }
        option, popup, popup_strategy = await _wait_for_option(page, candidate, value, timeout_ms=timeout_ms)
    if option is None and popup_strategy not in {"ambiguous_popup"}:
        option, popup, popup_strategy = await _scroll_virtualized_popup_for_option(page, candidate, value, popup)
    observed: list[str] = []
    if option is None:
        # Capture what the popup actually offers before unwinding: it closes
        # with this failure, and a caller told only what was missing can do
        # nothing but guess the same way again. Read it while the filter text
        # is still in the field — clearing it first restores the unfiltered
        # list, which is how a 250-country popup answered with its A's.
        scopes, _scope_strategy = await _popup_scopes(page, candidate)
        observed, still_loading = await _settled_option_texts(scopes)
    if option is None and editable and not text_before:
        # Leave the field as found: the filter text is ours, not a value, and
        # a later read-back must not mistake it for a selection.
        await _safe_call(locator, "fill", "", timeout=1000)
    if option is None:
        label = _norm(candidate.get("label")) or _norm(candidate.get("ref")) or "this control"
        near = _near_matches(observed, value)
        detail = (
            # The label a list decorates ("Canada +1") is the whole answer, and
            # the head of a 250-country list is not: name it so the retry is one
            # call. Selection itself stays exact — nothing is chosen by guess.
            f"did you mean {near[0]!r}? Retry with that exact label"
            if len(near) == 1 else
            f"these visible options all read like {value!r}: {near[:12]}. "
            f"Retry with one of them, exactly as written"
            if len(near) > 1 else
            f"the list was still loading when this gave up; what it had so far: "
            f"{observed}. Try again, or filter with `query` so the page fetches less"
            if still_loading and observed else
            "the list was still loading and had nothing to show yet; try again, or "
            "filter with `query` so the page fetches less"
            if still_loading else
            f"visible options were: {observed}" if observed
            else "no options were visible in the popup — the control may not have opened, "
                 "the list may still be loading, or this may be the wrong control"
        )
        # One named label is already the instruction; only a bare list needs
        # telling what to do with it.
        next_step = "" if near else (
            " Choose an exact label from that list, or use `query` to filter "
            "further before selecting." if observed else ""
        )
        raise SemanticOptionNotFound(
            f"missing or ambiguous option: no unique visible exact match for "
            f"{value!r} on {label!r}; "
            f"{detail}.{next_step} (popup_strategy={popup_strategy})",
            observed_options=observed,
            popup_strategy=str(popup_strategy or ""),
            for_ref=str(candidate.get("ref") or ""),
        )
    timing["option_ready"] = round((time.monotonic() - started) * 1000, 3)

    # Cap the commit click. An option that is disabled or never becomes stable
    # otherwise retries until the deadline, leaving nothing for verification —
    # the failure then reads as "did not verify in time" rather than naming the
    # option that would not take a click.
    click_budget = max(1000, min(int(timeout_ms), int(timeout_ms * 0.6)))
    # What the caller named may be part of the label ("Canada" of "Canada +1").
    # Everything downstream — the readback, the announcement, the receipt — has
    # to be about the option that is actually being clicked, not the shorthand.
    chosen = _norm(await _safe_call(option, "inner_text", default="", read_only=True)) or value
    await _safe_call(option, "evaluate", _MARK_POPUP_SCRIPT, default=None)
    _FIELD_ITEMS_BEFORE.set(Counter(await _field_item_texts(locator)))
    try:
        await _click_option(option, click_budget, page=page)
    except SemanticControlError:
        raise
    except Exception as first:
        # Widgets that rebuild their list on focus or input can replace the
        # option between resolving and clicking it. Resolve it again once from
        # the live popup and give the fresh node the rest of the budget.
        retry, _popup, _strategy = await _find_option(page, candidate, value)
        remaining = int(timeout_ms - (time.monotonic() - started) * 1000)
        try:
            if retry is None or remaining < 500:
                raise first
            await _click_option(retry, min(click_budget, remaining), page=page)
        except Exception as exc:
            if editable and not text_before:
                # The filter text is ours, not a value: leaving "pyt" in the
                # field reads as a half-made selection to the page and to the
                # next observation.
                await _safe_call(locator, "fill", "", timeout=1000)
            label = _norm(candidate.get("label")) or _norm(candidate.get("ref")) or "this control"
            raise SemanticControlError(
                f"option {chosen!r} on {label!r} was found but did not accept a click "
                f"({_click_failure_reason(exc)}); nothing was selected and the field "
                f"was left as found. Re-observe before retrying."
            ) from exc
    timing["dispatch"] = round((time.monotonic() - started) * 1000, 3)
    # Read the live region now: an announcement is transient and is usually
    # cleared by the time a readback wait gives up.
    if readback_selector and readback_selector != selector and not int(
        await _safe_call(page.locator(selector), "count", default=0, read_only=True) or 0
    ):
        selector = readback_selector  # the observed node was replaced on commit
    announced = await _announced_selection(page, selector, chosen)
    try:
        evidence = await verify_selection_commit(
            page, selector=selector, candidate=candidate, value=chosen, timeout_ms=timeout_ms,
        )
    except SemanticControlError as failure:
        submenu = await _opened_submenu(page, candidate, chosen)
        if submenu:
            # A category, not a choice (Workday's "How did you hear about us?"
            # opens Job Board, Social Media, ... as submenus). Reporting
            # "observed []" sent the model to keyboard arrows three times.
            label = _norm(candidate.get("label")) or _norm(candidate.get("ref")) or "this control"
            # Nothing was chosen; leave the field as found, not mid-menu. The
            # category click changed no value, so this is not an uncertain
            # effect to reconcile: the next select picks the leaf.
            await close_composite_popup(page, selector, candidate)
            from ascended_browser._app.browser_execution import dispatch_withdrawn

            dispatch_withdrawn("a category opened its submenu; no option was selected")
            raise SemanticOptionNotFound(
                f"{chosen!r} on {label!r} is a category: choosing it opened {len(submenu)} more "
                f"options instead of selecting it: {submenu[:15]}. Select one of those exactly; "
                f"`query` filters them.",
                observed_options=submenu,
                popup_strategy="submenu",
                for_ref=str(candidate.get("ref") or ""),
            ) from failure
        if not announced:
            raise
        # Some controls deliberately render no value (react-select with
        # controlShouldRenderValue disabled, menu-style pickers). Their only
        # statement of the commit is the live region a screen reader hears —
        # a real contract, not a guess. Keep it as its own evidence class so a
        # rendered-value readback is never confused with an announcement.
        await close_composite_popup(page, selector, candidate)
        evidence = {
            "strategy": "aria_live_announcement",
            "value": _norm(chosen),
            "observed": [announced],
            "verified": True,
        }
    return {
        **evidence,
        # How the option was found is not how the commit was proven. Keeping
        # both means a caller can see that the proof was a derived display
        # value rather than the label itself.
        "readback": str(evidence.get("strategy") or ""),
        "strategy": "aria_option",
        "popup_strategy": popup_strategy,
        # Say which label was committed when it is not the one that was asked
        # for, so a caller reading the receipt never has to assume.
        **({"requested_option": _norm(value)} if _fold(chosen) != _fold(value) else {}),
        "timing_ms": {**timing, "total": round((time.monotonic() - started) * 1000, 3)},
    }
