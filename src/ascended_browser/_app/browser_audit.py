"""``browser_extract read=audit``: how a page is doing, in one read.

A developer who asks "audit my site" (deployed or on localhost) wants the
things a browser can measure without a model guessing: accessibility
violations with the element each one is on, how fast the page got to its
content and what it weighs, the basics a crawler reads (title, lang,
description, headings, alt text), which of its own links are broken, and what
the page logged. Before this read the agent assembled that from ``evaluate``
scripts, one metric per round, or did not do it at all.

Accessibility runs Deque's axe-core (vendored, unmodified, MPL-2.0, see
``THIRD_PARTY_NOTICES.md``), the engine behind Lighthouse's accessibility
score and agent-browser's audit; its rule ids and help URLs are what
developers already know how to act on. Everything else is a bounded page
script over the Performance and DOM APIs, reading through open shadow roots.
Link checks are same-origin only, sent without cookies, HEAD first, and skip
URLs that look like they act (logout, delete), because an audit must not
change the site it reads.

Owner of the audit's shape and text; the manager owns tab access, the
effect barrier and the diagnostics the page already holds.
"""
from __future__ import annotations

import asyncio
import functools
import re
from pathlib import Path
from typing import Any, Iterable

from ascended_browser._app.browser_shadow_dom import SHADOW_DOM_JS

AXE_VERSION = "4.10.3"
_AXE_PATH = Path(__file__).resolve().parent / "browser_vendor" / "axe-core" / "axe.min.js"

CHECKS = ("accessibility", "performance", "seo", "links")
LINK_CHECK_CAP = 40
LINK_TIMEOUT_MS = 5000
TEXT_BUDGET = 6500
_IMPACT_ORDER = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
#: Links that read as actions are never requested, even with HEAD and no cookies.
_ACTING_LINK = re.compile(r"(?:^|[/?&#_.-])(?:log|sign)[-_]?out|delete|remove|destroy|unsubscribe|cancel|revoke|disable|archive", re.I)


@functools.lru_cache(maxsize=1)
def axe_source() -> str:
    return _AXE_PATH.read_text(encoding="utf-8")


def _axe_script() -> str:
    # The UMD build exports to a `module` in scope when one exists, and also
    # assigns window.axe. Both are contained: the export is captured from a
    # private module object and the page's own `axe` property, if any, is put
    # back before the audit runs. `define` is shadowed so no page AMD loader
    # registers the engine as a module.
    return (
        "async (options) => {\n"
        "  const previous = Object.getOwnPropertyDescriptor(window, 'axe');\n"
        "  let engine;\n"
        "  try {\n"
        "    const module = {exports: {}};\n"
        "    const define = undefined;\n"
        + axe_source() +
        "\n    engine = (module.exports && typeof module.exports.run === 'function') ? module.exports : window.axe;\n"
        "  } finally {\n"
        "    if (previous) Object.defineProperty(window, 'axe', previous);\n"
        "    else { try { delete window.axe; } catch (e) {} }\n"
        "  }\n"
        "  if (!engine || typeof engine.run !== 'function') return {error: 'axe-core did not initialise'};\n"
        "  const results = await engine.run(document, {resultTypes: ['violations', 'incomplete'], iframes: false, elementRef: false});\n"
        "  const locate = (target) => {\n"
        "    // axe names a shadow element by a path of selectors, one per root.\n"
        "    const path = Array.isArray(target) ? target : [target];\n"
        "    let scope = document, el = null;\n"
        "    for (const sel of path) {\n"
        "      if (Array.isArray(sel)) return null;\n"
        "      try { el = scope.querySelector(sel); } catch (e) { return null; }\n"
        "      if (!el) return null;\n"
        "      scope = el.shadowRoot || el;\n"
        "    }\n"
        "    return el;\n"
        "  };\n"
        "  const node = (n) => {\n"
        "    const el = locate(n.target && n.target[0] !== undefined ? n.target : []);\n"
        "    const target = Array.isArray(n.target) ? n.target.map(t => Array.isArray(t) ? t.join(' >>> ') : String(t)).join(' >>> ') : String(n.target || '');\n"
        "    const out = {target: target.slice(0, 160), html: String(n.html || '').replace(/\\s*data-odysseus-browser-ref=\"[^\"]*\"/g, '').slice(0, 200),\n"
        "                 summary: String(n.failureSummary || '').replace(/^Fix (?:any|all) of the following:\\s*/i, '').replace(/\\s+/g, ' ').trim().slice(0, 300)};\n"
        "    if (el) {\n"
        "      const token = el.getAttribute && el.getAttribute('data-odysseus-browser-ref');\n"
        "      if (token) out.node_token = token;\n"
        "      try { const r = el.getBoundingClientRect(); out.visible = r.width > 0 && r.height > 0; } catch (e) {}\n"
        "    }\n"
        "    return out;\n"
        "  };\n"
        "  const trim = (rows, nodes) => rows.map(r => ({\n"
        "    id: r.id, impact: r.impact || 'unknown', help: r.help, help_url: r.helpUrl,\n"
        "    wcag: (r.tags || []).filter(t => /^wcag\\d/.test(t)).slice(0, 4),\n"
        "    node_count: r.nodes.length, nodes: r.nodes.slice(0, nodes).map(node),\n"
        "  }));\n"
        "  return {\n"
        "    engine: results.testEngine ? results.testEngine.version : null,\n"
        "    counts: {violations: results.violations.length, incomplete: results.incomplete.length,\n"
        "             passes: results.passes.length, inapplicable: results.inapplicable.length,\n"
        "             violating_nodes: results.violations.reduce((n, r) => n + r.nodes.length, 0)},\n"
        "    violations: trim(results.violations, 5),\n"
        "    incomplete: trim(results.incomplete, 1),\n"
        "  };\n"
        "}"
    )


