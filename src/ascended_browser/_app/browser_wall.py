"""``wait_for_bot_wall``: wait out a bot wall, then attempt a solve.

A bot wall is a page state the browser cannot mechanically pass: a Cloudflare
"Just a moment…" check that usually clears by itself, or a CAPTCHA that needs a
human or a paid solver. This module owns the *waiting* policy and the structured
verdict the model turns into an action; the clearance work lives behind
``attempt_bot_wall_solve``: a curl_cffi sidecar impersonating Camoufox's own
Firefox TLS fingerprint harvests ``cf_clearance`` for Cloudflare (with
ai-cloudscraper as the legacy JS-challenge fallback). Embedded widgets — a
Turnstile, reCAPTCHA v2 or hCaptcha checkbox inside an ordinary form — have no
cookie to harvest: they get a wait for auto-pass, then a real press of their
checkbox (``src/browser_challenge_widget``). Image/puzzle challenges are never
solved here; a 2captcha seam is reserved for them.
"""

from __future__ import annotations

import asyncio
import re
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

DEFAULT_MAX_WAIT_MS = 60_000
DEFAULT_POLL_INTERVAL_MS = 5_000
_MAX_WAIT_MS = 600_000
_MIN_POLL_MS = 1_000
# Embedded widgets: Turnstile's managed/non-interactive modes usually issue a
# token within a few seconds without input; after a press a clean browser gets
# one in a few more. Two presses at most: a widget that still withholds a token
# is scoring the browser, and more presses only make that worse.
WIDGET_AUTO_PASS_S = 6.0
WIDGET_AFTER_PRESS_S = 15.0
WIDGET_POLL_S = 1.0
WIDGET_MAX_PRESSES = 2


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(value or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _verdict(
    status: str,
    *,
    page: dict[str, Any] | None = None,
    blocker: dict[str, Any] | None = None,
    state: str = "",
    wall_type: str = "",
    waited_ms: int = 0,
    solver: dict[str, Any] | None = None,
    message: str = "",
    recommendation: str = "continue",
) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "status": status,
        "wall_type": wall_type,
        "state": state,
        "waited_ms": waited_ms,
        "solver": solver or {"attempted": False, "method": "none", "detail": ""},
        "recommendation": recommendation,
        "message": message,
        "exit_code": 0,
    }
    if page:
        verdict["page"] = page
    if blocker:
        verdict["blocker"] = blocker
    return verdict


async def attempt_bot_wall_solve(
    manager: Any,
    owner: str,
    session_id: str,
    tab_id: str,
    blocker: dict[str, Any] | None,
    *,
    url: str = "",
    actor: str = "parent",
    actor_id: str = "parent",
) -> dict[str, Any]:
    """Solve a bot wall, returning ``{"solved": bool, "method": str, "detail": str}``.

    Cloudflare auto-verifying walls are cleared with the curl_cffi sidecar
    (harvest ``cf_clearance`` in its own TLS-matched session, inject into the
    Camoufox tab, reload). A checkbox widget — embedded in the page or drawn on
    the interstitial itself — is waited on and pressed. Image/puzzle captchas
    are reserved for a 2captcha integration that is not wired yet. Every
    failure is a structured ``solved=False`` result, never an exception: the
    caller keeps its hand-off fallback.
    """
    from ascended_browser.runtime.settings import get_setting

    state = str((blocker or {}).get("state") or "")
    if state == "user_required":
        widget = await solve_embedded_widget(
            manager, owner, session_id, tab_id, actor=actor, actor_id=actor_id,
        )
        if widget is not None:
            return widget
        return {
            "solved": False,
            "method": "captcha",
            "detail": "no captcha solver configured",
        }
    if not bool(get_setting("browser_work_challenge_auto_clear", True)):
        cleared = {
            "solved": False,
            "method": "cloudflare-clearance",
            "detail": "auto-clear disabled",
        }
    elif manager is None:
        return {
            "solved": False,
            "method": "cloudflare-clearance",
            "detail": "no browser workspace",
        }
    else:
        cleared = await _solve_cloudflare(
            manager,
            owner,
            session_id,
            tab_id,
            url,
            actor=actor,
            actor_id=actor_id,
        )
    if cleared.get("solved"):
        return cleared
    # A managed interstitial that outlived the wait is showing its Turnstile
    # checkbox; the press is the same as for an embedded widget.
    widget = await solve_embedded_widget(
        manager, owner, session_id, tab_id, actor=actor, actor_id=actor_id,
    )
    if widget is not None and (widget.get("solved") or widget.get("pressed")):
        return widget
    return cleared


