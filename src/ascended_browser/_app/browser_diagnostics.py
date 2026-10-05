"""What a tab's page reported while the agent worked on it.

A developer watching their own app keeps the devtools console open. The agent
has no console: a click that threw ``TypeError: x is undefined`` and a save
that returned 500 both read as "page changed" or "outcome unknown", and the
model's only way to learn more is a round trip it does not know it needs.

Playwright MCP appends "new console messages" to every action response;
Chrome DevTools MCP keeps console and network per page since the last
navigation. Neither attributes an entry to the action that caused it or
filters out what is not the page's fault. This module does both:

- one ``TabDiagnostics`` per resident page, attached where the manager
  attaches its other page listeners and dropped with the page;
- console errors, uncaught exceptions, failed requests and 4xx/5xx responses
  recorded in a bounded ring, plus a body-free network log for the
  ``browser_extract`` ``read="network"`` view;
- noise rules: identical messages collapse into one entry with a count,
  third-party failures are counted and never listed, adblock aborts, favicon
  misses, beacons, source maps and navigation-cancelled requests never enter;
- delivery as a delta: ``take_summary`` returns only what arrived since the
  previous result for this tab, split into "during this action" and
  "earlier", at most a few lines, and nothing at all when nothing happened.

Nothing here decides anything. The summary is page evidence beside
``auth_state`` and ``navigation_state``; the model decides what it means.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from ascended_browser._app.browser_adblock import registrable_domain

#: Entries kept per tab across the last few navigations.
MAX_ENTRIES = 400
#: Network log rows kept per tab.
MAX_NETWORK_ENTRIES = 300
#: Navigations whose entries stay readable (the current one included).
KEPT_EPOCHS = 3
#: Lines an ambient summary lists before pointing at the full log.
SUMMARY_LINES = 3
#: Longest message text kept per entry.
TEXT_CHARS = 300
#: Same-site JSON bodies kept for the network view, bytes.
BODY_PREVIEW_BYTES = 8_192
_BODY_TASKS_MAX = 8

#: The browser cancelled the request itself (the page navigated away or the
#: script that wanted it was torn down). Not the page's doing.
_CANCELLED_FAILURES = (
    "NS_BINDING_ABORTED", "NS_ERROR_ABORT", "net::ERR_ABORTED", "Load cancelled",
)
#: Our own request blocker. Its aborts are policy, not failures.
_BLOCKED_FAILURES = ("BLOCKED_BY_CLIENT", "blockedbyclient", "NS_ERROR_BLOCKED")
_NOISE_TYPES = frozenset({"ping", "beacon", "csp_report", "cspviolationreport"})
_ASSET_TYPES = frozenset({"image", "font", "media", "stylesheet", "imageset", "manifest"})
_NOISE_PATHS = re.compile(r"(?:/favicon\.ico|\.map)(?:\?|$)", re.I)
_LISTED_CONSOLE_LEVELS = frozenset({"error", "warning"})
_DEV_SUFFIXES = (".localhost", ".local", ".test", ".internal", ".lan", ".home.arpa")


def is_dev_origin(url: str) -> bool:
    """Loopback, private-network and ``.localhost``-style hosts: the developer's own app."""
    try:
        host = (urlparse(str(url or "")).hostname or "").casefold()
    except ValueError:
        return False
    if not host:
        return False
    if host in {"localhost", "0.0.0.0", "::1"} or host.endswith(_DEV_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback or address.is_link_local)


def _site(url: str) -> str:
    try:
        return registrable_domain(urlparse(str(url or "")).hostname or "")
    except ValueError:
        return ""