PAGE_HEALTH_SCRIPT = "async () => {" + SHADOW_DOM_JS + r"""
  const out = {};
  const meta = (name) => { const el = document.querySelector('meta[name="' + name + '" i], meta[property="' + name + '" i]'); return el ? String(el.content || '') : ''; };
  const visible = (el) => { try { return el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden'; } catch (e) { return false; } };
  const short = (el) => {
    if (!el || el.nodeType !== 1) return '';
    const id = el.id ? '#' + el.id : '';
    const cls = el.classList && el.classList.length ? '.' + Array.from(el.classList).slice(0, 2).join('.') : '';
    return (el.tagName.toLowerCase() + id + cls).slice(0, 60);
  };
  // -- page basics (what a crawler and a reader meet first)
  const title = document.title || '';
  out.seo = {
    title, title_length: title.length,
    lang: document.documentElement.getAttribute('lang') || '',
    description: meta('description').slice(0, 200), description_length: meta('description').length,
    viewport_meta: !!document.querySelector('meta[name="viewport" i]'),
    canonical: (document.querySelector('link[rel="canonical" i]') || {}).href || '',
    robots: meta('robots'), charset: document.characterSet || '',
    og_title: !!meta('og:title'), og_image: !!meta('og:image'),
    favicon: !!document.querySelector('link[rel~="icon" i]'),
  };
  const headings = odyQueryAll(document, 'h1,h2,h3,h4,h5,h6').filter(visible);
  const levels = headings.map(h => Number(h.tagName[1]));
  const skips = [];
  for (let i = 1; i < levels.length; i += 1) {
    if (levels[i] > levels[i - 1] + 1) skips.push('h' + levels[i - 1] + '→h' + levels[i]);
  }
  out.seo.h1_count = levels.filter(l => l === 1).length;
  out.seo.h1 = (headings.find(h => h.tagName === 'H1') || {}).innerText || '';
  out.seo.h1 = String(out.seo.h1).replace(/\s+/g, ' ').trim().slice(0, 120);
  out.seo.heading_count = levels.length;
  out.seo.heading_skips = skips.slice(0, 5);
  const images = odyQueryAll(document, 'img');
  out.seo.images = images.length;
  out.seo.images_without_alt = images.filter(img => !img.hasAttribute('alt') && visible(img)).length;
  out.seo.images_without_dimensions = images.filter(img => visible(img) && !(img.getAttribute('width') && img.getAttribute('height')) && !(getComputedStyle(img).aspectRatio && getComputedStyle(img).aspectRatio !== 'auto')).length;
  const anchors = odyQueryAll(document, 'a[href]');
  let sameOrigin = 0, external = 0, blankNoOpener = 0, emptyLinks = 0;
  for (const a of anchors) {
    let u; try { u = new URL(a.getAttribute('href'), location.href); } catch (e) { continue; }
    if (!/^https?:$/.test(u.protocol)) continue;
    if (u.origin === location.origin) sameOrigin += 1; else external += 1;
    if (a.target === '_blank' && !/\b(noopener|noreferrer)\b/i.test(a.rel || '')) blankNoOpener += 1;
    if (!(a.innerText || a.getAttribute('aria-label') || a.title || '').trim() && !a.querySelector('img[alt]:not([alt=""]), svg[aria-label]')) emptyLinks += 1;
  }
  out.seo.links = {same_origin: sameOrigin, external, target_blank_without_noopener: blankNoOpener, without_text: emptyLinks};
  const roots = odyRoots(document, 500);
  out.dom = {
    nodes: roots.reduce((n, r) => n + r.querySelectorAll('*').length, 0),
    shadow_roots: roots.length - 1,
    iframes: document.querySelectorAll('iframe,frame').length,
    forms: odyQueryAll(document, 'form').length,
    scripts: document.scripts.length,
    stylesheets: document.styleSheets.length,
  };
  // -- performance
  const nav = (performance.getEntriesByType('navigation') || [])[0];
  const perf = {};
  if (nav) {
    perf.type = nav.type;
    perf.protocol = nav.nextHopProtocol || '';
    perf.ttfb_ms = Math.round(nav.responseStart - nav.startTime);
    perf.response_ms = Math.round(nav.responseEnd - nav.startTime);
    perf.dom_content_loaded_ms = Math.round(nav.domContentLoadedEventEnd - nav.startTime);
    perf.load_ms = nav.loadEventEnd ? Math.round(nav.loadEventEnd - nav.startTime) : null;
    perf.document_bytes = nav.transferSize || null;
    perf.redirects = nav.redirectCount || 0;
  }
  const fcp = (performance.getEntriesByName('first-contentful-paint') || [])[0];
  if (fcp) perf.fcp_ms = Math.round(fcp.startTime);
  const supported = (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes) || [];
  const observe = (type, ms) => new Promise(resolve => {
    if (!supported.includes(type)) { resolve(undefined); return; }
    const seen = [];
    let po;
    try {
      po = new PerformanceObserver(list => { for (const e of list.getEntries()) seen.push(e); });
      po.observe({type, buffered: true});
    } catch (e) { resolve(undefined); return; }
    setTimeout(() => { try { po.disconnect(); } catch (e) {} resolve(seen); }, ms);
  });
  const [lcpEntries, shiftEntries, longEntries] = await Promise.all([
    observe('largest-contentful-paint', 200), observe('layout-shift', 200), observe('longtask', 200),
  ]);
  if (lcpEntries === undefined) perf.lcp = null;
  else if (lcpEntries.length) {
    const last = lcpEntries[lcpEntries.length - 1];
    perf.lcp = {ms: Math.round(last.startTime), element: short(last.element), size: last.size || 0,
                url: last.url ? String(last.url).slice(0, 160) : ''};
  }
  if (shiftEntries === undefined) perf.cls = null;
  else perf.cls = Math.round(shiftEntries.filter(e => !e.hadRecentInput).reduce((n, e) => n + e.value, 0) * 1000) / 1000;
  if (longEntries === undefined) perf.long_tasks = null;
  else perf.long_tasks = {count: longEntries.length, total_ms: Math.round(longEntries.reduce((n, e) => n + e.duration, 0))};
  const resources = performance.getEntriesByType('resource') || [];
  const byType = {};
  let total = 0, thirdParty = 0, thirdPartyBytes = 0, mixed = 0;
  const largest = [];
  for (const r of resources) {
    const kind = ({script: 'script', link: 'css', css: 'css', img: 'image', imageset: 'image', font: 'font',
                   fetch: 'fetch', xmlhttprequest: 'fetch', beacon: 'other', video: 'media', audio: 'media',
                   iframe: 'frame', other: 'other', navigation: 'document'})[r.initiatorType] || 'other';
    const bytes = r.transferSize || 0;
    total += bytes;
    const row = byType[kind] || (byType[kind] = {count: 0, bytes: 0});
    row.count += 1; row.bytes += bytes;
    let origin = ''; try { origin = new URL(r.name).origin; } catch (e) {}
    if (origin && origin !== location.origin) { thirdParty += 1; thirdPartyBytes += bytes; }
    if (location.protocol === 'https:' && /^http:/i.test(r.name)) mixed += 1;
    largest.push({url: String(r.name).replace(location.origin, '').slice(0, 120), kind, bytes, ms: Math.round(r.duration)});
  }
  largest.sort((a, b) => b.bytes - a.bytes);
  perf.requests = resources.length + (nav ? 1 : 0);
  perf.transfer_bytes = total + ((nav && nav.transferSize) || 0);
  perf.by_type = byType;
  perf.third_party = {requests: thirdParty, bytes: thirdPartyBytes};
  perf.largest = largest.slice(0, 5);
  perf.slowest = resources.map(r => ({url: String(r.name).replace(location.origin, '').slice(0, 120), ms: Math.round(r.duration)}))
    .sort((a, b) => b.ms - a.ms).slice(0, 3);
  perf.mixed_content = mixed;
  perf.render_blocking = odyQueryAll(document, 'head script[src]:not([async]):not([defer]):not([type="module"])').length;
  out.performance = perf;
  out.viewport = {width: innerWidth, height: innerHeight};
  return out;
}"""