async def solve_embedded_widget(
    manager: Any,
    owner: str,
    session_id: str,
    tab_id: str,
    *,
    actor: str = "parent",
    actor_id: str = "parent",
) -> dict[str, Any] | None:
    """Wait on, then press, an embedded checkbox widget until it issues a token.

    ``None`` means the page has no pending widget (or the workspace cannot read
    one), so the caller's own verdict stands. Otherwise the result has the
    ``attempt_bot_wall_solve`` shape plus ``provider`` and ``pressed``. An
    image/puzzle popup ends the attempt at once: it needs a human.
    """
    from ascended_browser.runtime.settings import get_setting

    reader = getattr(manager, "challenge_widget_state", None)
    presser = getattr(manager, "press_challenge_widget", None)
    if not callable(reader):
        return None
    kwargs = (
        {"actor": actor, "actor_id": actor_id}
        if (actor, actor_id) != ("parent", "parent")
        else {}
    )

    async def read() -> dict[str, Any]:
        try:
            return await reader(owner, session_id, tab_id, **kwargs) or {}
        except Exception:  # noqa: BLE001 - an unreadable page has no widget to solve
            return {}

    async def settle(budget_s: float) -> dict[str, Any]:
        started = time.monotonic()
        current = await read()
        while current.get("pending") and not current.get("challenge"):
            remaining = budget_s - (time.monotonic() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(WIDGET_POLL_S, remaining))
            current = await read()
        return current

    def result(solved: bool, detail: str, provider: str, presses: int) -> dict[str, Any]:
        return {
            "solved": solved,
            "method": "embedded-widget",
            "detail": detail,
            "provider": provider,
            "pressed": presses > 0,
            "presses": presses,
        }

    state = await read()
    provider = str(state.get("pending") or state.get("challenge") or "")
    if state.get("challenge"):
        return result(False, f"{provider} image challenge needs a human", provider, 0)
    if not state.get("pending"):
        return None

    state = await settle(WIDGET_AUTO_PASS_S)
    if not state.get("pending") and not state.get("challenge"):
        return result(True, f"{provider} widget passed without input", provider, 0)
    presses = 0
    allow_press = callable(presser) and bool(
        get_setting("browser_work_challenge_widget_press", True)
    )
    while allow_press and presses < WIDGET_MAX_PRESSES and not state.get("challenge"):
        try:
            press = await presser(owner, session_id, tab_id, **kwargs) or {}
        except Exception as exc:  # noqa: BLE001 - a refused press keeps the hand-off
            press = {"pressed": False, "detail": f"press failed: {type(exc).__name__}"}
        if not press.get("pressed"):
            return result(False, str(press.get("detail") or "press failed"), provider, presses)
        presses += 1
        state = await settle(WIDGET_AFTER_PRESS_S)
        if not state.get("pending") and not state.get("challenge"):
            return result(True, f"{provider} widget issued a token after a press", provider, presses)
    if state.get("challenge"):
        return result(False, f"{provider} escalated to an image challenge; it needs a human", provider, presses)
    if not allow_press:
        return result(False, f"{provider} widget did not pass on its own; pressing is disabled", provider, 0)
    return result(False, f"{provider} widget withheld a token after {presses} press(es)", provider, presses)


