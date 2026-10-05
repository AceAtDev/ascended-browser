"""Network-level ad and tracker blocking for any Playwright context.

Camoufox bundles uBlock Origin, but that addon is what caused the context-wide
navigation stall this runtime hit at ~7 tabs (see
scripts/browser_workspace_page_ceiling_probe.py and the recorded artifact). The
addon is therefore excluded at launch, and this module takes over its job through
Playwright request interception, which needs no extension.

Standalone by design: it takes a Playwright BrowserContext (or Page) and attaches.
It knows nothing about workspaces, owners or agents, so any browser surface in the
codebase can use it.

    blocker = AdBlocker()
    await blocker.attach(context)
    ...
    blocker.stats()  # {"blocked": 128, "allowed": 512, "bytes_saved_estimate": ...}

Blocking is deliberately conservative. An agent has to be able to *use* the page,
so a false block is worse than a missed ad: only third-party requests to known
ad/tracking hosts are dropped, plus third-party media, which is where the memory
goes. First-party requests are never blocked.
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable
from urllib.parse import urlparse

# Registrable domains whose sole purpose is ads, tracking or beaconing. Matching
# is on the registrable domain so subdomains (a.b.doubleclick.net) are covered.
DEFAULT_BLOCKED_DOMAINS: frozenset[str] = frozenset({
    # Google ads / analytics
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "google-analytics.com", "googletagmanager.com", "googletagservices.com",
    "adservice.google.com", "2mdn.net",
    # Meta / social trackers
    "facebook.net", "connect.facebook.net", "fbcdn.net/ads",
    # Major ad exchanges and SSPs
    "adnxs.com", "rubiconproject.com", "pubmatic.com", "openx.net",
    "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "casalemedia.com", "smartadserver.com", "adform.net", "sharethrough.com",
    "indexexchange.com", "33across.com", "media.net", "adsrvr.org",
    "bidswitch.net", "yieldmo.com", "teads.tv", "spotxchange.com",
    # Analytics / session recording (heavy: these record the DOM continuously)
    "hotjar.com", "fullstory.com", "mouseflow.com", "clarity.ms",
    "segment.io", "segment.com", "mixpanel.com", "amplitude.com",
    "heap.io", "quantserve.com", "scorecardresearch.com", "chartbeat.com",
    "newrelic.com", "nr-data.net", "bugsnag.com", "sentry-cdn.com",
    # Consent/tag managers that pull in everything else
    "onetrust.com", "cookielaw.org", "trustarc.com", "quantcast.com",
    # Misc trackers
    "branch.io", "appsflyer.com", "adjust.com", "kochava.com",
    "bounceexchange.com", "optimizely.com", "crazyegg.com",
})

# Path fragments that identify ad slots even on first-party hosts.
DEFAULT_BLOCKED_PATTERNS: tuple[str, ...] = (
    r"/adsbygoogle", r"/pagead/", r"/googleads", r"/ad(s|server|frame)?/",
    r"/banner(s|ad)?/", r"/prebid", r"/gpt\.js", r"/analytics\.js",
    r"/gtag/js", r"/piwik", r"/matomo", r"/beacon", r"/telemetry",
)

# Third-party resource types dropped regardless of host. This is the memory win:
# an ad-heavy article can pull tens of MB of third-party imagery and video.
DEFAULT_BLOCKED_THIRD_PARTY_TYPES: frozenset[str] = frozenset({"media", "font"})

# Rough per-request savings used only for reporting, so a caller can show
# "~40 MB not loaded" without measuring every response body.
_ESTIMATED_BYTES: dict[str, int] = {
    "media": 800_000, "image": 60_000, "font": 40_000,
    "script": 45_000, "stylesheet": 20_000, "xhr": 8_000,
    "fetch": 8_000, "other": 5_000,
}


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without a network call or a dependency."""
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Two-level public suffixes we actually meet (co.uk, com.au, com.br, ...).
    if len(parts[-1]) == 2 and parts[-2] in {"co", "com", "net", "org", "gov", "ac", "edu"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class AdBlocker:
    """Request-interception ad blocker. Attach to a context or a single page."""

    def __init__(
        self,
        *,
        blocked_domains: Iterable[str] | None = None,
        blocked_patterns: Iterable[str] | None = None,
        blocked_third_party_types: Iterable[str] | None = None,
        allow_domains: Iterable[str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.blocked_domains = frozenset(
            registrable_domain(d) for d in (blocked_domains or DEFAULT_BLOCKED_DOMAINS)
        )
        self.blocked_patterns = tuple(
            re.compile(p, re.I) for p in (blocked_patterns or DEFAULT_BLOCKED_PATTERNS)
        )
        self.blocked_third_party_types = frozenset(
            blocked_third_party_types
            if blocked_third_party_types is not None
            else DEFAULT_BLOCKED_THIRD_PARTY_TYPES
        )
        # An explicit allow always wins: a site that breaks without its own
        # analytics still has to work for the agent driving it.
        self.allow_domains = frozenset(registrable_domain(d) for d in (allow_domains or ()))
        self.enabled = enabled
        self.blocked = 0
        self.allowed = 0
        self.bytes_saved_estimate = 0
        self._blocked_by_domain: dict[str, int] = {}
        # The last aborts this blocker made, keyed by URL and the first party
        # that asked for it, with a time: a request failure that was our own
        # policy is never reported as the page's failure, and a URL blocked
        # for one page a while ago says nothing about a later real failure.
        self._recent_blocked: dict[tuple[str, str], float] = {}

    # -- decision -------------------------------------------------------

    def should_block(self, url: str, resource_type: str, document_url: str) -> bool:
        if not self.enabled:
            return False
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"}:
            return False
        host = registrable_domain(parsed.hostname or "")
        if not host or host in self.allow_domains:
            return False
        first_party = registrable_domain(urlparse(document_url or "").hostname or "")
        third_party = bool(first_party) and host != first_party
        if third_party and host in self.blocked_domains:
            return True
        if third_party and resource_type in self.blocked_third_party_types:
            return True
        # Path rules apply to third parties only. A first-party /ads/ path can be
        # the very content the agent was asked to read.
        if third_party:
            target = f"{parsed.path or ''}?{parsed.query or ''}"
            if any(pattern.search(target) for pattern in self.blocked_patterns):
                return True
        return False

    def _note(self, blocked: bool, url: str, resource_type: str, document_url: str = "") -> None:
        if blocked:
            self.blocked += 1
            self.bytes_saved_estimate += _ESTIMATED_BYTES.get(resource_type, 5_000)
            host = registrable_domain(urlparse(url or "").hostname or "")
            self._blocked_by_domain[host] = self._blocked_by_domain.get(host, 0) + 1
            first_party = registrable_domain(urlparse(document_url or "").hostname or "")
            self._recent_blocked[(url, first_party)] = time.monotonic()
            while len(self._recent_blocked) > 512:
                self._recent_blocked.pop(next(iter(self._recent_blocked)))
        else:
            self.allowed += 1

    def was_blocked(self, url: str, document_url: str = "", *, within: float = 120.0) -> bool:
        """Whether this blocker aborted ``url`` for that first party in the last ``within`` seconds."""
        first_party = registrable_domain(urlparse(document_url or "").hostname or "")
        at = self._recent_blocked.get((url, first_party))
        if at is None and not document_url:
            at = max((t for (u, _fp), t in self._recent_blocked.items() if u == url), default=None)
        return at is not None and time.monotonic() - at <= within

    # -- wiring ---------------------------------------------------------

    async def attach(self, target: Any) -> None:
        """Attach to a BrowserContext (preferred) or a Page."""
        await target.route("**/*", self._handle)

    async def detach(self, target: Any) -> None:
        try:
            await target.unroute("**/*", self._handle)
        except Exception:
            pass

    async def _handle(self, route: Any, request: Any = None) -> None:
        request = request if request is not None else route.request
        document_url = ""
        try:
            url = str(getattr(request, "url", "") or "")
            resource_type = str(getattr(request, "resource_type", "") or "other")
            try:
                frame = getattr(request, "frame", None)
                document_url = str(getattr(frame, "url", "") or "") if frame is not None else ""
            except Exception:
                # A popup's first document request has no frame yet.
                document_url = ""
            blocked = self.should_block(url, resource_type, document_url)
        except Exception:
            # Never let the blocker be the reason a page fails to load.
            blocked = False
            resource_type, url = "other", ""
        self._note(blocked, url, resource_type, document_url)
        try:
            if blocked:
                await route.abort("blockedbyclient")
            else:
                await route.continue_()
        except Exception:
            # The page can navigate away mid-decision; a dead route is not an error.
            pass

    # -- reporting ------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        total = self.blocked + self.allowed
        top = sorted(self._blocked_by_domain.items(), key=lambda item: item[1], reverse=True)
        return {
            "enabled": self.enabled,
            "blocked": self.blocked,
            "allowed": self.allowed,
            "block_rate": round(self.blocked / total, 4) if total else 0.0,
            "bytes_saved_estimate": self.bytes_saved_estimate,
            "top_blocked_domains": [{"domain": d, "count": c} for d, c in top[:10]],
        }

    def reset_stats(self) -> None:
        self.blocked = 0
        self.allowed = 0
        self.bytes_saved_estimate = 0
        self._blocked_by_domain.clear()


def build_from_settings() -> AdBlocker:
    """Construct a blocker from settings, so deployments can tune it."""
    from ascended_browser.runtime.settings import get_setting

    extra = str(get_setting("browser_adblock_extra_domains", "") or "")
    allow = str(get_setting("browser_adblock_allow_domains", "") or "")
    split = lambda value: [p.strip() for p in re.split(r"[,\s]+", value) if p.strip()]  # noqa: E731
    return AdBlocker(
        blocked_domains=DEFAULT_BLOCKED_DOMAINS.union(split(extra)),
        allow_domains=split(allow),
        enabled=bool(get_setting("browser_adblock_enabled", True)),
    )