LINK_CHECK_SCRIPT = "async (opts) => {" + SHADOW_DOM_JS + r"""
  const {cap, timeoutMs, actingPattern} = opts;
  const acting = new RegExp(actingPattern, 'i');
  const here = location.href.split('#')[0];
  const seen = new Map();
  let sameOrigin = 0, external = 0, skippedActing = 0;
  for (const a of odyQueryAll(document, 'a[href]')) {
    let u; try { u = new URL(a.getAttribute('href'), location.href); } catch (e) { continue; }
    if (!/^https?:$/.test(u.protocol)) continue;
    if (u.origin !== location.origin) { external += 1; continue; }
    sameOrigin += 1;
    u.hash = '';
    if (u.href === here) continue;
    if (acting.test(u.pathname + u.search)) { skippedActing += 1; continue; }
    if (!seen.has(u.href)) {
      if (seen.size >= cap) continue;
      seen.set(u.href, String(a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g, ' ').trim().slice(0, 60));
    }
  }
  const check = async ([url, text]) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const path = url.replace(location.origin, '') || '/';
    try {
      // Redirects are not followed: a same-origin link that forwards off-site
      // (a tracker, a login bounce) is reachable, and following it from inside
      // the page would run into the page's own CSP and log errors it did not have.
      let r = await fetch(url, {method: 'HEAD', credentials: 'omit', redirect: 'manual', cache: 'no-store', signal: ctrl.signal});
      if (r.status === 405 || r.status === 501) {
        r = await fetch(url, {method: 'GET', credentials: 'omit', redirect: 'manual', cache: 'no-store', signal: ctrl.signal});
      }
      const redirected = r.type === 'opaqueredirect' || (r.status >= 300 && r.status < 400);
      return {url: path, text, status: r.status, ok: r.ok || redirected, redirected};
    } catch (e) {
      // From inside the page a fetch can also be refused by its CSP; say so
      // instead of calling the link broken.
      return {url: path, text, status: 0, ok: false, error: e && e.name === 'AbortError' ? 'timeout' : 'not reachable from the page (network error or CSP)'};
    } finally { clearTimeout(timer); }
  };
  const entries = Array.from(seen.entries());
  const results = [];
  let next = 0;
  const worker = async () => { while (next < entries.length) { const entry = entries[next++]; results.push(await check(entry)); } };
  await Promise.all(Array.from({length: Math.min(6, entries.length)}, worker));
  return {
    same_origin: sameOrigin, external, checked: results.length,
    unchecked_same_origin: Math.max(0, seen.size - results.length) + Math.max(0, sameOrigin - seen.size - skippedActing),
    skipped_acting: skippedActing,
    broken: results.filter(r => !r.ok).sort((a, b) => a.url.localeCompare(b.url)),
    redirected: results.filter(r => r.ok && r.redirected).length,
  };
}"""