async def _solve_cloudflare(
    manager: Any,
    owner: str,
    session_id: str,
    tab_id: str,
    url: str,
    *,
    actor: str = "parent",
    actor_id: str = "parent",
) -> dict[str, Any]:
    from ascended_browser.runtime.settings import get_setting

    budget = float(get_setting("browser_work_challenge_budget_s", 45) or 45)
    # cf_clearance is IP-bound, so the harvest must exit where the browser
    # exits. The browser's own launch proxy is the source of truth; the
    # challenge_proxy setting remains only as an explicit override for the
    # exotic case where the sidecar must use a different exit than the tab.
    proxy = str(get_setting("browser_work_challenge_proxy", "") or "")
    if not proxy:
        proxy_lookup = getattr(manager, "browser_proxy", None)
        if callable(proxy_lookup):
            try:
                proxy = str(proxy_lookup(owner) or "")
            except Exception:  # noqa: BLE001 - a direct harvest beats no harvest
                proxy = ""
    injector = getattr(manager, "inject_clearance_cookies", None)
    if not callable(injector) or not url:
        return {
            "solved": False,
            "method": "cloudflare-clearance",
            "detail": "clearance injection unavailable",
        }
    user_agent = await _camoufox_user_agent(manager, owner, session_id, tab_id)
    try:
        cookies = await asyncio.wait_for(
            asyncio.to_thread(_harvest_cloudflare_cookies, url, proxy, user_agent),
            timeout=budget,
        )
    except Exception as exc:  # noqa: BLE001 - any harvest failure is a soft miss
        return {
            "solved": False,
            "method": "cloudflare-clearance",
            "detail": f"harvest failed: {type(exc).__name__}",
        }
    if not cookies:
        return {
            "solved": False,
            "method": "cloudflare-clearance",
            "detail": "no clearance cookies returned",
        }
    try:
        inject_kwargs = {"url": url}
        if (actor, actor_id) != ("parent", "parent"):
            inject_kwargs.update(actor=actor, actor_id=actor_id)
        injected = await injector(
            owner, session_id, tab_id, cookies, **inject_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "solved": False,
            "method": "cloudflare-clearance",
            "detail": f"injection failed: {type(exc).__name__}",
        }
    return {
        "solved": True,
        "method": "cloudflare-clearance",
        "detail": f"injected {injected.get('injected', 0)} cookie(s)",
        "cookies": sorted(cookie.get("name", "") for cookie in cookies),
    }


async def _camoufox_user_agent(manager: Any, owner: str, session_id: str, tab_id: str) -> str:
    """Read the live tab's ``navigator.userAgent``; empty when unavailable.

    Best effort: a failed read means cloudscraper solves with its default UA,
    which just makes the transplanted cookie a little less likely to be accepted.
    """
    try:
        page = await manager.page_for(owner, session_id, tab_id)
        ua = await asyncio.wait_for(page.evaluate("() => navigator.userAgent"), timeout=5)
        return str(ua or "")
    except Exception:  # noqa: BLE001 - no page means no UA to match against
        return ""


@lru_cache(maxsize=1)
def _curl_impersonation_target() -> str:
    """The curl_cffi Firefox impersonation target matching the installed Camoufox.

    Camoufox's TLS ClientHello is whatever its bundled Firefox/NSS emits, so
    the sidecar must impersonate the same Firefox major — a Python ``requests``
    hello gets the harvest rejected at the TLS layer on hardened walls before
    the challenge page is even served, and a mismatched browser hello makes the
    transplanted clearance look replayed. Exact major match when curl_cffi
    ships one, else the nearest lower target (Firefox's hello barely churns
    between adjacent versions); failing everything, the latest Firefox target
    is still closer than no impersonation. Empty string means no Firefox
    target exists at all and the session runs unimpersonated.
    """
    from camoufox.pkgman import installed_verstr
    from curl_cffi.requests import BrowserType

    try:
        major = int(str(installed_verstr()).split(".", 1)[0])
    except (ValueError, IndexError):
        major = 0
    targets = sorted(
        int(match.group(1))
        for name in dir(BrowserType)
        if (match := re.fullmatch(r"firefox(\d+)", name))
    )
    if not targets:
        return ""
    if major in targets:
        return f"firefox{major}"
    lower = [target for target in targets if target < major]
    return f"firefox{lower[-1] if lower else targets[-1]}"


def _wanted_clearance_cookies(jar: Any, domain: str) -> list[dict[str, Any]]:
    wanted = {"cf_clearance", "cf_chl_2", "cf_chl_prog", "cf_chl_rc_ni", "cf_turnstile", "__cf_bm"}
    cookies: list[dict[str, Any]] = []
    for cookie in jar:
        if cookie.name in wanted and cookie.value:
            cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or domain,
                "path": cookie.path or "/",
                "expires": int(cookie.expires) if cookie.expires else None,
            })
    return cookies