def _path(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return str(url or "")[:120]
    return (parsed.path or "/")[:120]


def _host_path(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return str(url or "")[:120]
    return f"{parsed.hostname or ''}{(parsed.path or '/')[:100]}"


def _short(text: Any, limit: int = TEXT_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _source_name(url: str, page_url: str = "") -> str:
    """``app.js`` for a script URL; ``inline`` for the page document itself."""
    bare = url.split("#", 1)[0]
    if page_url and bare.split("?", 1)[0] == page_url.split("#", 1)[0].split("?", 1)[0]:
        return "inline"
    name = bare.rsplit("/", 1)[-1].split("?", 1)[0]
    return name or "inline"


def _location(location: Any, page_url: str = "") -> str:
    """``app.js:41`` from a console message location, or empty."""
    if not isinstance(location, dict):
        return ""
    url = str(location.get("url") or "")
    if not url or url == "about:blank":
        return ""
    name = _source_name(url, page_url)
    line = location.get("lineNumber", location.get("line"))
    return f"{name}:{line}" if line not in (None, "", 0) else name


_STACK_FRAME = re.compile(r"((?:https?|file|blob):[^\s()]+?):(\d+):(\d+)\)?(?:\s|$)")
_STACK_FRAME_LOOSE = re.compile(r"((?:https?|file|blob):[^\s()]+?):(\d+)\)?(?:\s|$)")


def _stack_location(stack: str, page_url: str = "") -> str:
    """The first ``file:line`` a stack names, so an uncaught error says where."""
    text = str(stack or "")
    match = _STACK_FRAME.search(text) or _STACK_FRAME_LOOSE.search(text)
    if not match:
        return ""
    return f"{_source_name(match.group(1), page_url)}:{match.group(2)}"


#: Playwright renders non-primitive console arguments as handles.
_HANDLE_TOKENS = (("JSHandle@object", "{…}"), ("JSHandle@array", "[…]"), ("JSHandle@error", "Error{…}"),
                  ("JSHandle@node", "<node>"), ("JSHandle@function", "ƒ"))
#: Firefox reports its own resource and parser diagnostics through the console
#: wrapped like this; the message inside is what a developer reads.
_FIREFOX_WRAPPED = re.compile(
    r'^\[JavaScript (?P<kind>Warning|Error): "(?P<message>.*?)"'
    r'(?:\s*\{file: "(?P<file>[^"]*)"\s*line: (?P<line>\d+)(?:\s*column: \d+)?[^}]*\})?\]$', re.S,
)
_LOADING_FAILED = re.compile(r"Loading failed for the <\w+> with source [“\"'](?P<src>[^”\"']+)[”\"']")


def _clean_console_text(text: str) -> str:
    for token, replacement in _HANDLE_TOKENS:
        text = text.replace(token, replacement)
    return text


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'”“<>)\]]+")
#: The browser's own policy notices about other people's cookies and
#: cross-origin reads. They are reported at error level, but a tracker's
#: cookie being rejected is not the page failing.
_BROWSER_POLICY_NOTICE = re.compile(
    r"^(?:Cookie [“\"].*?[”\"] has been rejected|Cross-Origin Request Blocked|"
    r"Access to (?:fetch|XMLHttpRequest|script|font|image) at|Partitioned cookie or storage access|"
    r"downloadable font: |"
    r"The resource at [“\"].*[”\"] was blocked)",
)
#: Firefox logs this at error level when a navigation tears down an open
#: EventSource or WebSocket: every hot reload of a dev server (Vite's HMR
#: socket, a reload stream) produces one. It is the reload happening, which
#: the navigation already reports, not the page failing; it made `until
#: no_new_errors` stop on every reload and kept a fixed page from reading as
#: quiet. Not recorded at all.
_NAVIGATION_TEARDOWN_NOTICE = re.compile(
    r"^The connection to \S+ was interrupted while the page was loading",
)
_CORS_TEXT = re.compile(r"Cross-Origin Request Blocked|blocked by CORS policy|Access to (?:fetch|XMLHttpRequest) at", re.I)


@dataclass
class Entry:
    seq: int
    epoch: int
    at: float
    kind: str                     # console | pageerror | request_failed | response_error
    level: str                    # error | warning | info | log
    text: str
    same_site: bool
    url: str = ""
    method: str = ""
    status: int | None = None
    resource_type: str = ""
    location: str = ""
    count: int = 1

    def key(self) -> tuple:
        return (self.kind, self.level, self.text, self.url, self.method, self.status)

    def line(self) -> str:
        suffix = f" ×{self.count}" if self.count > 1 else ""
        where = f" ({self.location})" if self.location else ""
        if self.kind == "console":
            return f"console.{self.level}: {self.text}{where}{suffix}"
        if self.kind == "pageerror":
            return f"uncaught: {self.text}{where}{suffix}"
        target = _path(self.url) if self.same_site else _host_path(self.url)
        kind = f" ({self.resource_type})" if self.resource_type else ""
        if self.kind == "request_failed":
            return f"{self.method} {target} failed: {self.text}{kind}{suffix}"
        return f"{self.method} {target} → {self.status}{kind}{suffix}"

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "level": self.level, "text": self.text}
        for name in ("url", "method", "status", "resource_type", "location"):
            value = getattr(self, name)
            if value not in ("", None):
                out[name] = value
        if self.count > 1:
            out["count"] = self.count
        if not self.same_site:
            out["third_party"] = True
        return out


@dataclass
class NetworkEntry:
    seq: int
    epoch: int
    method: str
    url: str
    resource_type: str
    same_site: bool
    started: float
    status: int | None = None
    duration_ms: int | None = None
    size: int | None = None
    content_type: str = ""
    failure: str = ""
    body: str = ""
    body_truncated: bool = False
    navigation: bool = False

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "method": self.method,
            "url": self.url if len(self.url) <= 512 else self.url[:511] + "…",
            "type": self.resource_type,
        }
        if self.status is not None:
            out["status"] = self.status
        if self.failure:
            out["failed"] = self.failure
        elif self.status is None:
            out["pending"] = True
        if self.duration_ms is not None:
            out["ms"] = self.duration_ms
        if self.size is not None:
            out["bytes"] = self.size
        if self.content_type:
            out["content_type"] = self.content_type
        if not self.same_site:
            out["third_party"] = True
        if self.navigation:
            out["navigation"] = True
        if self.body:
            out["body"] = self.body
            if self.body_truncated:
                out["body_truncated"] = True
        return out


@dataclass
class _Epoch:
    index: int
    url: str
    started: float
    dev: bool = field(default=False)


class TabDiagnostics:
    """Console, error and network evidence for one resident Playwright page."""

    def __init__(
        self, page: Any, *, is_noise: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.page = page
        self._clock = clock
        self._is_noise = is_noise
        self._seq = 0
        self.entries: list[Entry] = []
        self.network: list[NetworkEntry] = []
        self._open_requests: dict[Any, NetworkEntry] = {}
        self._body_tasks: set[Any] = set()
        self._delivered_seq = 0
        self._action_mark: int | None = None
        self._listeners: list[tuple[str, Any]] = []
        self._epoch = _Epoch(0, self._page_url(), self._clock(), is_dev_origin(self._page_url()))
        # The main-frame document request in flight, so a reload of the same
        # URL (no URL change to see) still starts a new epoch, while a
        # pushState route change (no document request) does not.
        self._pending_document = ""
        self.attached = False

    # -- lifecycle -----------------------------------------------------------

    def attach(self) -> "TabDiagnostics":
        if self.attached:
            return self
        handlers = (
            ("console", self._on_console),
            ("pageerror", self._on_page_error),
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfinished", self._on_request_finished),
            ("requestfailed", self._on_request_failed),
            ("framenavigated", self._on_frame_navigated),
        )
        for event, handler in handlers:
            try:
                self.page.on(event, handler)
            except Exception:
                continue
            self._listeners.append((event, handler))
        self.attached = True
        return self

    def detach(self) -> None:
        for event, handler in self._listeners:
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass
        self._listeners.clear()
        self.attached = False
        for task in list(self._body_tasks):
            try:
                task.cancel()
            except Exception:
                pass
        self._body_tasks.clear()
        self._open_requests.clear()

    # -- what the page is -----------------------------------------------------

    def _page_url(self) -> str:
        try:
            return str(getattr(self.page, "url", "") or "")
        except Exception:
            return ""

    @property
    def epoch(self) -> int:
        return self._epoch.index

    @property
    def dev(self) -> bool:
        return self._epoch.dev

    @property
    def seq(self) -> int:
        return self._seq

    def _same_site(self, url: str) -> bool:
        page_site = _site(self._epoch.url) or _site(self._page_url())
        site = _site(url)
        if not site or not page_site:
            return True
        return site == page_site

    # -- listeners --------------------------------------------------------------

    def _on_frame_navigated(self, frame: Any) -> None:
        try:
            if frame is not self.page.main_frame:
                return
            url = str(getattr(frame, "url", "") or "")
        except Exception:
            return
        if not url or (url == "about:blank" and not self._epoch.url):
            return
        bare = url.split("#", 1)[0]
        reloaded = bool(self._pending_document) and self._pending_document.split("#", 1)[0] == bare
        self._pending_document = ""
        if bare == self._epoch.url.split("#", 1)[0] and not reloaded:
            return
        self._epoch = _Epoch(self._epoch.index + 1, url, self._clock(), is_dev_origin(url))
        # The document request that produced this navigation (and its error
        # response, if any) was recorded before the commit; it belongs to the
        # page it loaded, not the one it left.
        bare = url.split("#", 1)[0]
        for entry in reversed(self.network[-20:]):
            if entry.navigation and entry.url.split("#", 1)[0] == bare:
                entry.epoch = self._epoch.index
                break
        for entry in reversed(self.entries[-20:]):
            if entry.resource_type == "document" and entry.url.split("#", 1)[0] == bare:
                entry.epoch = self._epoch.index
                break
        floor = self._epoch.index - KEPT_EPOCHS + 1
        self.entries = [entry for entry in self.entries if entry.epoch >= floor]
        self.network = [entry for entry in self.network if entry.epoch >= floor]

    def _on_console(self, message: Any) -> None:
        try:
            level = str(getattr(message, "type", "") or "").casefold()
            if level == "warn":
                level = "warning"
            if level not in _LISTED_CONSOLE_LEVELS and not (self.dev and level in {"log", "info"}):
                return
            raw = str(getattr(message, "text", "") or "")
            location = getattr(message, "location", None)
            source_url = str(location.get("url") or "") if isinstance(location, dict) else ""
            wrapped = _FIREFOX_WRAPPED.match(raw.strip())
            if wrapped:
                raw = wrapped.group("message")
                if wrapped.group("file"):
                    location = {"url": wrapped.group("file"), "lineNumber": int(wrapped.group("line") or 0)}
                    source_url = wrapped.group("file")
                failed_load = _LOADING_FAILED.search(raw)
                if failed_load and not self._same_site(failed_load.group("src")):
                    # The request log already counted this third-party miss.
                    return
            text = _short(_clean_console_text(raw))
            where = _location(location, self._page_url())
            same_site = self._same_site(source_url) if source_url else True
            # A message about another origin is that origin's problem: a
            # tracker's cookie rejected, a CORS read of an analytics
            # endpoint. Judge it by the URL it names, not the page that logged it.
            named = _URL_IN_TEXT.findall(raw)
            if named and all(not self._same_site(url) for url in named):
                same_site = False
            if _NAVIGATION_TEARDOWN_NOTICE.match(raw.strip()):
                return
            if level == "error" and _BROWSER_POLICY_NOTICE.match(raw.strip()):
                level = "warning"
                if _CORS_TEXT.search(raw) and not named:
                    same_site = False
        except Exception:
            return
        if not text:
            return
        self._record(Entry(
            seq=0, epoch=self.epoch, at=self._clock(), kind="console", level=level, text=text,
            same_site=same_site, location=where,
        ))

    def _on_page_error(self, error: Any) -> None:
        try:
            message = getattr(error, "message", None)
            if message is None:
                message = str(error)
            stack = str(getattr(error, "stack", "") or "")
            name = str(getattr(error, "name", "") or "")
            if not str(message or "").strip() and stack.strip():
                # Firefox hands some errors over with an empty message and
                # the whole story on the stack's first line.
                message = stack.strip().split("\n", 1)[0]
            text = _short(message)
            if name and not text.startswith(name):
                text = _short(f"{name}: {message}")
            elif stack and ":" in stack.split("\n", 1)[0] and not re.match(r"^\w*Error\b", text):
                head = stack.split("\n", 1)[0]
                if head.endswith(text):
                    text = _short(head)
            where = _stack_location(stack, self._page_url())
            frame = _STACK_FRAME.search(stack) or _STACK_FRAME_LOOSE.search(stack)
            same_site = self._same_site(frame.group(1)) if frame else True
        except Exception:
            return
        self._record(Entry(
            seq=0, epoch=self.epoch, at=self._clock(), kind="pageerror", level="error",
            text=text or "uncaught error", same_site=same_site, location=where,
        ))

    def _on_request(self, request: Any) -> None:
        try:
            url = str(request.url or "")
            resource_type = str(request.resource_type or "")
            method = str(request.method or "GET").upper()
            navigation = bool(request.is_navigation_request())
        except Exception:
            return
        if not url.startswith(("http://", "https://")):
            return
        if navigation:
            try:
                frame = getattr(request, "frame", None)
                if frame is None or getattr(frame, "parent_frame", None) is None:
                    self._pending_document = url
            except Exception:
                self._pending_document = url
        self._seq += 1
        entry = NetworkEntry(
            seq=self._seq, epoch=self.epoch, method=method, url=url, resource_type=resource_type,
            same_site=self._same_site(url), started=self._clock(), navigation=navigation,
        )
        self._open_requests[request] = entry
        self.network.append(entry)
        del self.network[:-MAX_NETWORK_ENTRIES]
        if len(self._open_requests) > MAX_NETWORK_ENTRIES:
            for stale in list(self._open_requests)[: len(self._open_requests) - MAX_NETWORK_ENTRIES]:
                self._open_requests.pop(stale, None)

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            status = int(response.status)
            url = str(response.url or "")
            method = str(request.method or "GET").upper()
            resource_type = str(request.resource_type or "")
            headers = {}
            try:
                headers = {str(k).casefold(): str(v) for k, v in (response.headers or {}).items()}
            except Exception:
                headers = {}
        except Exception:
            return
        entry = self._open_requests.get(request)
        if entry is not None:
            entry.status = status
            entry.content_type = headers.get("content-type", "").split(";", 1)[0].strip()
            try:
                entry.size = int(headers["content-length"]) if headers.get("content-length") else None
            except ValueError:
                entry.size = None
            self._maybe_keep_body(response, entry)
        if status < 400 or self._noise(url, resource_type):
            return
        same_site = self._same_site(url)
        self._record(Entry(
            seq=0, epoch=self.epoch, at=self._clock(), kind="response_error", level="error",
            text=str(status), same_site=same_site, url=url, method=method, status=status,
            resource_type=resource_type,
        ))

    def _on_request_finished(self, request: Any) -> None:
        entry = self._open_requests.pop(request, None)
        if entry is not None:
            entry.duration_ms = int((self._clock() - entry.started) * 1000)

    def _on_request_failed(self, request: Any) -> None:
        entry = self._open_requests.pop(request, None)
        try:
            url = str(request.url or "")
            method = str(request.method or "GET").upper()
            resource_type = str(request.resource_type or "")
            failure = getattr(request, "failure", None)
            if callable(failure):
                failure = failure()
            if isinstance(failure, dict):
                failure = failure.get("errorText") or failure.get("error_text") or ""
            failure = _short(failure or "failed", 80)
        except Exception:
            return
        if entry is not None:
            entry.duration_ms = int((self._clock() - entry.started) * 1000)
            entry.failure = failure
            if entry.status is not None and entry.status >= 400:
                # Already reported as the error response it was; the failure
                # that follows (a proxy's corrupted body) is the same event.
                return
        if any(token in failure for token in _CANCELLED_FAILURES):
            return
        if any(token.casefold() in failure.casefold() for token in _BLOCKED_FAILURES):
            return
        try:
            document_url = str(getattr(getattr(request, "frame", None), "url", "") or "")
        except Exception:
            document_url = ""
        if self._noise(url, resource_type, document_url):
            return
        self._record(Entry(
            seq=0, epoch=self.epoch, at=self._clock(), kind="request_failed", level="error",
            text=failure, same_site=self._same_site(url), url=url, method=method,
            resource_type=resource_type,
        ))

    # -- recording ---------------------------------------------------------------

    def _noise(self, url: str, resource_type: str, document_url: str = "") -> bool:
        if resource_type in _NOISE_TYPES or _NOISE_PATHS.search(url or ""):
            return True
        if self._is_noise is not None:
            try:
                return bool(self._is_noise(url, document_url or self._page_url()))
            except TypeError:
                try:
                    return bool(self._is_noise(url))
                except Exception:
                    return False
            except Exception:
                return False
        return False

    def inflight(self, *, max_age: float = 3.0) -> list[str]:
        """Tracked requests (document/fetch/XHR/script) still open and younger than ``max_age``.

        Seen from the log, so a request that began before a wait started is
        not invisible to it the way a fresh listener's would be.
        """
        now = self._clock()
        return [
            entry.url for entry in self.network
            if entry.status is None and not entry.failure
            and entry.resource_type in {"document", "fetch", "xhr", "script"}
            and now - entry.started < max_age
        ]

    def _record(self, entry: Entry) -> None:
        key = entry.key()
        # Never merge across the action mark: an error that existed before the
        # action and fires again during it is the action's own new error.
        floor = max(self._delivered_seq, self._action_mark or 0)
        for existing in reversed(self.entries):
            if existing.seq <= floor:
                break
            if existing.key() == key:
                existing.count += 1
                existing.at = entry.at
                return
        self._seq += 1
        entry.seq = self._seq
        self.entries.append(entry)
        del self.entries[:-MAX_ENTRIES]

    def _maybe_keep_body(self, response: Any, entry: NetworkEntry) -> None:
        if not entry.same_site or entry.resource_type not in {"fetch", "xhr"}:
            return
        if "json" not in entry.content_type and "text/plain" not in entry.content_type:
            return
        if not (self.dev or (entry.status or 0) >= 400):
            return
        if entry.size is not None and entry.size > BODY_PREVIEW_BYTES * 4:
            return
        if len(self._body_tasks) >= _BODY_TASKS_MAX:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def keep() -> None:
            try:
                text = await asyncio.wait_for(response.text(), 3.0)
            except Exception:
                return
            if len(text) > BODY_PREVIEW_BYTES:
                entry.body, entry.body_truncated = text[:BODY_PREVIEW_BYTES], True
            else:
                entry.body = text

        task = loop.create_task(keep())
        self._body_tasks.add(task)
        task.add_done_callback(self._body_tasks.discard)

    # -- delivery ----------------------------------------------------------------

    def mark_action(self) -> int:
        """Everything recorded after this belongs to the action being dispatched."""
        self._action_mark = self._seq
        return self._seq

    @property
    def action_mark(self) -> int | None:
        return self._action_mark

    def errors_since(self, seq: int) -> list[str]:
        """Same-site error lines recorded after ``seq`` (uncaught, console.error, failed requests)."""
        return [
            entry.line() for entry in self.entries
            if entry.seq > seq and entry.same_site and entry.level == "error"
        ]

    def failed_writes_since(self, seq: int) -> list[str]:
        """Same-site state-changing requests (POST/PUT/PATCH/DELETE) that failed after ``seq``.

        The strongest sign that the effect an action waits for will not
        arrive: its own save came back as an error.
        """
        return [
            entry.line() for entry in self.entries
            if entry.seq > seq and entry.same_site
            and entry.kind in {"request_failed", "response_error"}
            and entry.method in {"POST", "PUT", "PATCH", "DELETE"}
        ]

    def take_summary(self, *, limit: int = SUMMARY_LINES) -> dict[str, Any] | None:
        """New evidence since the previous result for this tab, or None.

        Advances the delivered cursor: the next result only reports what
        arrives after this one. Third-party failures are counted, not listed;
        warnings are listed only on the developer's own origins.
        """
        pending = [entry for entry in self.entries if entry.seq > self._delivered_seq]
        mark = self._action_mark
        self._action_mark = None
        self._delivered_seq = self._seq
        if not pending:
            return None
        listed = [
            entry for entry in pending
            if entry.same_site and (entry.level == "error" or (self.dev and entry.level == "warning"))
            # On the public web a missing image, font or stylesheet is counted,
            # not listed: it rarely changes what can be done on the page. On the
            # developer's own origin it is exactly what they want to see.
            and (self.dev or entry.resource_type not in _ASSET_TYPES)
        ]
        counts = {
            "errors": sum(e.count for e in pending if e.same_site and e.level == "error"
                          and e.kind in {"console", "pageerror"}),
            "warnings": sum(e.count for e in pending if e.same_site and e.level == "warning"),
            "failed_requests": sum(e.count for e in pending if e.same_site
                                   and e.kind in {"request_failed", "response_error"}),
            "third_party_failed": sum(e.count for e in pending if not e.same_site
                                      and e.kind in {"request_failed", "response_error"}),
        }
        if not listed and not (self.dev and counts["third_party_failed"]):
            return None
        summary: dict[str, Any] = {key: value for key, value in counts.items() if value}
        if mark is not None:
            during = [e for e in listed if e.seq > mark]
            earlier = [e for e in listed if e.seq <= mark]
        else:
            during, earlier = [], listed
        shown = 0
        if during:
            summary["during_action"] = [e.line() for e in during[:limit]]
            shown += len(summary["during_action"])
        if earlier and shown < limit:
            summary["earlier"] = [e.line() for e in earlier[-(limit - shown):]]
            shown += len(summary["earlier"])
        more = len(listed) - shown
        if more > 0:
            summary["more"] = more
        if more > 0 or counts["failed_requests"] or counts["third_party_failed"]:
            summary["read"] = (
                "browser_extract read=console for every message, read=network for requests"
            )
        return summary

    # -- views for browser_extract -------------------------------------------------

    def console_entries(self, *, level: str = "", epochs: int = 1) -> list[dict[str, Any]]:
        floor = self.epoch - max(1, epochs) + 1
        wanted = str(level or "").casefold()
        out = []
        for entry in self.entries:
            if entry.epoch < floor or entry.kind in {"request_failed", "response_error"}:
                continue
            if wanted and entry.level != wanted:
                continue
            out.append({**entry.public(), "navigation": self.epoch - entry.epoch})
        return out

    def network_entries(
        self, *, pattern: str = "", types: Iterable[str] = (), failed_only: bool = False,
        epochs: int = 1,
    ) -> list[dict[str, Any]]:
        floor = self.epoch - max(1, epochs) + 1
        regex = re.compile(pattern, re.I) if pattern else None
        wanted = {str(t).casefold() for t in types if t}
        out = []
        for entry in self.network:
            if entry.epoch < floor:
                continue
            if wanted and entry.resource_type.casefold() not in wanted:
                continue
            if failed_only and not (entry.failure or (entry.status or 0) >= 400):
                continue
            if regex is not None and not regex.search(entry.url):
                continue
            row = entry.public()
            if self.epoch != entry.epoch:
                row["navigation"] = self.epoch - entry.epoch
            out.append(row)
        return out

    def counts(self) -> dict[str, int]:
        return {
            "entries": len(self.entries),
            "network": len(self.network),
            "pending_requests": sum(1 for e in self.network if e.status is None and not e.failure),
        }


# -- the model-facing text for browser_extract read=console / read=network -----


def _size(value: int | None) -> str:
    if value is None:
        return ""
    if value >= 1_048_576:
        return f"{value / 1_048_576:.1f}MB"
    if value >= 1024:
        return f"{value / 1024:.1f}kB"
    return f"{value}B"


def format_console_log(result: dict, *, level: str = "") -> str:
    rows = result.get("entries") or []
    total = int(result.get("total") or 0)
    scope = "the current navigation" if int(result.get("navigations") or 1) <= 1 else f"the last {result.get('navigations')} navigations"
    wanted = f" at level {level}" if level else ""
    if total == 0:
        return f"No console messages{wanted} since {scope}. Errors, warnings and uncaught exceptions are recorded; log/info only on your own dev origins."
    errors = sum(int(r.get("count") or 1) for r in rows if r.get("level") == "error")
    warnings = sum(int(r.get("count") or 1) for r in rows if r.get("level") == "warning")
    lines = [f"Console since {scope}: {total} message{'s' if total != 1 else ''}{wanted}"
             f" (showing {result.get('showing')} from {result.get('cursor') or 0}; errors {errors}, warnings {warnings}):", ""]
    start = int(result.get("cursor") or 0)
    for index, row in enumerate(rows, start):
        prefix = f"[{index}] {row.get('level')}"
        if row.get("kind") == "pageerror":
            prefix += " uncaught"
        text = row.get("text") or ""
        where = f" ({row['location']})" if row.get("location") else ""
        count = f" ×{row['count']}" if row.get("count") else ""
        nav = f" [{row['navigation']} navigation(s) ago]" if row.get("navigation") else ""
        third = " [third-party script]" if row.get("third_party") else ""
        lines.append(f"{prefix}: {text}{where}{count}{third}{nav}")
    if result.get("next_cursor") is not None:
        lines.append("")
        lines.append(f"More: continue with cursor={result['next_cursor']}.")
    return "\n".join(lines)


def format_network_log(result: dict, *, pattern: str = "", types: Iterable[str] = (), failed_only: bool = False) -> str:
    rows = result.get("entries") or []
    total = int(result.get("total") or 0)
    scope = "the current navigation" if int(result.get("navigations") or 1) <= 1 else f"the last {result.get('navigations')} navigations"
    filters = []
    if pattern:
        filters.append(f"matching /{pattern}/")
    if types:
        filters.append("types " + ",".join(str(t) for t in types))
    if failed_only:
        filters.append("failed only")
    described = (" " + ", ".join(filters)) if filters else ""
    if total == 0:
        return f"No requests{described} since {scope}."
    failed = sum(1 for r in rows if r.get("failed") or int(r.get("status") or 0) >= 400)
    lines = [f"Requests since {scope}{described}: {total} (showing {result.get('showing')} from {result.get('cursor') or 0}; failed {failed} of those shown):", ""]
    start = int(result.get("cursor") or 0)
    for index, row in enumerate(rows, start):
        url = str(row.get("url") or "")
        target = _path(url) if not row.get("third_party") else _host_path(url)
        query = ""
        try:
            query = urlparse(url).query
        except ValueError:
            query = ""
        if query and not row.get("third_party"):
            target += "?" + (query if len(query) <= 60 else query[:59] + "…")
        outcome = (f"failed: {row['failed']}" if row.get("failed")
                   else "pending" if row.get("pending") else f"→ {row.get('status')}")
        parts = [f"[{index}] {row.get('method')} {target} {outcome}", str(row.get("type") or "")]
        if row.get("ms") is not None:
            parts.append(f"{row['ms']}ms")
        if row.get("bytes") is not None:
            parts.append(_size(row["bytes"]))
        if row.get("third_party"):
            parts.append("third-party")
        if row.get("navigation") and row.get("navigation") is not True:
            parts.append(f"[{row['navigation']} navigation(s) ago]")
        line = " ".join(p for p in parts if p)
        if row.get("body"):
            body = " ".join(str(row["body"]).split())
            line += f"\n    body: {body if len(body) <= 600 else body[:599] + '…'}" + (" [truncated]" if row.get("body_truncated") or len(body) > 600 else "")
        lines.append(line)
    if result.get("next_cursor") is not None:
        lines.append("")
        lines.append(f"More: continue with cursor={result['next_cursor']}.")
    return "\n".join(lines)