def normalize_checks(value: Any) -> tuple[str, ...]:
    if value in (None, "", [], ()):
        return CHECKS
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    wanted = tuple(dict.fromkeys(str(v).strip().casefold() for v in value if str(v).strip()))
    unknown = [w for w in wanted if w not in CHECKS]
    if unknown:
        raise ValueError(f"checks must be among {', '.join(CHECKS)}; unknown: {', '.join(unknown)}")
    return wanted or CHECKS


async def run_audit(
    page: Any, *, checks: Iterable[str] = CHECKS, candidates: list[dict] | None = None,
    diagnostics: Any = None, link_cap: int = LINK_CHECK_CAP,
) -> dict[str, Any]:
    """Run the requested checks on the page; each is bounded and reported on its own."""
    checks = tuple(checks)
    result: dict[str, Any] = {"checks": list(checks), "errors": {}}

    async def bounded(coro: Any, seconds: float, name: str) -> Any:
        try:
            return await asyncio.wait_for(coro, seconds)
        except asyncio.TimeoutError:
            result["errors"][name] = f"did not finish within {seconds:.0f} s"
        except Exception as exc:  # the page refused or navigated away
            result["errors"][name] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return None

    health = await bounded(page.evaluate(PAGE_HEALTH_SCRIPT), 10.0, "page")
    if isinstance(health, dict):
        if "performance" in checks:
            result["performance"] = health.get("performance") or {}
        if "seo" in checks:
            result["seo"] = health.get("seo") or {}
        result["dom"] = health.get("dom") or {}
        result["viewport"] = health.get("viewport") or {}
    if "accessibility" in checks:
        a11y = await bounded(page.evaluate(_axe_script(), {}), 40.0, "accessibility")
        if isinstance(a11y, dict) and a11y.get("error"):
            result["errors"]["accessibility"] = str(a11y["error"])
        elif isinstance(a11y, dict):
            by_token = {
                str(c.get("node_token") or ""): str(c.get("ref") or "")
                for c in (candidates or []) if c.get("node_token") and int(c.get("frame_index") or 0) == 0
            }
            for group in ("violations", "incomplete"):
                rows = a11y.get(group) or []
                rows.sort(key=lambda r: (_IMPACT_ORDER.get(str(r.get("impact")), 9), -int(r.get("node_count") or 0)))
                for row in rows:
                    for node in row.get("nodes") or []:
                        token = str(node.pop("node_token", "") or "")
                        ref = by_token.get(token)
                        if ref:
                            node["ref"] = ref
            counts = a11y.get("counts") or {}
            counts["by_impact"] = {}
            for row in a11y.get("violations") or []:
                impact = str(row.get("impact") or "unknown")
                counts["by_impact"][impact] = counts["by_impact"].get(impact, 0) + 1
            result["accessibility"] = {**a11y, "engine": f"axe-core {a11y.get('engine') or AXE_VERSION}", "counts": counts}
    # The page's own console, read before the link check adds requests of its own.
    if diagnostics is not None:
        try:
            errors = diagnostics.console_entries(level="error")
            failed = diagnostics.network_entries(failed_only=True)
            result["console"] = {
                "errors": len(errors), "failed_requests": len(failed),
                "third_party_failed": sum(1 for row in failed if row.get("third_party")),
                "first_errors": [str(row.get("text") or "")[:160] for row in errors[:3]],
            }
        except Exception:
            pass
    if "links" in checks:
        links = await bounded(page.evaluate(LINK_CHECK_SCRIPT, {
            "cap": int(link_cap), "timeoutMs": LINK_TIMEOUT_MS, "actingPattern": _ACTING_LINK.pattern,
        }), 30.0, "links")
        if isinstance(links, dict):
            result["links"] = links
    result["summary"] = _summary(result)
    return result


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    a11y = result.get("accessibility") or {}
    if a11y:
        counts = a11y.get("counts") or {}
        summary["violations"] = counts.get("violations", 0)
        summary["violating_elements"] = counts.get("violating_nodes", 0)
        summary["needs_review"] = counts.get("incomplete", 0)
        summary["by_impact"] = counts.get("by_impact") or {}
    links = result.get("links") or {}
    if links:
        summary["broken_links"] = len(links.get("broken") or [])
        summary["links_checked"] = links.get("checked", 0)
    console = result.get("console") or {}
    if console:
        summary["console_errors"] = console.get("errors", 0)
        summary["failed_requests"] = console.get("failed_requests", 0)
    perf = result.get("performance") or {}
    if perf:
        if isinstance(perf.get("lcp"), dict):
            summary["lcp_ms"] = perf["lcp"].get("ms")
        if perf.get("load_ms") is not None:
            summary["load_ms"] = perf.get("load_ms")
        summary["requests"] = perf.get("requests")
        summary["transfer_kb"] = round((perf.get("transfer_bytes") or 0) / 1024)
    seo = result.get("seo") or {}
    if seo:
        gaps = []
        if not seo.get("title"):
            gaps.append("no title")
        if not seo.get("lang"):
            gaps.append("no lang")
        if not seo.get("description_length"):
            gaps.append("no meta description")
        if not seo.get("viewport_meta"):
            gaps.append("no viewport meta")
        if seo.get("h1_count") != 1:
            gaps.append(f"{seo.get('h1_count', 0)} h1")
        if seo.get("images_without_alt"):
            gaps.append(f"{seo['images_without_alt']} img without alt")
        summary["basics_gaps"] = gaps
    return summary