def _harvest_with_curl(url: str, proxy: str, user_agent: str) -> list[dict[str, Any]]:
    """Harvest clearance with the same TLS/HTTP2 fingerprint as Camoufox."""
    from curl_cffi import requests as curl_requests

    session_kwargs: dict[str, Any] = {}
    target = _curl_impersonation_target()
    if target:
        session_kwargs["impersonate"] = target
    if proxy:
        session_kwargs["proxy"] = proxy
    headers = {"User-Agent": user_agent} if user_agent else None
    with curl_requests.Session(**session_kwargs) as session:
        resp = session.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return _wanted_clearance_cookies(session.cookies.jar, urlparse(str(resp.url)).netloc)


def _harvest_with_cloudscraper(url: str, proxy: str, user_agent: str) -> list[dict[str, Any]]:
    """Legacy fallback: solve the old JS challenge via the ``nodejs`` interpreter.

    The interpreter shells out to a local ``node`` binary, which is the only
    interpreter still working on this Python (``js2py`` is broken on 3.14).
    cloudscraper runs on plain ``requests``, so its TLS fingerprint betrays it
    on hardened walls — that path is what ``_harvest_with_curl`` now covers.
    """
    import cloudscraper

    create_kwargs: dict[str, Any] = {"interpreter": "nodejs"}
    if user_agent:
        create_kwargs["browser"] = {"custom": user_agent}
    scraper = cloudscraper.create_scraper(**create_kwargs)
    request_kwargs: dict[str, Any] = {"timeout": 30}
    if proxy:
        request_kwargs["proxies"] = {"http": proxy, "https": proxy}
    resp = scraper.get(url, **request_kwargs)
    resp.raise_for_status()
    return _wanted_clearance_cookies(scraper.cookies, urlparse(resp.url).netloc)


def _harvest_cloudflare_cookies(
    url: str, proxy: str = "", user_agent: str = "",
) -> list[dict[str, Any]]:
    """Solve the Cloudflare challenge out-of-band and return clearance cookies.

    Runs in a worker thread. curl_cffi goes first, impersonating the Camoufox
    build's Firefox major so the harvest presents the same TLS fingerprint the
    transplanted cookies will later arrive with; ``user_agent`` is the Camoufox
    tab's own ``navigator.userAgent`` because Cloudflare also binds
    ``cf_clearance`` to the UA that passed. cloudscraper is the fallback for
    legacy JS challenges curl_cffi cannot execute. Returns a list of
    ``{name, value, domain, path, expires}`` for the cookies Camoufox needs.
    """
    try:
        cookies = _harvest_with_curl(url, proxy, user_agent)
    except Exception:  # noqa: BLE001 - a broken curl attempt still has the fallback
        cookies = []
    if cookies:
        return cookies
    return _harvest_with_cloudscraper(url, proxy, user_agent)


async def _embedded_verdict(
    manager: Any,
    owner: str,
    session_id: str,
    tab_id: str,
    started: float,
    *,
    actor: str,
    actor_id: str,
    observe_kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """Verdict for a page that is readable but gated by an embedded widget."""
    solver = await solve_embedded_widget(
        manager, owner, session_id, tab_id, actor=actor, actor_id=actor_id,
    )
    if solver is None:
        return None
    solver = {**solver, "attempted": True}
    provider = str(solver.get("provider") or "")
    waited = int((time.monotonic() - started) * 1000)
    if solver.get("solved"):
        return _verdict(
            "solved",
            page=await manager.observe(owner, session_id, tab_id, **observe_kwargs),
            state="user_required",
            wall_type="embedded_widget",
            waited_ms=waited,
            solver=solver,
            message=(
                f"The embedded {provider} check passed; its token is in the form. "
                "Continue with the form."
            ),
        )
    return _verdict(
        "failed",
        blocker={
            "type": "challenge",
            "state": "user_required",
            "reason": str(solver.get("detail") or "embedded check needs a human"),
            "evidence": provider,
        },
        state="user_required",
        wall_type="embedded_widget",
        waited_ms=waited,
        solver=solver,
        message=(
            f"The page is readable, but its embedded {provider} check did not pass "
            "and a submit that needs it will be rejected. If that submit is required "
            "for the user's goal, stop and tell them to complete the check (or hand "
            "off if you are a browser sub-agent); otherwise continue."
        ),
        recommendation="notify_user_or_continue",
    )


async def wait_for_bot_wall(
    manager: Any,
    owner: str,
    session_id: str,
    tab_id: str,
    *,
    max_wait_ms: Any = DEFAULT_MAX_WAIT_MS,
    poll_interval_ms: Any = DEFAULT_POLL_INTERVAL_MS,
    apply_solver: bool = True,
    actor: str = "parent",
    actor_id: str = "parent",
) -> dict[str, Any]:
    """Poll the tab while a wall auto-verifies, then attempt a solve.

    The verdict is structured so the model decides what happens next: continue,
    tell the user, or stop because the page is a hard dependency. This function
    never writes to the page beyond whatever ``attempt_bot_wall_solve`` does.
    """
    max_wait_ms = _clamp(max_wait_ms, DEFAULT_MAX_WAIT_MS, 1_000, _MAX_WAIT_MS)
    poll_interval_ms = _clamp(poll_interval_ms, DEFAULT_POLL_INTERVAL_MS, _MIN_POLL_MS, 60_000)

    started = time.monotonic()
    last: dict[str, Any] | None = None
    state = ""
    wall_type = ""
    observe_kwargs = (
        {"actor": actor, "actor_id": actor_id}
        if (actor, actor_id) != ("parent", "parent")
        else {}
    )

    while True:
        remaining_ms = max_wait_ms - int((time.monotonic() - started) * 1000)
        if remaining_ms <= 0:
            break
        last = await manager.observe(owner, session_id, tab_id, **observe_kwargs)
        if not last.get("blocked"):
            if apply_solver:
                embedded = await _embedded_verdict(
                    manager, owner, session_id, tab_id, started,
                    actor=actor, actor_id=actor_id, observe_kwargs=observe_kwargs,
                )
                if embedded is not None:
                    return embedded
            waited = int((time.monotonic() - started) * 1000)
            return _verdict(
                "cleared",
                page=last,
                waited_ms=waited,
                message="The bot wall cleared on its own.",
            )
        blocker = last.get("blocker") or {}
        state = str(blocker.get("state") or "")
        wall_type = "captcha" if state == "user_required" else str(blocker.get("type") or "challenge")
        if state == "user_required":
            break
        await asyncio.sleep(min(poll_interval_ms, remaining_ms) / 1000.0)

    waited = int((time.monotonic() - started) * 1000)
    solver: dict[str, Any] = {"attempted": False, "method": "none", "detail": ""}
    if apply_solver:
        solver_kwargs: dict[str, Any] = {
            "url": str(last.get("url") or "") if last else "",
        }
        if (actor, actor_id) != ("parent", "parent"):
            solver_kwargs.update(actor=actor, actor_id=actor_id)
        solver = await attempt_bot_wall_solve(
            manager, owner, session_id, tab_id,
            last.get("blocker") if last else None,
            **solver_kwargs,
        )
        solver = {**solver, "attempted": True}
        if solver.get("solved"):
            refreshed = await manager.observe(owner, session_id, tab_id, **observe_kwargs)
            if not refreshed.get("blocked"):
                return _verdict(
                    "solved",
                    page=refreshed,
                    state=state,
                    wall_type=wall_type,
                    waited_ms=waited,
                    solver=solver,
                    message="Bot wall solved by the configured solver.",
                )
        last = await manager.observe(owner, session_id, tab_id, **observe_kwargs)
        if last and not last.get("blocked"):
            return _verdict(
                "cleared",
                page=last,
                state=state,
                wall_type=wall_type,
                waited_ms=waited,
                solver=solver,
                message="Bot wall cleared after the solver attempt.",
            )

    if state == "user_required":
        status = "failed"
        message = (
            "The bot wall needs a human (captcha/user verification) and could not be "
            "solved. If reaching this page is required for the user's goal, stop and "
            "tell them to solve it (or hand off if you are a browser sub-agent); "
            "otherwise continue to the next task."
        )
    else:
        status = "still_verifying"
        message = (
            "The bot wall did not clear within the wait budget. If reaching this page "
            "is required for the user's goal, stop and tell them to solve it (or hand "
            "off if you are a browser sub-agent); otherwise continue to the next task."
        )

    return _verdict(
        status,
        blocker=(last.get("blocker") if last else None),
        state=state,
        wall_type=wall_type,
        waited_ms=waited,
        solver=solver,
        message=message,
        recommendation="notify_user_or_continue",
    )