def _kb(value: Any) -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return "?"
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def _ms(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{n / 1000:.1f} s" if n >= 1000 else f"{n:.0f} ms"


def format_audit(result: dict[str, Any], *, budget: int = TEXT_BUDGET) -> str:
    """The audit as a brief: worst first, each item with the element it is on."""
    summary = result.get("summary") or {}
    checks = set(result.get("checks") or CHECKS)
    vp = result.get("viewport") or {}
    lines: list[str] = []
    head = f"Audit of {result.get('url') or 'the page'}"
    if result.get("title"):
        head += f" ({str(result['title'])[:80]!r})"
    if vp.get("width"):
        head += f", viewport {vp.get('width')}x{vp.get('height')}"
    lines.append(head + ":")
    parts = []
    if "violations" in summary:
        impacts = summary.get("by_impact") or {}
        detail = ", ".join(f"{impacts[k]} {k}" for k in ("critical", "serious", "moderate", "minor") if impacts.get(k))
        parts.append(f"{summary['violations']} accessibility violation{'s' if summary['violations'] != 1 else ''} on "
                     f"{summary.get('violating_elements', 0)} elements" + (f" ({detail})" if detail else ""))
    if "broken_links" in summary:
        parts.append(f"{summary['broken_links']} broken of {summary.get('links_checked', 0)} same-origin links checked")
    if "console_errors" in summary:
        parts.append(f"{summary['console_errors']} console errors, {summary.get('failed_requests', 0)} failed requests on this page")
    if "lcp_ms" in summary or "load_ms" in summary:
        parts.append(f"LCP {_ms(summary.get('lcp_ms'))}, load {_ms(summary.get('load_ms'))}, "
                     f"{summary.get('requests') or 0} requests, {summary.get('transfer_kb') or 0} KB")
    if summary.get("basics_gaps"):
        parts.append("basics: " + ", ".join(summary["basics_gaps"]))
    lines.append("Summary: " + ("; ".join(parts) if parts else "nothing measured") + ".")
    for name, reason in (result.get("errors") or {}).items():
        lines.append(f"  {name} check did not run: {reason}")

    sections: dict[str, list[str]] = {}
    a11y = result.get("accessibility") or {}
    if a11y:
        counts = a11y.get("counts") or {}
        rows = [f"Accessibility ({a11y.get('engine')}; {counts.get('passes', 0)} rules passed, "
                f"{counts.get('incomplete', 0)} need review):"]
        for row in (a11y.get("violations") or [])[:12]:
            rows.append(f"  [{row.get('impact')}] {row.get('id')}: {row.get('help')} ({row.get('node_count')} element"
                        f"{'s' if row.get('node_count') != 1 else ''}) {row.get('help_url') or ''}".rstrip())
            for node in (row.get("nodes") or [])[:3]:
                where = node.get("html") or node.get("target") or ""
                ref = f" ref={node['ref']}" if node.get("ref") else ""
                why = f" — {node['summary']}" if node.get("summary") else ""
                rows.append(f"      {where[:140]}{ref}{why}"[:400])
            if (row.get("node_count") or 0) > 3:
                rows.append(f"      … {row['node_count'] - 3} more element{'s' if row['node_count'] - 3 != 1 else ''}; "
                            f"browser_extract selector= finds them all")
        more = len(a11y.get("violations") or []) - 12
        if more > 0:
            rows.append(f"  … {more} more rule{'s' if more != 1 else ''} violated")
        if not (a11y.get("violations") or []):
            rows.append("  No violations.")
        review = a11y.get("incomplete") or []
        if review:
            rows.append("  Needs a human check: " + ", ".join(
                f"{r.get('id')} ({r.get('node_count')})" for r in review[:6]))
        sections["accessibility"] = rows
    perf = result.get("performance") or {}
    if perf:
        timing = [f"TTFB {_ms(perf.get('ttfb_ms'))}"]
        if perf.get("fcp_ms") is not None:
            timing.append(f"FCP {_ms(perf.get('fcp_ms'))}")
        lcp = perf.get("lcp")
        if isinstance(lcp, dict):
            timing.append(f"LCP {_ms(lcp.get('ms'))}" + (f" ({lcp.get('element')})" if lcp.get("element") else ""))
        elif lcp is None and "lcp" in perf:
            timing.append("LCP not measured on this engine")
        timing.append(f"DOMContentLoaded {_ms(perf.get('dom_content_loaded_ms'))}")
        timing.append(f"load {_ms(perf.get('load_ms'))}" if perf.get("load_ms") is not None else "load not finished")
        if perf.get("cls") is not None:
            timing.append(f"CLS {perf['cls']}")
        if isinstance(perf.get("long_tasks"), dict) and perf["long_tasks"].get("count"):
            timing.append(f"{perf['long_tasks']['count']} long tasks ({_ms(perf['long_tasks']['total_ms'])})")
        rows = ["Performance: " + ", ".join(timing) + (f"; {perf.get('protocol')}" if perf.get("protocol") else "")
                + (f", {perf['redirects']} redirect(s)" if perf.get("redirects") else "")]
        by_type = perf.get("by_type") or {}
        weight = ", ".join(f"{k} {_kb(v.get('bytes'))} in {v.get('count')}" for k, v in sorted(
            by_type.items(), key=lambda kv: -(kv[1].get("bytes") or 0))[:5])
        rows.append(f"  {perf.get('requests') or 0} requests, {_kb(perf.get('transfer_bytes'))} transferred"
                    + (f" ({weight})" if weight else "")
                    + (f"; third-party {perf['third_party'].get('requests')} requests {_kb(perf['third_party'].get('bytes'))}"
                       if (perf.get("third_party") or {}).get("requests") else ""))
        if perf.get("largest"):
            rows.append("  largest: " + "; ".join(f"{r.get('url')} {_kb(r.get('bytes'))}" for r in perf["largest"][:4]))
        if perf.get("slowest"):
            rows.append("  slowest: " + "; ".join(f"{r.get('url')} {_ms(r.get('ms'))}" for r in perf["slowest"][:3]))
        flags = []
        if perf.get("render_blocking"):
            flags.append(f"{perf['render_blocking']} render-blocking script(s) in <head>")
        if perf.get("mixed_content"):
            flags.append(f"{perf['mixed_content']} http resource(s) on an https page")
        if flags:
            rows.append("  " + "; ".join(flags))
        sections["performance"] = rows
    seo = result.get("seo") or {}
    if seo:
        bits = [f"title {seo.get('title')!r} ({seo.get('title_length')} chars)" if seo.get("title") else "NO TITLE",
                f"lang {seo.get('lang')}" if seo.get("lang") else "no lang attribute",
                f"meta description {seo.get('description_length')} chars" if seo.get("description_length") else "no meta description",
                "viewport meta" if seo.get("viewport_meta") else "no viewport meta",
                f"{seo.get('h1_count')} h1" + (f" ({seo.get('h1')!r})" if seo.get("h1") and seo.get("h1_count") == 1 else "")]
        if seo.get("canonical"):
            bits.append(f"canonical {seo['canonical'][:80]}")
        if seo.get("robots"):
            bits.append(f"robots {seo['robots']}")
        if seo.get("heading_skips"):
            bits.append("heading levels skip " + ", ".join(seo["heading_skips"]))
        bits.append(f"{seo.get('images', 0)} images" + (f", {seo['images_without_alt']} without alt" if seo.get("images_without_alt") else "")
                    + (f", {seo['images_without_dimensions']} without dimensions" if seo.get("images_without_dimensions") else ""))
        links = seo.get("links") or {}
        if links:
            bits.append(f"{links.get('same_origin', 0)} same-origin + {links.get('external', 0)} external links"
                        + (f", {links['target_blank_without_noopener']} target=_blank without noopener" if links.get("target_blank_without_noopener") else "")
                        + (f", {links['without_text']} without text" if links.get("without_text") else ""))
        if not seo.get("og_title"):
            bits.append("no og:title")
        if not seo.get("favicon"):
            bits.append("no favicon link")
        dom = result.get("dom") or {}
        if dom:
            bits.append(f"{dom.get('nodes', 0)} DOM nodes" + (f", {dom['shadow_roots']} shadow roots" if dom.get("shadow_roots") else "")
                        + (f", {dom['iframes']} iframes" if dom.get("iframes") else ""))
        sections["seo"] = ["Page basics: " + "; ".join(bits) + "."]
    links = result.get("links") or {}
    if links:
        broken = links.get("broken") or []
        row = (f"Links: {links.get('checked', 0)} same-origin links checked (HEAD, no cookies)"
               + (f", {links['skipped_acting']} skipped as actions" if links.get("skipped_acting") else "")
               + (f", {links['unchecked_same_origin']} more not checked (cap {LINK_CHECK_CAP})" if links.get("unchecked_same_origin") else "")
               + f"; {links.get('external', 0)} external links not checked")
        rows = [row + (f": {len(broken)} broken" if broken else ": none broken") + "."]
        for b in broken[:12]:
            status = b.get("error") or f"HTTP {b.get('status')}"
            rows.append(f"  {b.get('url')} → {status}" + (f" ({b.get('text')!r})" if b.get("text") else ""))
        if len(broken) > 12:
            rows.append(f"  … {len(broken) - 12} more")
        sections["links"] = rows
    console = result.get("console") or {}
    if console and (console.get("errors") or console.get("failed_requests")):
        rows = [f"Console: {console.get('errors', 0)} errors, {console.get('failed_requests', 0)} failed requests"
                + (f" ({console['third_party_failed']} third-party)" if console.get("third_party_failed") else "")
                + " on this page; browser_extract read=console / read=network lists them."]
        for text in console.get("first_errors") or []:
            rows.append(f"  {text}")
        sections["console"] = rows
    order = ["accessibility", "links", "console", "performance", "seo"]
    body = [line for name in order for line in sections.get(name, [])]
    next_step = ("Next: fix an element by its ref (browser_extract read=inspect target_ref=… shows its box and styles); "
                 "reload and run read=audit again to compare.")
    text = "\n".join([*lines, *body, next_step])
    if len(text) <= budget:
        return text
    # Over budget: drop the deepest detail first, whole sections last.
    for name in ("seo", "performance", "links", "console"):
        if name in sections and len(text) > budget:
            sections[name] = sections[name][:1]
            body = [line for n in order for line in sections.get(n, [])]
            text = "\n".join([*lines, *body, next_step])
    if len(text) > budget and "accessibility" in sections:
        kept = [sections["accessibility"][0]]
        for line in sections["accessibility"][1:]:
            if len("\n".join([*lines, *kept, line])) > budget - 400:
                kept.append("  … more violations cut by the output budget; pass checks=['accessibility'] to see them all")
                break
            kept.append(line)
        sections["accessibility"] = kept
        body = [line for n in order for line in sections.get(n, [])]
        text = "\n".join([*lines, *body, next_step])
    return text[:budget]
