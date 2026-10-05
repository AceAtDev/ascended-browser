"""Shared click fallbacks, bbox snapshots, and Set-of-Mark screenshots for browser automation."""

from __future__ import annotations

import asyncio
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
import io
import hashlib
import inspect
import json
import math
import random
import secrets
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ascended_browser._app.browser_navigation_hints import (
    collect_open_tabs,
    detect_blocking_overlays,
    dismiss_blocking_overlays,
    is_click_intercept_error,
)
from ascended_browser._app.browser_shadow_dom import SHADOW_DOM_JS
from ascended_browser._app.browser_viewport_content import VIEWPORT_CONTENT_JS

class StaleRefError(ValueError):
    """An opaque element ref is unknown, expired, or belongs to another tab."""


@dataclass
class ScopedRefRegistry:
    """Keep observation refs isolated across concurrent workspaces and tabs."""

    selectors: dict[tuple[str, str, str, str], str] = field(default_factory=dict)
    identities: dict[tuple[str, str, str, str], dict[str, Any]] = field(default_factory=dict)
    latest: dict[tuple[str, str], str] = field(default_factory=dict)

    def replace(self, workspace_id: str, tab_id: str, observation_id: str,
                selectors: dict[str, str], identities: dict[str, dict[str, Any]]) -> None:
        scope = (workspace_id, tab_id)
        # Selectors are dispatch authority, so retain an older selector only
        # while the exact live node token is still present. Semantic identity
        # is not authority: retain a bounded history of it so a replaced node
        # can be re-resolved against a fresh observation without asking the
        # model to invent a selector.
        live_nodes = {i.get("node_token") for i in identities.values() if i.get("node_token")}
        # Generations are counted from identity history, which outlives an
        # invalidation (see invalidate_tab), not from the selectors it drops.
        generations = list(dict.fromkeys(k[2] for k in self.identities if k[:2] == scope))[-7:]
        retained_selectors = {
            k for k, identity in self.identities.items()
            if k[:2] != scope or (
                k[2] in generations and identity.get("node_token") in live_nodes
                and identity.get("node_token")
            )
        }
        retained_identities = {
            k for k in self.identities
            if k[:2] != scope or k[2] in generations
        }
        self.selectors = {
            k: v for k, v in self.selectors.items() if k in retained_selectors
        }
        self.identities = {
            k: v for k, v in self.identities.items() if k in retained_identities
        }
        self.latest[scope] = observation_id
        for ref, selector in selectors.items():
            key = (workspace_id, tab_id, observation_id, ref)
            self.selectors[key] = selector
            self.identities[key] = dict(identities.get(ref) or {})

    def invalidate_tab(self, workspace_id: str, tab_id: str) -> None:
        """Revoke dispatch authority for every ref of a tab; keep its identity.

        Selectors are authority and go. Identity is only a hint for re-finding
        the same element: a tab restored from sleep or a window resize (the
        live view's panel) invalidated every ref, and with the identities gone a
        stale ref could not heal — three radios named "Radio button" were
        unaddressable although each kept its id. Healing still requires the
        same page and a unique match.
        """
        prefix = (workspace_id, tab_id)
        self.selectors = {k: v for k, v in self.selectors.items() if k[:2] != prefix}
        self.latest.pop(prefix, None)

    def _key(self, workspace_id: str, tab_id: str, ref: str,
             observation_id: str | None = None) -> tuple[str, str, str, str] | None:
        obs = observation_id or self.latest.get((workspace_id, tab_id))
        return (workspace_id, tab_id, obs, ref) if obs else None

    def selector(self, workspace_id: str, tab_id: str, ref: str,
                 observation_id: str | None = None) -> str | None:
        key = self._key(workspace_id, tab_id, ref, observation_id)
        return self.selectors.get(key) if key else None

    def identity(self, workspace_id: str, tab_id: str, ref: str,
                 observation_id: str | None = None) -> dict[str, Any] | None:
        key = self._key(workspace_id, tab_id, ref, observation_id)
        value = self.identities.get(key) if key else None
        if value is None and observation_id is None:
            # Historical identity may inform one bounded re-resolution, but it
            # never restores the old selector or authorizes dispatch.
            for candidate_key, candidate in reversed(list(self.identities.items())):
                if candidate_key[:2] == (workspace_id, tab_id) and candidate_key[3] == ref:
                    value = candidate
                    break
        return dict(value) if value else None

    def current_candidates(self, workspace_id: str, tab_id: str,
                           observation_id: str | None = None) -> list[dict[str, Any]]:
        """Complete semantics for the current generation, never display prefixes.

        This uses the existing ref owner, so stale generations and another tab's
        candidates cannot enter a recovery scan. Values are not retained here.
        """
        current = self.latest.get((workspace_id, tab_id))
        if not current or (observation_id and observation_id != current):
            return []
        return [
            {**identity, "ref": key[3]}
            for key, identity in self.identities.items()
            if key[:3] == (workspace_id, tab_id, current)
        ]


_REFS = ScopedRefRegistry()
_LEGACY_WORKSPACE = "__legacy__"
_LEGACY_TAB = "__legacy__"
_OPAQUE_REF_RE = re.compile(r"^e[0-9a-f]{10,40}$", re.I)
#: role+index tokens a model invents when it does not copy the real ref
#: ("link_17", "button_3"). Never a valid selector, so never worth trying.
_ROLE_INDEX_REF_RE = re.compile(r"^[a-z]+_\d+$", re.I)
#: Human-readable labels passed where a ref belongs ("Try it", "Sign in").
#: Words and spaces with none of the punctuation CSS needs to mean anything.
_PROSE_REF_RE = re.compile(r"^[A-Za-z0-9]+(?:[ '’\-][A-Za-z0-9]+)+$")
#: Every element name a bare descendant selector could be built from, so
#: `ul li` and `form input` are not mistaken for prose.
_HTML_TAGS = frozenset("""
a abbr address area article aside audio b base bdi bdo blockquote body br button
canvas caption cite code col colgroup data datalist dd del details dfn dialog div
dl dt em embed fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 head
header hgroup hr html i iframe img input ins kbd label legend li link main map
mark menu meta meter nav noscript object ol optgroup option output p param
picture pre progress q rp rt ruby s samp script search section select slot small
source span strong style sub summary sup table tbody td template textarea tfoot
th thead time title tr track u ul var video wbr svg path g circle rect
""".split())


def _is_prose_ref(ref: str) -> bool:
    """True when a ref is a human label rather than a selector."""
    if not _PROSE_REF_RE.fullmatch(ref):
        return False
    # A descendant selector is words and spaces too — but every word is a tag.
    return not all(part.lower() in _HTML_TAGS for part in ref.split())
_REF_SCOPE: ContextVar[tuple[str, str]] = ContextVar(
    "browser_ref_scope", default=(_LEGACY_WORKSPACE, _LEGACY_TAB)
)


@contextmanager
def bind_ref_scope(workspace_id: str, tab_id: str):
    """Bind opaque-ref lookup to one async worker/tab execution context."""
    token = _REF_SCOPE.set((str(workspace_id), str(tab_id)))
    try:
        yield
    finally:
        _REF_SCOPE.reset(token)


def _resolved_scope(workspace_id: str | None, tab_id: str | None) -> tuple[str, str]:
    bound_workspace, bound_tab = _REF_SCOPE.get()
    return workspace_id or bound_workspace, tab_id or bound_tab

INTERACTIVE_ELEMENTS_BBOX_SCRIPT = """() => {
  /*SHADOW_DOM*/
  // WeakMap identity survives scans but not document replacement or cloning.
  const identityKey = Symbol.for('odysseus.browser.nodes');
  const nodes = window[identityKey] || (window[identityKey] = new WeakMap());
  function cssIdent(value) {
    return (window.CSS && CSS.escape) ? CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&");
  }
  function selectorFor(el) {
    if (el.id) return '#' + cssIdent(el.id);
    if (el.name && ['INPUT','TEXTAREA','SELECT'].includes(el.tagName)) {
      return el.tagName.toLowerCase() + '[name="' + String(el.name).replaceAll('"', '\\"') + '"]';
    }
    const parts = [];
    let cur = el;
    while (cur && cur !== document.documentElement && parts.length < 6) {
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(' > ');
  }
  // Absolute path from the document root, so it identifies exactly one node.
  // selectorFor() stops after 6 levels and joins with '>', which produces an
  // UNANCHORED css selector: 'div:nth-of-type(2) > ul > li > a' matches
  // anywhere in the page. On a page with repeated structure (nav lists, cards,
  // a duplicated mobile menu) `.first` then resolves to a hidden copy, and the
  // click spends its whole actionability timeout on an element that can never
  // be clicked. An absolute xpath cannot be ambiguous.
  function xpathFor(el) {
    if (!el || el.nodeType !== 1) return '';
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1) {
      const tag = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (!parent) { parts.unshift(tag); break; }
      const same = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
      parts.unshift(same.length > 1 ? tag + '[' + (same.indexOf(cur) + 1) + ']' : tag);
      cur = parent;
    }
    return '/' + parts.join('/');
  }
  function rendered(el) {
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
  }
  function hitTarget(el) {
    // A form control can be styled invisible and still be operated: the label
    // is what a user clicks, and the control follows. Workday's facet
    // checkboxes are opacity:0 boxes behind "Full time (808)", so asking only
    // whether the input itself renders hid every filter on the page.
    // An input parked off the page (lichess's colour picker sits at
    // left:-99999px) or shrunk to a 1px screen-reader box also renders, and
    // is equally operated through its label.
    const box = el.getBoundingClientRect();
    const parked = box.right + scrollX <= 0 || box.bottom + scrollY <= 0
      || (box.width <= 1 && box.height <= 1);
    if (rendered(el) && !parked) return null;
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'select' && tag !== 'textarea') return null;
    const own = (el.labels && el.labels.length ? Array.from(el.labels) : [])
      .concat(el.id ? Array.from(odyQueryAll(el.getRootNode(), 'label[for="' + CSS.escape(el.id) + '"]')) : [])
      .concat(el.closest('label') ? [el.closest('label')] : []);
    for (const label of own) {
      if (label && rendered(label)) return label;
    }
    return null;
  }
  function operable(el) {
    return rendered(el) || hitTarget(el) !== null;
  }
  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth;
  }
  function labelFor(el) {
    const labelledBy = (el.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {
      const value = labelledBy.split(/\\s+/).map(id => odyById(el, id)?.innerText || '').join(' ').replace(/\\s+/g, ' ').trim();
      if (value) return value.slice(0, 220);
    }
    if (el.id) {
      try {
        const explicit = el.getRootNode().querySelector('label[for="' + cssIdent(el.id) + '"]');
        const value = ownLabelText(explicit);
        if (value) return value.slice(0, 220);
      } catch (e) {}
    }
    const wrapping = ownLabelText(el.closest('label'));
    if (wrapping) return wrapping.slice(0, 220);
    // <label>Username</label><input>: a label element that names no control
    // (no for, nothing inside) directly before this one is its label to any
    // reader, even though no association was declared.
    const prev = el.previousElementSibling;
    if (prev && prev.tagName === 'LABEL' && !prev.htmlFor
        && !prev.querySelector('input, select, textarea, button')) {
      return ownLabelText(prev).slice(0, 220);
    }
    return '';
  }
  // A label's own words. A <label> wrapping a <select> also holds every
  // option's text; that is the control's content, not its name.
  function ownLabelText(label) {
    if (!label) return '';
    const parts = [];
    const walk = node => {
      node.childNodes.forEach(child => {
        if (child.nodeType === Node.TEXT_NODE) { parts.push(child.textContent); return; }
        if (child.nodeType !== Node.ELEMENT_NODE) return;
        if (child.matches('select, textarea, input, button, [role="listbox"], [role="combobox"], script, style')) return;
        walk(child);
      });
    };
    walk(label);
    return parts.join(' ').replace(/\\s+/g, ' ').trim();
  }
  function contextFor(el) {
    const dialog = el.closest('dialog,[role="dialog"],[role="alertdialog"]');
    if (dialog) {
      const aria = (dialog.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
      if (aria) return aria.slice(0, 220);
      const labelledBy = (dialog.getAttribute('aria-labelledby') || '').trim();
      if (labelledBy) {
        const labelled = labelledBy.split(/\\s+/).map(id => odyById(dialog, id)?.innerText || '').join(' ').replace(/\\s+/g, ' ').trim();
        if (labelled) return labelled.slice(0, 220);
      }
      const heading = dialog.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"]');
      const headingText = (heading?.innerText || heading?.textContent || '').replace(/\\s+/g, ' ').trim();
      if (headingText) return headingText.slice(0, 220);
    }
    // Prefer a container that actually has a heading. Forms often wrap controls
    // without a legend, while the section/article above holds "Contact draft".
    // A heading only names the container that owns it: a legend inside a nested
    // fieldset ("Newsletter topics") is not the heading of the whole form, and
    // using it labelled every other field of that form with the wrong group.
    const selectors = 'article,li,[role="listitem"],section,fieldset,form,.card,.job,.job-posting,.posting';
    let start = el;
    while (start) {
      const container = start.closest(selectors);
      if (!container) return '';
      const owned = Array.from(container.querySelectorAll('h1,h2,h3,h4,h5,h6,legend')).filter(node => {
        if (node.tagName === 'LEGEND') return node.parentElement === container;
        const owner = node.parentElement?.closest(selectors);
        if (owner === container || !owner || owner.contains(el)) return true;
        // A nested block holding its own controls is a sibling group; its
        // heading names that group, not this one.
        return !owner.querySelector('input,select,textarea,button,[role="checkbox"],[role="radio"],[role="combobox"]');
      });
      const preceding = owned.filter(node => node.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
      const heading = preceding[preceding.length - 1] || owned[0];
      if (heading) {
        return (heading.innerText || heading.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 220);
      }
      start = container.parentElement;
    }
    return '';
  }
  function roleFor(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'file') return 'file';
      // Button-type inputs are buttons (HTML-AAM). Reporting "Add to cart" or
      // a search "Go" as a textbox told the model to type into a button.
      if (t === 'submit' || t === 'reset' || t === 'button' || t === 'image') return 'button';
      return 'textbox';
    }
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'summary') return 'button';
    return tag;
  }
  function regionSummary(el) {
    if (!el) return null;
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    return {
      id: el.id || '',
      role: roleFor(el),
      label: labelFor(el) || el.getAttribute('aria-label') || '',
      text: text.slice(0, 1200),
      visible: rendered(el),
    };
  }
  function controlledRegionsFor(el) {
    return (el.getAttribute('aria-controls') || '').trim().split(/\\s+/)
      .filter(Boolean).slice(0, 4)
      .map(id => regionSummary(odyById(el, id)))
      .filter(Boolean);
  }
  function containingRegionFor(el) {
    return regionSummary(el.closest(
      '[role="tabpanel"],[role="dialog"],[role="alertdialog"],[role="region"],dialog,section,form'
    ));
  }
  const CONTROL_SELECTOR = 'a[href],button,input,select,textarea,summary,[role],[tabindex]:not([tabindex="-1"])';
  // Click targets with no semantics: a <span class="link"> or a <div> card
  // wired up by a JS handler, which a person spots by its pointer cursor.
  // Without these a "link" the page draws as a span could not be clicked
  // except through evaluate. Only the outermost pointer element counts, it
  // must carry short text of its own, and it may not wrap a real control.
  // Bounded, so a huge page pays for at most a few thousand style reads.
  const pointerTargets = [];
  {
    let inspected = 0;
    for (const el of document.body ? odyQueryAll(document.body, 'span,div,li,td,p,img,i,label,h1,h2,h3,h4,h5,h6') : []) {
      if (++inspected > 4000 || pointerTargets.length >= 150) break;
      if (el.matches(CONTROL_SELECTOR) || el.closest('a[href],button,[role="button"],[role="link"],label,[role="option"],[role="menuitem"]')) continue;
      if (el.tagName === 'LABEL') continue;  // a label clicks through to its control
      if (!el.getClientRects().length) continue;
      const style = getComputedStyle(el);
      if (style.cursor !== 'pointer' || style.visibility === 'hidden') continue;
      const parent = el.parentElement;
      if (parent && getComputedStyle(parent).cursor === 'pointer') continue;
      if (el.querySelector(CONTROL_SELECTOR)) continue;
      const label = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '')
        .replace(/\\s+/g, ' ').trim();
      if (!label || label.length > 80) continue;
      pointerTargets.push(el);
    }
  }
  const controls = odyQueryAll(document, CONTROL_SELECTOR).concat(pointerTargets)
    // Keep rendered controls from the whole document, not only the first
    // viewport. Playwright can scroll an observed locator into view itself;
    // omitting below-fold controls forced the model into slow screenshot-led
    // scrolling and made late-page CTAs effectively invisible.
    .filter(el => operable(el) || (el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'file'));
  const documentOrder = new Map(controls.map((el, index) => [el, index]));
  const structuredControl = el => {
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    return ['button', 'input', 'select', 'textarea'].includes(tag) ||
      ['button', 'checkbox', 'combobox', 'radio', 'searchbox', 'slider',
       'spinbutton', 'switch', 'textbox'].includes(role);
  };
  // Scrolling must make late controls observable even past the collection cap.
  // Repetitive navigation must not consume the entire cap and hide a form or
  // dialog later in the document. Preserve semantic controls first, then the
  // viewport and document order. This is a DOM capability rule, not task-text
  // routing; the model-facing projection still ranks the current viewport.
  const kept = controls.sort((a, b) =>
      Number(structuredControl(b)) - Number(structuredControl(a)) ||
      Number(inViewport(b)) - Number(inViewport(a)) ||
      documentOrder.get(a) - documentOrder.get(b))
    .slice(0, 500);
  // Controls sharing a role and a name ("Add" under Work Experience,
  // Education and Languages) are told apart by the named group around each.
  // Values that must never reach the model or the ref registry: secrets and
  // one-time codes the person typed, card data, hidden and file inputs. A
  // name derived from el.value carried a typed password into the outline.
  const sensitive = el => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['password', 'hidden'].includes(type)) return true;
    const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
    // Same tokens as browser_sensitive_state._SENSITIVE_AUTOCOMPLETE.
    return autocomplete.split(/\\s+/).some(token => [
      'current-password', 'new-password', 'one-time-code', 'cc-number', 'cc-csc',
      'cc-exp', 'cc-exp-month', 'cc-exp-year',
    ].includes(token));
  };
  // A file input's value is a fake path; its files are reported as file_names.
  const liveValue = el => (sensitive(el) || (el.getAttribute('type') || '').toLowerCase() === 'file'
    ? '' : (el.value || ''));
  // A control whose only content is an image is named by that image: its
  // alt/label/title, else its file name. Query strings and data: URLs can
  // carry tokens and are never used.
  const imageName = el => {
    const img = el.matches('img, [role="img"]') ? el : el.querySelector('img, [role="img"], svg[aria-label]');
    if (!img) return '';
    const named = img.getAttribute('alt') || img.getAttribute('aria-label') || img.getAttribute('title') || '';
    if (named.trim()) return named;
    const src = img.getAttribute('src') || '';
    if (!src || src.startsWith('data:')) return '';
    try {
      const file = new URL(src, location.href).pathname.split('/').filter(Boolean).pop() || '';
      return decodeURIComponent(file).replace(/\\.[a-z0-9]{2,5}$/i, '');
    } catch (e) { return ''; }
  };
  const nameOf = el => (el.innerText || odySlottedText(el) || liveValue(el) || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || imageName(el) || '')
    .replace(/\\s+/g, ' ').trim().slice(0, 220);
  const shared = new Map();
  kept.forEach(el => { const key = roleFor(el) + '|' + nameOf(el); shared.set(key, (shared.get(key) || 0) + 1); });
  // The name a reader is shown: an explicit label or aria-label before text.
  const displayNameOf = el => (labelFor(el) || el.getAttribute('aria-label') || el.getAttribute('placeholder') || nameOf(el) || '')
    .replace(/\\s+/g, ' ').trim().slice(0, 220);
  const sharedDisplay = new Map();
  kept.forEach(el => { const key = roleFor(el) + '|' + displayNameOf(el); sharedDisplay.set(key, (sharedDisplay.get(key) || 0) + 1); });
  // The visible text beside a control whose own name is missing or shared
  // (Formy: <input type=radio aria-label="Radio button"> College). Tools that
  // list controls without the page's text lose it; browser-use and Playwright
  // MCP keep it by printing text nodes in place.
  const isRealControl = n => n.nodeType === 1 && n.matches(
    'input:not([type="hidden"]),select,textarea,button,[role="button"],[role="combobox"],[role="checkbox"],[role="radio"],[role="textbox"],a[href]');
  const holdsControl = n => n.nodeType === 1 && (isRealControl(n) || !!n.querySelector(
    'input:not([type="hidden"]),select,textarea,button,[role="combobox"],[role="checkbox"],[role="radio"]'));
  const plain = n => (n.nodeType === 3 ? n.textContent : (n.innerText || '')).replace(/\\s+/g, ' ').trim();
  const holdsOtherField = (node, el) => Array.from(node.querySelectorAll(
    'input:not([type="hidden"]),select,textarea,[role="combobox"],[role="checkbox"],[role="radio"]'))
    .some(other => other !== el && !el.contains(other) && !other.contains(el));
  function nearbyFor(el, role) {
    const scan = (dir) => {
      let n = el;
      for (let steps = 0; steps < 4; steps += 1) {
        n = dir > 0 ? n.nextSibling : n.previousSibling;
        if (!n || holdsControl(n)) return '';
        const text = plain(n);
        if (text) return text;
      }
      return '';
    };
    if (['radio', 'checkbox', 'switch'].includes(role)) {
      const beside = scan(1) || scan(-1);
      if (beside) return beside.slice(0, 80);
    }
    // Anything else is labelled from outside its own box, above or to the left:
    // text inside the widget (react-select's placeholder) describes the value.
    let box = el.getBoundingClientRect();
    for (let n = el.parentElement, d = 0; n && d < 8; n = n.parentElement, d += 1) {
      const r = n.getBoundingClientRect();
      if (r.height > 60 || r.width > 900 || holdsOtherField(n, el)) break;
      // A child beside the field that carries text makes this a layout row
      // (label column + field column), not the widget itself.
      const beside = Array.from(n.children).find(child => !child.contains(el) && plain(child));
      if (beside) {
        const r0 = beside.getBoundingClientRect();
        if (r0.right <= box.left + 2 || r0.bottom <= box.top + 2) break;
      }
      box = r;
    }
    const outside = node => {
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      return r.height > 0 && (r.bottom <= box.top + 2 || r.right <= box.left + 2);
    };
    let node = el;
    for (let depth = 0; depth < 8 && node.parentElement && node.parentElement !== document.body; depth += 1) {
      node = node.parentElement;
      if (holdsOtherField(node, el)) return '';  // reached a block holding other fields
      let prev = node.previousElementSibling;
      for (let k = 0; prev && k < 2; k += 1, prev = prev.previousElementSibling) {
        if (holdsControl(prev)) break;
        const text = plain(prev);
        if (text && text.length <= 80 && outside(prev)) return text;
      }
    }
    return '';
  }
  function namedGroupFor(el) {
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      if (!node.matches('[role="group"],[role="region"],[role="radiogroup"],fieldset,section')) continue;
      const labelledBy = (node.getAttribute('aria-labelledby') || '').trim();
      const name = (node.getAttribute('aria-label')
        || (labelledBy ? labelledBy.split(/\\s+/).map(id => odyById(node, id)?.innerText || '').join(' ') : '')
        || (node.matches('fieldset') ? (node.querySelector('legend')?.innerText || '') : ''))
        .replace(/\\s+/g, ' ').trim();
      if (name) return name.slice(0, 120);
    }
    return '';
  }
  const elements = kept
    .map((el, index) => {
      const r = el.getBoundingClientRect();
      const text = (el.innerText || odySlottedText(el) || liveValue(el) || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || imageName(el) || '')
        .replace(/\\s+/g, ' ').trim().slice(0, 220);
      const role = roleFor(el);
      const ref = text ? (role + ':' + text) : '';
      const form = el.closest('form');
      const searchContainer = el.closest('[role="search"],search');
      const cx = Math.round(r.left + r.width / 2);
      const cy = Math.round(r.top + r.height / 2);
      // Stagehand executes the exact element it observed instead of asking a
      // later CSS query to rediscover it. Mirror that behavior locally by
      // attaching a document-scoped locator to the original live node.
      // This avoids nth-of-type selectors resolving to a hidden sibling.
      const fallbackSelector = selectorFor(el);
      let snapshotRef = nodes.get(el);
      if (!snapshotRef) {
        const bytes = new Uint32Array(4);
        crypto.getRandomValues(bytes);
        snapshotRef = Array.from(bytes, n => n.toString(16).padStart(8, '0')).join('');
        nodes.set(el, snapshotRef);
      }
      try { el.setAttribute('data-odysseus-browser-ref', snapshotRef); } catch (e) {}
      return {
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        role,
        ref,
        node_token: snapshotRef,
        selector: '[data-odysseus-browser-ref="' + snapshotRef + '"]',
        fallback_selector: fallbackSelector,
        xpath: xpathFor(el),
        id: el.id || '',
        name: el.getAttribute('name') || '',
        aria_label: el.getAttribute('aria-label') || '',
        aria_expanded: el.getAttribute('aria-expanded') || '',
        haspopup: el.getAttribute('aria-haspopup') || '',
        aria_controls: el.getAttribute('aria-controls') || '',
        aria_activedescendant: el.getAttribute('aria-activedescendant') || '',
        aria_autocomplete: el.getAttribute('aria-autocomplete') || '',
        controlled_regions: controlledRegionsFor(el),
        containing_region: containingRegionFor(el),
        placeholder: el.getAttribute('placeholder') || '',
        title_attr: el.getAttribute('title') || '',
        label: labelFor(el),
        // A radio or checkbox is named by its group ("Size": S, M) before any
        // heading around it; other controls use the group only to tell
        // same-named twins apart.
        context: ['radio', 'checkbox'].includes(role)
          ? (namedGroupFor(el) || contextFor(el))
          : (contextFor(el) || (shared.get(role + '|' + text) > 1 ? namedGroupFor(el) : '')),
        nearby: ['textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'switch', 'spinbutton', 'listbox', 'slider'].includes(role)
          && (sharedDisplay.get(role + '|' + displayNameOf(el)) > 1 || !displayNameOf(el))
          ? nearbyFor(el, role) : '',
        value: liveValue(el),
        sensitive: sensitive(el),
        file_names: el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'file'
          ? Array.from(el.files || []).map(file => String(file.name || '')).filter(Boolean)
          : [],
        multiple: !!el.multiple,
        // A multi-select's selectedIndex is only its first choice: reporting
        // that alone read Python+Docker back as "Python".
        selected_text: el.tagName === 'SELECT' && el.selectedIndex >= 0
          ? Array.from(el.multiple ? el.selectedOptions : [el.options[el.selectedIndex]])
              .map(option => ((option && option.textContent) || '').replace(/\\s+/g, ' ').trim())
              .filter(Boolean).join(', ').slice(0, 220)
          : '',
        selected_options: el.tagName === 'SELECT' && el.multiple
          ? Array.from(el.selectedOptions).slice(0, 40)
              .map(option => (option.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80))
          : [],
        // A native checkbox/radio states itself through .checked; an ARIA one
        // (div role=checkbox, a switch, a toggle button) only through its
        // attributes, and reading .checked there reported every one unchecked.
        checked: el.tagName === 'INPUT' && ['checkbox', 'radio'].includes((el.type || '').toLowerCase())
          ? !!el.checked
          : el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-pressed') === 'true',
        // Only the third state needs its own field; true/false live in checked.
        aria_checked: el.indeterminate || el.getAttribute('aria-checked') === 'mixed'
          || el.getAttribute('aria-pressed') === 'mixed' ? 'mixed' : '',
        disabled: !!el.disabled,
        required: !!el.required,
        // The page's verdict on the value (aria-invalid) and the message the
        // field points at; hints in aria-describedby are read only then.
        invalid: el.getAttribute('aria-invalid') === 'true',
        error: el.getAttribute('aria-invalid') === 'true'
          ? [el.getAttribute('aria-errormessage'), el.getAttribute('aria-describedby')].join(' ').trim()
              .split(/\\s+/).filter(Boolean).map(id => (odyById(el, id) || {}).innerText || '')
              .join(' ').replace(/\\s+/g, ' ').trim().slice(0, 160)
          : '',
        text,
        href: el.getAttribute('href') || '',
        target: el.getAttribute('target') || '',
        in_form: !!form,
        form_action: form ? (form.getAttribute('action') || '') : '',
        form_method: form ? (form.getAttribute('method') || '') : '',
        form_role: form ? (form.getAttribute('role') || '') : '',
        in_search: !!searchContainer || !!(form && (
          (form.getAttribute('role') || '').toLowerCase() === 'search' ||
          /search|filter|query|jobs?/i.test(form.getAttribute('action') || '')
        )),
        visible: rendered(el) || hitTarget(el) !== null,
        // Say when the thing a user actually clicks is the label, so acting on
        // it aims there instead of at a zero-opacity box.
        hit_via: hitTarget(el) ? 'label' : '',
        in_viewport: inViewport(hitTarget(el) || el),
        document_order: documentOrder.get(el),
        option_texts: el.tagName === 'SELECT'
          ? Array.from(el.options || []).slice(0, 80).map(option => (option.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120))
          : [],
        options: el.tagName === 'SELECT'
          ? Array.from(el.options || []).slice(0, 80).map(option => ({
              text: (option.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
              value: String(option.value || '').slice(0, 240),
            }))
          : [],
        bbox: { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) },
        center: { x: cx, y: cy },
      };
    });
  /*VIEWPORT_CONTENT*/
  return {elements, total: controls.length, content_blocks: contentBlocks,
    scroll_containers: scrollContainers, content_complete: readingComplete,
    viewport_revision: viewportSignature()};
}"""
INTERACTIVE_ELEMENTS_BBOX_SCRIPT = INTERACTIVE_ELEMENTS_BBOX_SCRIPT.replace(
    "/*VIEWPORT_CONTENT*/", VIEWPORT_CONTENT_JS,
).replace("/*SHADOW_DOM*/", SHADOW_DOM_JS)


def _looks_like_css(ref: str) -> str:
    """True when a bare string is unambiguously a CSS selector."""
    if ref[:1] in {"#", ".", "["}:
        return True
    if any(ch in ref for ch in ">+~[]()*=|^$"):
        return True
    # `ul li`, `form input`, `button` — every token is an element name.
    return all(part.lower() in _HTML_TAGS for part in ref.split())


def parse_ref(ref: str, *, workspace_id: str | None = None,
              tab_id: str | None = None, observation_id: str | None = None,
              allow_selector: bool = True) -> str:
    """Convert a snapshot ref into a Playwright selector.

    A ref is assumed to BE a ref. Anything unrecognised is reported as such
    rather than passed to Playwright as a raw selector: `locator("Try it")`,
    `locator("bw-1")` and `locator("5c31c1ee5b2")` are all valid CSS that match
    nothing, so falling through cost a full actionability timeout — and the
    per-owner lock with it — to discover what is knowable here for free.
    Raw selectors are an internal diagnostic capability only. Agent-facing
    actions must use a scoped opaque ref so authority, frame ownership and
    semantic recovery remain enforceable.
    """
    ref = str(ref or "").strip()
    workspace_id, tab_id = _resolved_scope(workspace_id, tab_id)
    if not ref:
        raise StaleRefError(
            "No element ref was given. Observe the tab and pass ref=<the e\u2026 "
            "token of the element you mean>."
        )
    selector = _REFS.selector(workspace_id, tab_id, ref, observation_id)
    if selector:
        return selector
    if ref.isdigit():
        raise ValueError(
            f"{ref!r} is a display number, not an element ref. "
            f"Use the explicit ref=e\u2026 token from the snapshot."
        )
    if allow_selector:
        if ref.startswith(("css=", "xpath=", "text=", "role=", "id=")):
            return ref
        if ref.startswith(("/", "(")):
            return "xpath=" + ref
        if ":" in ref and not ref.startswith(("http", "//")):
            role, name = ref.split(":", 1)
            escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
            return f"role={role}[name='{escaped_name}']"
        if _looks_like_css(ref):
            return ref
    raise StaleRefError(
        f"{ref!r} is not a usable element ref for this tab. Refs are the exact "
        f"ref=e\u2026 tokens from the most recent browser_observe of THIS tab \u2014 not "
        f"labels, not row numbers, not refs from an earlier observation or a "
        f"different tab. The browser will re-resolve a stale observed target "
        f"when its semantic identity is unique; otherwise observe the tab and "
        f"use a fresh ref."
    )


def ref_identity(ref: str, *, workspace_id: str | None = None,
                 tab_id: str | None = None, observation_id: str | None = None) -> dict[str, Any] | None:
    """Return identity metadata for a scoped, retained observation ref."""
    workspace_id, tab_id = _resolved_scope(workspace_id, tab_id)
    return _REFS.identity(workspace_id, tab_id, str(ref or "").strip(), observation_id)


def current_ref_candidates(*, workspace_id: str, tab_id: str,
                           observation_id: str | None = None) -> list[dict[str, Any]]:
    return _REFS.current_candidates(workspace_id, tab_id, observation_id)


def frame_for_ref(page, ref: str, *, workspace_id: str | None = None,
                  tab_id: str | None = None, observation_id: str | None = None):
    """The document a ref lives in — the page itself, or one of its iframes.

    `page.locator()` searches the top-level document only; it does not descend
    into iframes. The observer has always walked `page.frames`, so a form inside
    an embedded application widget (Greenhouse, Lever, Workday, a payment
    frame) produced perfectly good refs that no click or fill could ever
    resolve — the worst possible shape, because it looks like it should work.
    The frame each ref came from is already recorded in its identity; this is
    what reads it back.

    `frame_index` alone is not enough: frames load, unload and re-order between
    the observation and the action, so the recorded url has to agree. An
    absolute xpath is scoped to its own document, which makes hitting the wrong
    frame quietly hit the wrong element.
    """
    identity = ref_identity(
        ref, workspace_id=workspace_id, tab_id=tab_id, observation_id=observation_id,
    ) or {}
    index = int(identity.get("frame_index") or 0)
    if index <= 0:
        return page  # main document, or a raw selector the caller typed
    frames = list(getattr(page, "frames", []) or [])
    wanted = str(identity.get("frame_url") or "")
    wanted_name = str(identity.get("frame_name") or "")

    def url_of(frame) -> str:
        return str(getattr(frame, "url", "") or "")

    def matches(frame) -> bool:
        name = getattr(frame, "name", "")
        if callable(name):
            name = name()
        return bool(wanted and url_of(frame) == wanted and (not wanted_name or str(name or "") == wanted_name))

    matching = [frame for frame in frames if matches(frame)]
    if len(matching) == 1:
        return matching[0]
    if index < len(frames) and matches(frames[index]) and identity.get("node_token"):
        # resolve_live_target validates the exact node token before an effect.
        return frames[index]
    raise StaleRefError(
        f"{ref!r} was found inside an embedded frame ({wanted or 'unknown url'}) "
        f"that is missing or ambiguous on this page. Observe the tab again and use a fresh ref."
    )


def resolve_target(page, ref: str, *, workspace_id: str | None = None,
                   tab_id: str | None = None, observation_id: str | None = None,
                   allow_selector: bool = False):
    """`(root, selector)` — what to search, and what to search for.

    Every element interaction goes through this so that none of them can forget
    the frame and silently look in the wrong document.
    """
    selector = parse_ref(
        ref, workspace_id=workspace_id, tab_id=tab_id, observation_id=observation_id,
        allow_selector=allow_selector,
    )
    root = frame_for_ref(
        page, ref, workspace_id=workspace_id, tab_id=tab_id, observation_id=observation_id,
    )
    return root, selector


async def resolve_live_target(page, ref: str, *, allow_selector: bool = False, **scope):
    """Resolve and validate original node identity before dispatching an action."""
    root, selector = resolve_target(page, ref, allow_selector=allow_selector, **scope)
    identity = ref_identity(ref, **scope) or {}
    token = identity.get("node_token")
    if token:
        from ascended_browser.runtime.settings import get_setting

        # ``locator.count()`` accepts no timeout and inherits none, so a wedged
        # page hangs it while the caller already holds the per-owner action lock.
        # That reads to every other tab and the live view as the whole browser
        # hanging. Bound the probe and fail as a stale ref the model can recover.
        try:
            probe_timeout = float(
                get_setting("browser_workspace_probe_timeout_seconds", 5) or 5
            )
        except Exception:
            probe_timeout = 5.0
        locator = root.locator(selector)
        try:
            count = await asyncio.wait_for(locator.count(), timeout=probe_timeout)
        except asyncio.TimeoutError as exc:
            raise StaleRefError(
                "Observed element did not answer in time; observe the tab again."
            ) from exc
        if count != 1:
            raise StaleRefError("Observed element was removed or duplicated; observe the tab again.")
        valid = await locator.evaluate(
            "(el, token) => el.isConnected && window[Symbol.for('odysseus.browser.nodes')]?.get(el) === token",
            token, timeout=3000,
        )
        if not valid:
            raise StaleRefError("Observed element was replaced; observe the tab again.")
    return root, selector


# Firefox (Camoufox) never fires `change` for a value set by Playwright's
# fill(): not on blur, not on a real Tab. Chromium fires it on blur. A form
# listening for change (plain JS, jQuery, Angular updateOn:'blur', many
# sign-in pages) never saw what the agent typed. Leave the field and dispatch
# change only when the engine did not, so no engine delivers it twice.
COMMIT_EDIT_SCRIPT = """(el, refocus) => {
  if (document.activeElement !== el) return false;
  let fired = false;
  const seen = () => { fired = true; };
  el.addEventListener('change', seen);
  el.blur();
  el.removeEventListener('change', seen);
  if (!fired) el.dispatchEvent(new Event('change', {bubbles: true}));
  if (refocus) el.focus({preventScroll: true});
  return true;
}"""


async def commit_edit(locator, *, refocus: bool) -> None:
    """Leave a just-filled field so the page's change handlers run, bounded."""
    try:
        await asyncio.wait_for(locator.evaluate(COMMIT_EDIT_SCRIPT, refocus), timeout=1.5)
    except Exception:
        pass  # the caller's readback still decides


def invalidate_refs(*, workspace_id: str | None = None, tab_id: str | None = None) -> None:
    """Invalidate refs after a navigation, tab close, or ownership reset."""
    workspace_id, tab_id = _resolved_scope(workspace_id, tab_id)
    _REFS.invalidate_tab(workspace_id, tab_id)


async def viewport_metadata(page) -> dict[str, Any]:
    """Return viewport size and coordinate-space hints for screenshots."""
    width = height = 0
    scale = 1.0
    # Measure the page first. page.viewport_size is the emulated size the
    # context was created with; a native window resize (browser_viewport,
    # the live view) never updates it, so a 450px window reported 1280x979.
    try:
        dims = await page.evaluate("""() => ({
          width: window.innerWidth,
          height: window.innerHeight,
          deviceScaleFactor: window.devicePixelRatio || 1
        })""")
        if isinstance(dims, dict):
            width = int(dims.get("width") or 0)
            height = int(dims.get("height") or 0)
            scale = float(dims.get("deviceScaleFactor") or 1)
    except Exception:
        pass
    if not width or not height:
        try:
            vp = page.viewport_size
            if vp:
                width = int(vp.get("width") or 0)
                height = int(vp.get("height") or 0)
        except Exception:
            pass
    return {
        "width": width,
        "height": height,
        "device_scale_factor": scale,
        "coordinate_space": "viewport_css_pixels",
    }


async def frame_is_rendered(frame) -> bool:
    """Check every embedding document, including cross-origin frame ancestors."""
    current = frame
    while getattr(current, "parent_frame", None) is not None:
        host = await current.frame_element()
        try:
            shown = await host.evaluate("""el => {
              const r = el.getBoundingClientRect();
              if (r.width <= 0 || r.height <= 0 || r.right <= 0 || r.bottom <= 0) return false;
              for (let n = el; n; n = n.parentElement) {
                const s = getComputedStyle(n);
                if (s.display === 'none' || s.visibility === 'hidden' ||
                    s.visibility === 'collapse' || Number(s.opacity) === 0 ||
                    n.hidden || n.inert) return false;
                if (n !== el && /hidden|clip/.test(s.overflow + s.overflowX + s.overflowY)) {
                  const clip = n.getBoundingClientRect();
                  if (clip.width <= 0 || clip.height <= 0 || r.right <= clip.left ||
                      r.left >= clip.right || r.bottom <= clip.top || r.top >= clip.bottom) return false;
                }
              }
              return true;
            }""")
            if not shown:
                return False
        finally:
            await host.dispose()
        current = current.parent_frame
    return True


async def collect_interactive_elements(
    page,
    *,
    max_elements: int = 500,
    register_refs: bool = True,
    frame_timeout_seconds: float | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    tab_id: str | None = None,
) -> list[dict[str, Any]]:
    """Collect rendered interactive elements across the document with bbox metadata."""
    elements: list[dict[str, Any]] = []
    try:
        frames = list(getattr(page, "frames", []) or [])
    except Exception:
        frames = []
    if not frames:
        frames = [page]

    workspace_id, tab_id = _resolved_scope(workspace_id, tab_id)
    observation_id = secrets.token_hex(8)
    if coverage is not None:
        coverage.update(observation_id=observation_id, elements_complete=False,
                        frames_total=len(frames), frames_observed=0, controls_total=0)
    current_selectors: dict[str, str] = {}
    current_identities: dict[str, dict[str, Any]] = {}
    reading_items: list[dict[str, Any]] = []
    reading_complete = True
    complete = len(frames) <= 12
    total_controls = 0
    frames_observed = 0
    for index, frame in enumerate(frames[:12]):
        try:
            if not await asyncio.wait_for(
                frame_is_rendered(frame), timeout=frame_timeout_seconds or 3.0,
            ):
                frames_observed += 1
                continue
            evaluation = frame.evaluate(INTERACTIVE_ELEMENTS_BBOX_SCRIPT)
            frame_elements = await asyncio.wait_for(evaluation, timeout=frame_timeout_seconds) if frame_timeout_seconds else await evaluation
        except asyncio.TimeoutError:
            complete = False
            if diagnostics is not None:
                diagnostics.append({"phase": "observing_controls", "frame_index": index, "status": "timeout"})
            continue
        except Exception as exc:
            complete = False
            if diagnostics is not None:
                diagnostics.append({
                    "phase": "observing_controls", "frame_index": index,
                    "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                })
            continue
        frames_observed += 1
        if isinstance(frame_elements, dict):
            count = int(frame_elements.get("total") or 0)
            if coverage is not None and index == 0 and frame_elements.get("viewport_revision"):
                coverage["viewport_revision"] = frame_elements["viewport_revision"]
            reading_complete = reading_complete and bool(frame_elements.get("content_complete", True))
            auxiliary = [*(frame_elements.get("content_blocks") or []), *(frame_elements.get("scroll_containers") or [])]
            frame_elements = frame_elements.get("elements") or []
            complete = complete and count <= len(frame_elements)
            total_controls += max(count, len(frame_elements))
        else:
            # Legacy collectors expose no total count. Reaching their cap is
            # uncertain rather than proof that every control was collected.
            auxiliary = []
            total_controls += len(frame_elements or [])
            complete = complete and len(frame_elements or []) < 500
        frame_url = str(getattr(frame, "url", "") or "")
        frame_name = getattr(frame, "name", "")
        if callable(frame_name):
            try:
                frame_name = frame_name()
            except Exception:
                frame_name = ""
        for element in [*(frame_elements or []), *auxiliary]:
            if not isinstance(element, dict):
                continue
            if not element.get("_reading_kind") and len(elements) >= max_elements:
                continue
            element["frame_index"] = index
            element["frame_url"] = frame_url
            element["frame_name"] = str(frame_name or "")
            element["frame_scope"] = "main" if index == 0 else "iframe"
            transient_selector = str(element.get("selector") or "")
            # Real collectors supply a stable node selector. Older backends
            # retain snapshot-scoped xpath fallback and strict invalidation.
            xpath = str(element.get("xpath") or "")
            selector = (
                transient_selector if element.get("node_token")
                else f"xpath={xpath}" if xpath
                else str(element.get("fallback_selector") or transient_selector)
            )
            identity = {
                key: element[key]
                for key in ("role", "aria_label", "placeholder", "context", "nearby", "in_form", "visible",
                            # A recorded flow needs these to replay safely: a
                            # secret is never recorded, and a menu item's opener
                            # is re-opened when the item is missing.
                            "sensitive", "aria_expanded", "haspopup")
                if key in element
            }
            identity.update({
                "tag": str(element.get("tag") or "").lower(),
                "type": str(element.get("type") or "").lower(),
                "id": str(element.get("id") or ""),
                "name": str(element.get("name") or ""),
                "label": str(element.get("label") or ""),
                "frame_index": index,
                "frame_url": frame_url,
                "frame_name": str(frame_name or ""),
                "frame_scope": "main" if index == 0 else "iframe",
                "observation_id": observation_id,
                "node_token": str(element.get("node_token") or ""),
            })
            # Text can derive from input.value; it is not target identity for
            # editable controls and must never retain a password in the registry.
            if element.get("tag") not in {"input", "textarea", "select"}:
                identity["text"] = str(element.get("text") or "")
            token_material = json.dumps(
                [workspace_id, tab_id, index, frame_url, element["node_token"]] if element.get("node_token")
                else [observation_id, index, frame_url, selector, identity],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8", errors="ignore")
            opaque_ref = "e" + hashlib.sha1(token_material).hexdigest()[:10]
            if selector:
                current_selectors[opaque_ref] = selector
                current_identities.setdefault(opaque_ref, identity)
            element["legacy_ref"] = element.get("ref", "")
            element["ref"] = opaque_ref
            element["css_selector"] = selector
            element["observation_id"] = observation_id
            if element.get("_reading_kind"):
                reading_items.append(element)
            else:
                elements.append(element)
    if register_refs:
        _REFS.replace(workspace_id, tab_id, observation_id, current_selectors, current_identities)
    if coverage is not None:
        token_refs = {identity.get("node_token"): ref for ref, identity in current_identities.items()}
        coverage.update(
            content_blocks=[{**{k: item[k] for k in ("ref", "text", "media", "truncated", "frame_index", "frame_url") if k in item},
                             "control_refs": [token_refs[t] for t in item.get("control_tokens", []) if t in token_refs]}
                            for item in reading_items if item.get("_reading_kind") == "content"],
            scroll_containers=[{k: item[k] for k in ("ref", "text", "top", "max_top", "height", "document", "at_start", "at_end", "frame_index", "frame_url") if k in item}
                               for item in reading_items if item.get("_reading_kind") == "scroll"],
            content_complete=reading_complete and complete and frames_observed == len(frames),
        )
        coverage.update(elements_complete=complete and total_controls == len(elements) and frames_observed == len(frames),
                        controls_observed=len(elements), controls_total=total_controls,
                        frames_observed=frames_observed,
                        elements_omitted=max(0, total_controls - len(elements)))
    return elements


def _bbox_center(bbox: dict[str, Any] | None) -> dict[str, int] | None:
    if not bbox:
        return None
    x = int(bbox.get("x") or 0)
    y = int(bbox.get("y") or 0)
    w = int(bbox.get("width") or 0)
    h = int(bbox.get("height") or 0)
    if w < 1 or h < 1:
        return None
    return {"x": x + w // 2, "y": y + h // 2}


async def get_bounding_box_for_ref(page, ref: str) -> dict[str, Any]:
    """Resolve a snapshot ref to bounding box and click center."""
    root, selector = resolve_target(page, ref, allow_selector=True)
    locator = root.locator(selector).first
    bbox = await locator.bounding_box()
    if not bbox:
        raise ValueError(f"Element not found or not visible for ref: {ref}")
    center = _bbox_center(bbox)
    return {
        "ref": ref,
        "selector": selector,
        "bbox": {
            "x": int(bbox["x"]),
            "y": int(bbox["y"]),
            "width": int(bbox["width"]),
            "height": int(bbox["height"]),
        },
        "center": center,
    }


async def click_at_coordinates(
    page,
    x: int,
    y: int,
    *,
    button: Literal["left", "right", "middle"] = "left",
    double_click: bool = False,
    workspace_id: str | None = None,
    tab_id: str | None = None,
) -> str:
    """Click viewport coordinates after dismissing overlays."""
    ix, iy = int(x), int(y)
    try:
        inside_dialog = bool(await page.evaluate(
            "([x, y]) => { const hit = document.elementFromPoint(x, y);"
            " return !!hit && !!hit.closest(" + json.dumps(_DIALOG_SELECTOR) + "); }",
            [ix, iy],
        ))
    except Exception:
        inside_dialog = False
    if not inside_dialog:
        await dismiss_blocking_overlays(page)
    cue = "Double-clicking" if double_click else "Clicking"
    await _agent_cursor_event(page, workspace_id=workspace_id, tab_id=tab_id, kind="pointer_move", x=float(ix), y=float(iy), detail=cue)
    await _agent_cursor_event(page, workspace_id=workspace_id, tab_id=tab_id, kind="pointer_down", x=float(ix), y=float(iy), detail=cue)
    if double_click:
        await page.mouse.dblclick(ix, iy, button=button)
        action = "Double-clicked"
    else:
        await page.mouse.click(ix, iy, button=button)
        action = "Clicked"
    await _agent_cursor_event(page, workspace_id=workspace_id, tab_id=tab_id, kind="pointer_up", x=float(ix), y=float(iy), detail=cue)
    tag = ""
    try:
        tag = await page.evaluate(
            "([x, y]) => { const el = document.elementFromPoint(x, y); return el ? el.tagName.toLowerCase() : ''; }",
            [ix, iy],
        )
    except Exception:
        pass
    detail = f" at ({ix}, {iy})"
    if tag:
        detail += f" on <{tag}>"
    return f"{action}{detail}"


async def _mouse_click_bbox(page, bbox: dict[str, Any], *, button: str = "left", double_click: bool = False, workspace_id: str | None = None, tab_id: str | None = None, detail: str = "") -> None:
    center = _bbox_center(bbox)
    if not center:
        raise ValueError("invalid bounding box")
    jitter_x = random.randint(-2, 2)
    jitter_y = random.randint(-2, 2)
    x = center["x"] + jitter_x
    y = center["y"] + jitter_y
    await _agent_cursor_event(page, workspace_id=workspace_id, tab_id=tab_id, kind="pointer_move", x=float(x), y=float(y), detail=detail)
    await _agent_cursor_event(page, workspace_id=workspace_id, tab_id=tab_id, kind="pointer_down", x=float(x), y=float(y), detail=detail)
    if double_click:
        await page.mouse.dblclick(x, y, button=button)
    else:
        await page.mouse.click(x, y, button=button)
    await _agent_cursor_event(page, workspace_id=workspace_id, tab_id=tab_id, kind="pointer_up", x=float(x), y=float(y), detail=detail)


#: Sentinel for "this fallback did not produce a value", so a legitimately
#: falsy result is not mistaken for a failed step.
_FAILED = object()


class ClickBudgetExceeded(RuntimeError):
    """The click fallback chain ran out of time.

    Raised rather than silently returning so the caller reports a failed click
    the model can react to (re-observe, pick another ref) instead of believing
    the page was clicked.
    """


# A click aimed inside a dialog is the dialog's own interaction: dismissing
# "blocking overlays" first closed Workday's sign-in modal (a role=dialog) and
# took the target button with it, every time.
_DIALOG_SELECTOR = '[role="dialog"], [role="alertdialog"], dialog, [aria-modal="true"]'
_IN_DIALOG_SCRIPT = "el => !!el.closest(" + json.dumps(_DIALOG_SELECTOR) + ")"


class ClickTargetUnreachable(RuntimeError):
    """The target failed an actionability check; no input was sent."""


_ACTIONABILITY_BLOCKERS = (
    ("outside of the viewport", "it sits outside the visible page and cannot be scrolled into view"),
    ("element is not visible", "it is not visible"),
    ("element is not enabled", "it is disabled"),
    ("element is disabled", "it is disabled"),
)


def _actionability_blocker(exc: BaseException) -> str:
    """The last actionability state Playwright reported, in plain words."""
    log = str(exc).lower()
    found = [(log.rfind(needle), reason) for needle, reason in _ACTIONABILITY_BLOCKERS if needle in log]
    return max(found)[1] if found else ""


async def _attach_inspection(error: BaseException, locator, budget, *, workspace_id, tab_id) -> None:
    """Say why the target could not take the click, read while it still stands.

    Bounded by what is left of the click budget; a failure to inspect never
    changes the refusal itself.
    """
    try:
        from ascended_browser._app.browser_inspect import compact_inspection, inspect_locator

        remaining = min(2.5, max(0.3, float(budget.remaining())))
        candidates = (
            current_ref_candidates(workspace_id=workspace_id, tab_id=tab_id)
            if workspace_id and tab_id else []
        )
        error.inspect = compact_inspection(
            await inspect_locator(locator, candidates=candidates, timeout=remaining)
        )
    except Exception:
        return


async def _unreachable_next_step(locator, reason: str) -> str:
    """One next step, chosen from what the element is."""
    info = await _bounded(locator.evaluate("""el => ({
      tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
      checkable: el.matches('input[type=checkbox], input[type=radio]'),
      editable: el.matches('input:not([type=checkbox]):not([type=radio]):not([type=button])'
        + ':not([type=submit]):not([type=reset]):not([type=image]), textarea,'
        + ' [contenteditable=""], [contenteditable="true"]')
    })"""), 1.0, None) or {}
    if "disabled" in reason:
        return "Complete the required fields it depends on, then observe again."
    if info.get("checkable"):
        return "It is a checkbox or radio: use kind 'check' with checked: true on this ref."
    if info.get("editable") or info.get("role") == "spinbutton":
        return "It is typed into, not clicked: use kind 'fill' with the value on this ref."
    return "Observe the tab and act on the visible control that shows this value."


# Who owns the target's center point. `shield` accepts one widget pattern only:
# a transparent, textless overlay laid over the target's own small container
# (Workday's `click_filter` covers every button). It never accepts a dialog,
# a backdrop or anything larger than the target's immediate surroundings.
_HIT_TEST_SCRIPT = """el => {
  let r = el.getBoundingClientRect();
  if (r.bottom <= 0 || r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth) {
    // Off screen: a point outside the viewport belongs to nothing. Scrolling
    // here needs no animation frame, unlike Playwright's actionability wait.
    el.scrollIntoView({block: 'center', inline: 'nearest'});
    r = el.getBoundingClientRect();
  }
  // elementFromPoint on the document answers with a shadow host for any
  // point inside its shadow tree, so a component's own inner <button> read
  // as "covered by <sl-button>". Descend through open shadow roots to the
  // node that really owns the point, and compare across those boundaries.
  const topAt = (x, y) => {
    let h = document.elementFromPoint(x, y);
    for (let depth = 0; h && h.shadowRoot && depth < 10; depth += 1) {
      let inner = null;
      try { inner = h.shadowRoot.elementFromPoint(x, y); } catch (e) { break; }
      if (!inner || inner === h) break;
      h = inner;
    }
    return h;
  };
  const within = (outer, node) => {
    for (let n = node; n; n = n.parentNode || n.host || null) if (n === outer) return true;
    return false;
  };
  // The target's own label owns its point too: a styled checkbox is painted
  // by the label laid over it, and clicking the label is how it is operated.
  const ownLabel = h => { const l = h && h.closest && h.closest('label'); return !!l && (l.control === el || l.contains(el)); };
  const owns = (x, y) => { const h = topAt(x, y); return !!h && (h === el || within(el, h) || ownLabel(h)); };
  // The centre first, then other points of the target's own box: a chip or
  // badge laid over part of an input (Workday) leaves the rest clickable.
  for (const fy of [0.5, 0.25, 0.75]) {
    for (const fx of [0.5, 0.85, 0.15, 0.7, 0.3]) {
      if (owns(r.left + r.width * fx, r.top + r.height * fy)) {
        return {owns: true, dx: r.width * fx, dy: r.height * fy};
      }
    }
  }
  const hit = topAt(r.left + r.width / 2, r.top + r.height / 2);
  if (!hit) return {owns: false, owner: 'nothing at that point'};
  const describe = node => {
    const name = (node.getAttribute('aria-label') || node.innerText || '').trim().slice(0, 60);
    const role = node.getAttribute('role');
    return '<' + node.tagName.toLowerCase() + (role ? ' role=' + role : '') + '>' + (name ? ' "' + name + '"' : '');
  };
  const h = hit.getBoundingClientRect();
  const area = x => Math.max(1, x.width * x.height);
  let box = el.parentElement;
  for (let depth = 0; box && depth < 3 && !box.contains(hit); depth += 1) box = box.parentElement;
  const shield = !!box && within(box, hit) && !within(hit, el)
    && !(hit.innerText || '').trim()
    && !hit.matches('input, textarea, select, button, a, [role=dialog], [role=alertdialog], dialog')
    && area(h) <= area(r) * 2.5
    && h.left <= r.left + 2 && h.top <= r.top + 2 && h.right >= r.right - 2 && h.bottom >= r.bottom - 2;
  // A bar pinned to the top or bottom edge (a sticky header or footer) is
  // cleared by scrolling the target to another alignment; a cover in the
  // middle of the screen is not.
  let edge = false;
  for (let n = hit; n && n !== document.body; n = n.parentElement) {
    const pos = getComputedStyle(n).position;
    if (pos === 'fixed' || pos === 'sticky') {
      const b = n.getBoundingClientRect();
      edge = b.top <= 1 || b.bottom >= innerHeight - 1;
      break;
    }
  }
  return {owns: false, shield, edge, owner: describe(hit)};
}"""


#: The hit test's answer when no element is painted at the target's point (a
#: zero-size or clipped box). That is not a cover; the keyboard can still act.
_NOTHING_AT_POINT = "nothing at that point"


def _inside(origin: float, size: float, offset: float) -> int:
    """A whole-pixel coordinate inside ``[origin, origin + size)``.

    Firefox rounds fractional mouse coordinates, so a point near the edge of a
    small toggle could round onto its neighbour (Playwright picks integer
    points inside the quad for the same reason).
    """
    lo = math.ceil(origin)
    hi = math.floor(origin + size - 1e-6) if size >= 1 else lo
    return int(min(max(round(origin + offset), lo), max(lo, hi)))


class _ClickBudget:
    """One deadline shared by every step of a click attempt."""

    def __init__(self, seconds: float) -> None:
        self.deadline = asyncio.get_event_loop().time() + max(1.0, seconds)

    def remaining(self) -> float:
        return max(0.0, self.deadline - asyncio.get_event_loop().time())

    def ms(self, share_seconds: float) -> int:
        """Playwright timeout in ms: this step's share, capped by what is left."""
        return int(max(0.25, min(float(share_seconds), self.remaining())) * 1000)

    def check(self, target: str, cause: Exception | None) -> None:
        if self.remaining() > 0:
            return
        raise ClickBudgetExceeded(
            f"Click on {target} gave up after exhausting its time budget; "
            f"the element may be stale. Re-observe the page and try again. "
            f"Last error: {cause}"
        ) from cause

    async def run(self, awaitable, share: float, fallback):
        """Await within both this step's share and the overall budget.

        Runs as a task so a step we stopped waiting on still has its exception
        retrieved; otherwise every timed-out Playwright call leaves an
        "exception was never retrieved" warning in the app log.
        """
        left = self.remaining()
        if left <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            return fallback
        task = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=min(share, left))
        except Exception:
            task.cancel()
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
            return fallback


def _current_url(page: Any) -> str:
    """The page address Playwright tracks, including same-document routes."""
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


async def _target_detached(locator: Any, budget: "_ClickBudget") -> bool:
    """True only when the target is known to be gone; unknown is not gone."""
    try:
        count = locator.count()
    except (AttributeError, TypeError):
        return False
    if not inspect.isawaitable(count):
        return False
    result = await budget.run(count, 1.0, None)
    return isinstance(result, int) and not isinstance(result, bool) and result == 0


def _is_timeout_error(exc: BaseException) -> bool:
    """A dispatch that never came back, as opposed to one that was refused."""
    text = f"{type(exc).__name__}: {exc}"
    return "Timeout" in text and "exceeded" in text


_KEYBOARD_ACTIVATION = """el => {
  if (!el) return null;
  // A ref can name a decoration inside the control (an <svg> or <span
  // role=presentation> in a step button). The keyboard operates the control
  // that owns it, as Playwright's retarget() does for hit testing.
  const controls = 'button, a[href], summary, label, input, [role=button], [role=link], '
    + '[role=menuitem], [role=menuitemradio], [role=menuitemcheckbox], [role=option], '
    + '[role=tab], [role=checkbox], [role=radio], [role=switch], [tabindex]';
  const t = el.closest(controls) || el;
  if (t.disabled || t.getAttribute('aria-disabled') === 'true') return null;
  const tag = t.tagName.toLowerCase();
  const type = (t.getAttribute('type') || '').toLowerCase();
  const role = (t.getAttribute('role') || '').toLowerCase();
  // Space toggles a checkbox, radio or switch and checks a menu checkbox or
  // radio item; Enter activates a button, a link, a menu item, a tab or a
  // listbox option. Anything else has no documented key and is left alone.
  let key = '';
  if ((tag === 'input' && (type === 'checkbox' || type === 'radio'))
      || role === 'checkbox' || role === 'radio' || role === 'switch'
      || role === 'menuitemradio' || role === 'menuitemcheckbox') key = ' ';
  else if (tag === 'button' || (tag === 'a' && t.hasAttribute('href')) || tag === 'summary' || tag === 'label'
      || role === 'button' || role === 'link' || role === 'menuitem' || role === 'tab' || role === 'option'
      || (tag === 'input' && ['submit', 'button', 'reset'].includes(type))) key = 'Enter';
  else if (t.getAttribute('tabindex') !== null && t.getAttribute('tabindex') !== '-1' && role) key = 'Enter';
  if (!key) return null;
  // Native controls get a click from Enter/Space by the HTML spec. An ARIA
  // role only promises a key if the page wired one up.
  const native = tag === 'button' || (tag === 'a' && t.hasAttribute('href')) || tag === 'summary'
    || (tag === 'input' && ['checkbox', 'radio', 'submit', 'button', 'reset'].includes(type));
  if (t === el) return {key, token: '', native};
  const token = 'key-' + Math.random().toString(36).slice(2, 10);
  t.setAttribute('data-odysseus-key-target', token);
  return {key, token, native};
}"""


_EFFECT_SIGNATURE = """el => {
  const state = el ? [
    'present',
    el.checked === undefined ? '' : String(el.checked),
    el.getAttribute('aria-expanded') || '',
    el.getAttribute('aria-pressed') || '',
    el.getAttribute('aria-checked') || '',
    el.getAttribute('aria-selected') || '',
    (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
  ].join('~') : 'gone';
  return [location.href, document.title, state].join('|');
}"""


async def effect_signature(locator):
    """What this click is supposed to change: the address, and the target itself.

    Deliberately narrow. A whole-page fingerprint counts the clocks, adverts and
    lazy images a live page rewrites by itself, and reporting a stalled click as
    landed on that basis is worse than reporting it failed.
    """
    try:
        return str(await asyncio.wait_for(locator.evaluate(_EFFECT_SIGNATURE), timeout=2.0))
    except Exception:
        return None


_ACTIVATION_TARGET = """el => {
  const box = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  const shown = box.width > 0 && box.height > 0 && style.display !== 'none'
    && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
  // Parked off the page or shrunk to a 1px screen-reader box: operated
  // through its label like an invisible one (see hitTarget).
  const parked = box.right + scrollX <= 0 || box.bottom + scrollY <= 0
    || (box.width <= 1 && box.height <= 1);
  if (shown && !parked) return '';
  const tag = el.tagName.toLowerCase();
  if (tag !== 'input' && tag !== 'select' && tag !== 'textarea') return '';
  const labels = (el.labels && el.labels.length ? Array.from(el.labels) : [])
    .concat(el.closest('label') ? [el.closest('label')] : []);
  for (const label of labels) {
    const lb = label.getBoundingClientRect();
    const ls = getComputedStyle(label);
    if (lb.width > 0 && lb.height > 0 && ls.display !== 'none'
        && ls.visibility !== 'hidden' && Number(ls.opacity || 1) > 0) {
      const token = 'hit-' + Math.random().toString(36).slice(2, 10);
      label.setAttribute('data-odysseus-hit', token);
      return token;
    }
  }
  return '';
}"""


async def activation_locator(root, locator):
    """What a user would actually click to operate this control.

    A checkbox styled ``opacity: 0`` is still operated — through its label.
    Playwright refuses to click the invisible box, and a coordinate click lands
    on whatever is painted there, so aim at the label the page gave it. Returns
    ``(locator, "label")`` when redirected and ``(locator, "")`` otherwise.
    """
    try:
        token = await asyncio.wait_for(locator.evaluate(_ACTIVATION_TARGET), timeout=2.0)
    except Exception:
        return locator, ""
    if not token:
        return locator, ""
    return root.locator(f'[data-odysseus-hit="{token}"]').first, "label"


#: Browser contexts whose trial click has already been seen to time out.
#: The hit-target check hangs for the whole context in Camoufox's virtual
#: display, so after the first proof there is no reason to keep buying the
#: full wait — a run that clicks twenty things would pay it twenty times.
#: Weak keys: the note dies with the context.
_TRIAL_STALLED: "weakref.WeakKeyDictionary[Any, bool]" = weakref.WeakKeyDictionary()


def _trial_budget_ms(page: Any, base_ms: int) -> int:
    """How long to ask for actionability, given what this context has shown."""
    context = getattr(page, "context", None)
    if context is None:
        return base_ms
    try:
        if _TRIAL_STALLED.get(context):
            return max(200, int(base_ms * 0.2))
    except TypeError:
        return base_ms
    return base_ms


#: How long after a timed-out pointer dispatch its input can still land:
#: Camoufox's humanized movement takes up to 1.5 s, plus display latency.
_LATE_DELIVERY_SECONDS = 2.5


def _trial_stalled(page: Any) -> bool:
    """Whether this page's context has already shown a stalled trial."""
    context = getattr(page, "context", None)
    if context is None:
        return False
    try:
        return bool(_TRIAL_STALLED.get(context))
    except TypeError:
        return False


def _remember_trial_stall(page: Any) -> None:
    context = getattr(page, "context", None)
    if context is None:
        return
    try:
        _TRIAL_STALLED[context] = True
    except TypeError:
        pass


#: Seconds held back from the pointer rungs so the keyboard one always runs.
#: A live session spent its whole twenty second budget inside a stalled
#: coordinate click, four times, and never reached the keyboard — which had
#: worked on those same elements in an earlier run.
_KEYBOARD_RESERVE_SECONDS = 3.0


def _pointer_budget(budget: "_ClickBudget") -> float:
    """How long a pointer dispatch may hang before the keyboard gets its turn.

    The reserve is a share of what is left, capped: holding three seconds back
    out of a one second budget would spend the whole click deciding not to
    dispatch.
    """
    remaining = budget.remaining()
    reserve = min(_KEYBOARD_RESERVE_SECONDS, max(0.0, remaining * 0.3))
    return max(0.25, min(_POINTER_CAP_SECONDS, remaining - reserve))


_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _watch_state_changes(page: Any, sent: list[str]) -> Any:
    """Record same-origin state-changing requests the page sends from now on.

    The page sending a POST is the click's effect, visible before any DOM or
    address change. On Camoufox's virtual display a click that landed while
    Playwright reported a timeout left nothing else to see yet: the fixture
    form's submit waited on its own request before re-rendering, the witness
    probe ran out of its 0.3 s, and the coordinate rung submitted the
    application a second time (two identical POSTs, live, both arms).
    """
    origin = _origin(_current_url(page))

    def on_request(request: Any) -> None:
        try:
            if str(request.method or "").upper() in _READ_ONLY_METHODS:
                return
            if str(getattr(request, "resource_type", "") or "") not in {"fetch", "xhr", "document"}:
                return
            if origin and _origin(request.url) != origin:
                return  # third-party beacons and analytics are not the click's effect
            sent.append(str(request.method).upper())
        except Exception:
            pass

    try:
        page.on("request", on_request)
    except Exception:
        return None
    return on_request


async def click_with_fallbacks(
    page,
    *,
    ref: str,
    button: Literal["left", "right", "middle"] = "left",
    double_click: bool = False,
    label: str | None = None,
    workspace_id: str | None = None,
    tab_id: str | None = None,
    observation_id: str | None = None,
    allow_selector: bool = True,
) -> tuple[str, str]:
    """Click via ref with overlay dismiss and fallback chain. Returns (message, click_method)."""
    sent: list[str] = []
    listener = _watch_state_changes(page, sent)
    try:
        return await _click_with_fallbacks(
            page, ref=ref, button=button, double_click=double_click, label=label,
            workspace_id=workspace_id, tab_id=tab_id, observation_id=observation_id,
            allow_selector=allow_selector, state_changes=sent,
        )
    finally:
        if listener is not None:
            try:
                page.remove_listener("request", listener)
            except Exception:
                pass


async def _click_with_fallbacks(
    page,
    *,
    ref: str,
    button: Literal["left", "right", "middle"] = "left",
    double_click: bool = False,
    label: str | None = None,
    workspace_id: str | None = None,
    tab_id: str | None = None,
    observation_id: str | None = None,
    allow_selector: bool = True,
    state_changes: list[str] | None = None,
) -> tuple[str, str]:
    """The fallback chain behind click_with_fallbacks.

    Bounded by one shared deadline, for the same reason `_SnapshotBudget` exists:
    the steps below are additive, and `bounding_box`/`evaluate` inherit
    Playwright's 30s default when given no timeout. A ref that no longer resolves
    measured 70s end to end — and this runs under the per-owner action lock, so
    that is 70s during which every browser tool in every one of this owner's
    chats blocks. Better to give up early and let the model re-observe.
    """
    from ascended_browser.runtime.settings import get_setting

    budget = _ClickBudget(float(get_setting("browser_click_budget_seconds", 20) or 20))
    target = label or ref
    # `root` is the page for an ordinary element and the owning iframe for one
    # inside an embedded widget; `page.locator` never descends into a frame.
    root, selector = await resolve_live_target(
        page, ref, workspace_id=workspace_id, tab_id=tab_id,
        observation_id=observation_id, allow_selector=allow_selector,
    )
    if not await asyncio.wait_for(frame_is_rendered(root), timeout=3.0):
        raise ValueError("Target iframe is hidden; observe the page again before acting")
    locator = root.locator(selector).first
    return await _activate(
        page, root, locator, target=target, budget=budget, button=button,
        double_click=double_click, workspace_id=workspace_id, tab_id=tab_id,
        state_changes=state_changes,
    )


async def activate_locator(
    page: Any,
    root: Any,
    locator: Any,
    *,
    target: str,
    budget_seconds: float | None = None,
    workspace_id: str | None = None,
    tab_id: str | None = None,
) -> tuple[str, str]:
    """Activate one resolved control through the same ladder a click uses.

    For callers that already hold a locator (checkables, listbox options):
    a bare ``locator.click`` there skipped every rung that makes clicks land
    on Camoufox -- the owned-point mouse click, the keyboard, the background
    tab rule -- and was the first failure in five of eleven escalation chains
    to low-level tools in the 2026-10-04 stress sessions.
    """
    from ascended_browser.runtime.settings import get_setting

    sent: list[str] = []
    listener = _watch_state_changes(page, sent)
    seconds = budget_seconds or float(get_setting("browser_click_budget_seconds", 20) or 20)
    try:
        return await _activate(
            page, root, locator, target=target, budget=_ClickBudget(seconds),
            workspace_id=workspace_id, tab_id=tab_id, state_changes=sent,
        )
    finally:
        if listener is not None:
            try:
                page.remove_listener("request", listener)
            except Exception:
                pass


#: Longest a single pointer dispatch may hang before the keyboard gets its turn.
#: Juggler queues mouse input behind its global tab-activation chain; with several
#: tabs and the live view contending, a dispatch waited 14-17 s and the keyboard
#: rung landed at 18 s (2026-10-04, Workday). A healthy dispatch, humanized
#: cursor movement included, finishes in about 1-2 s.
_POINTER_CAP_SECONDS = 4.0

#: Scroll alignments tried before a covered target is reported: a sticky header
#: or footer covers a target that one of the other alignments clears.
_SCROLL_ALIGNMENTS = ("center", "end", "start")

#: Remember every scroll position between the target and the document, so a
#: failed search for an unobstructed alignment leaves the page as it was.
_SAVE_SCROLL_SCRIPT = """el => {
  // Positions travel back to the caller rather than living in a page global:
  // successive evaluate calls are not guaranteed to share one JS world.
  const token = 'scroll-' + Math.random().toString(36).slice(2, 10);
  const saved = {token, x: scrollX, y: scrollY, nodes: []};
  let index = 0;
  for (let n = el.parentElement; n; n = n.parentElement) {
    if (n === document.scrollingElement) continue;
    if (n.scrollHeight > n.clientHeight || n.scrollWidth > n.clientWidth) {
      const mark = token + '-' + (index++);
      n.setAttribute('data-odysseus-scroll', mark);
      saved.nodes.push([mark, n.scrollLeft, n.scrollTop]);
    }
  }
  return saved;
}"""
_RESTORE_SCROLL_SCRIPT = """(el, saved) => {
  // 'instant': a page with scroll-behavior: smooth would otherwise still be
  // animating when the cover is inspected.
  for (const [mark, left, top] of saved.nodes || []) {
    const node = document.querySelector('[data-odysseus-scroll="' + mark + '"]');
    if (node) { node.scrollTo({left, top, behavior: 'instant'}); node.removeAttribute('data-odysseus-scroll'); }
  }
  window.scrollTo({left: saved.x, top: saved.y, behavior: 'instant'});
  return true;
}"""

#: The target lives inside an open popup (listbox, menu, an aria-expanded
#: owner's controlled region). Escape would close the very thing being clicked.
_IN_OPEN_POPUP_SCRIPT = """el => {
  if (!el) return false;
  if (el.closest('[role=listbox],[role=menu],[role=menubar],[role=tree],[role=grid],[role=combobox]')) return true;
  for (const owner of document.querySelectorAll('[aria-expanded="true"][aria-controls]')) {
    for (const id of (owner.getAttribute('aria-controls') || '').split(/\\s+/)) {
      const region = id && document.getElementById(id);
      if (region && region.contains(el)) return true;
    }
  }
  return false;
}"""


async def _keyboard_activate(
    page, root, locator, budget, *, minimum: float = 0.5, native_only: bool = False,
) -> str:
    """Focus the control that owns ``locator`` and press its documented key.

    Returns the key pressed, or "" when the target has no documented key or the
    budget is too small. Key events do not go through Juggler's tab activation,
    so this never brings a background tab to the front.
    """
    activation = await budget.run(locator.evaluate(_KEYBOARD_ACTIVATION), 2, None)
    if not isinstance(activation, dict) or not activation.get("key") or budget.remaining() <= minimum:
        return ""
    if native_only and not activation.get("native"):
        # A role-only widget may have no key handler; a key press there could
        # "succeed" without doing anything. The pointer ladder decides it.
        if activation.get("token"):
            try:
                await budget.run(root.evaluate(
                    "t => document.querySelectorAll('[data-odysseus-key-target=\"' + t + '\"]')"
                    ".forEach(n => n.removeAttribute('data-odysseus-key-target'))", activation["token"],
                ), 1, None)
            except TypeError:
                pass
        return ""
    key = str(activation["key"])
    token = str(activation.get("token") or "")
    focus_target = root.locator(f'[data-odysseus-key-target="{token}"]').first if token else locator
    try:
        # Playwright's own timeout, not only wait_for's: a cancelled focus
        # left a 30 s Playwright future running whose failure nobody read.
        seconds = max(0.25, min(3.0, budget.remaining()))
        await asyncio.wait_for(focus_target.focus(timeout=int(seconds * 1000)), timeout=seconds + 0.5)
        # A key goes wherever focus is. An inert or focus-trapped target leaves
        # focus elsewhere (a modal's default button), and Enter there would be
        # a different action than the one asked for.
        focused = await budget.run(focus_target.evaluate(
            "el => { let a = el.ownerDocument.activeElement;"
            " while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;"
            " return a === el || (!!a && el.contains(a)); }"
        ), 1, False)
        if not focused:
            return ""
        await asyncio.wait_for(page.keyboard.press(key), timeout=min(3.0, budget.remaining()))
    except Exception:
        return ""
    finally:
        if token:
            try:
                await budget.run(root.evaluate(
                    "t => document.querySelectorAll('[data-odysseus-key-target=\"' + t + '\"]')"
                    ".forEach(n => n.removeAttribute('data-odysseus-key-target'))", token,
                ), 1, None)
            except TypeError:
                pass
    return "Space" if key == " " else key


async def _activate(
    page,
    root,
    locator,
    *,
    target: str,
    budget: "_ClickBudget",
    button: Literal["left", "right", "middle"] = "left",
    double_click: bool = False,
    workspace_id: str | None = None,
    tab_id: str | None = None,
    state_changes: list[str] | None = None,
) -> tuple[str, str]:
    """The fallback ladder for one resolved target. See ``click_with_fallbacks``."""
    from ascended_browser.runtime.settings import get_setting
    from ascended_browser._app.browser_agent_cursor import caption as _cursor_caption
    from ascended_browser._app.browser_presented_tab import presented_state

    click_caption = _cursor_caption(
        "Double-clicking" if double_click else
        "Right-clicking" if button == "right" else "Clicking",
        target,
    )

    async def agent_cursor(kind, *, x=None, y=None):
        """Spectator cursor events with this call's exact ownership scope."""
        await _agent_cursor_event(
            page, kind, x=x, y=y, workspace_id=workspace_id, tab_id=tab_id,
            detail=click_caption,
        )
    first_exc: Exception | None = None
    # The rungs that ran, in order, so a final failure names what was tried
    # instead of quoting the first preflight's timeout.
    tried: list[str] = []
    # A control styled invisible is operated through its label; aim there.
    locator, aimed_at = await activation_locator(root, locator)
    # What the page and the target were before anything was sent. Every rung
    # below checks this first, so a click that stalled after landing is never
    # sent a second time.
    before_signature = await effect_signature(locator)
    # The address alone, read from Playwright's own navigation tracking. The
    # signature above needs the target to still exist, and a row that
    # navigated its SPA away is gone: every later probe of it then waited out
    # its whole share, and a click that had landed spent 18 s of its budget
    # proving nothing before reporting failure.
    before_url = _current_url(page)
    # A click can reach the page while Playwright reports an actionability
    # timeout. An unchanged button label then gives effect_signature nothing
    # to compare, and a coordinate fallback sends a second real click. Keep a
    # short-lived trusted DOM-event witness in the owning frame so a completed
    # first dispatch is never retried merely because the button looks the same.
    witness_token = secrets.token_hex(8)
    try:
        witness_probe = locator.evaluate("""(el, token) => {
      if (!el || !el.isConnected) return false;
      const witnesses = window.__odysseusClickWitnesses ||= new Map();
      if (witnesses.size >= 32) witnesses.delete(witnesses.keys().next().value);
      const witness = { click: false };
      witnesses.set(token, witness);
      const click = event => {
        if (event.isTrusted) { witness.click = true; el.removeEventListener('click', click, true); }
      };
      el.addEventListener('click', click, true);
      setTimeout(() => {
        witnesses.delete(token);
        el.removeEventListener('click', click, true);
      }, 30000);
      return true;
    }""", witness_token)
    except TypeError:
        witness_installed = False  # a legacy locator adapter without evaluate arguments
    else:
        witness_installed = bool(await budget.run(witness_probe, 0.3, False))

    async def witnessed_click() -> bool:
        if not witness_installed:
            return False
        try:
            probe = root.evaluate(
                "token => !!window.__odysseusClickWitnesses?.get(token)?.click",
                witness_token,
            )
        except TypeError:
            return False
        return bool(await budget.run(probe, 0.3, False))

    async def already_landed() -> bool:
        if state_changes:
            return True  # the page already sent the click's request
        if before_url and _current_url(page) not in ("", before_url):
            return True
        if await witnessed_click():
            return True
        if before_signature is None:
            return False
        after = await effect_signature(locator)
        # An unreadable target says nothing either way: a framework can replace
        # the node it re-renders, and that is not evidence of the click.
        return after is not None and after != before_signature

    inside_dialog = bool(await budget.run(locator.evaluate(_IN_DIALOG_SCRIPT), 2, False))
    # No speculative overlay sweep here. It ran before every click outside a
    # dialog: up to five seconds of class-name guessing, close-button clicks
    # and two Escape presses that could close the very listbox or submenu the
    # model was about to click into. The hit test below decides whether
    # anything covers the target, and only then is anything dismissed.

    # Where the pointer will act, read once before anything is sent. On
    # Camoufox the trial rung below usually delivers the click itself, and a
    # box read after that finds a link that already navigated away, so the
    # watcher saw no pointer for most clicks. Off-screen targets are not
    # aimed at here: the dispatch scrolls them, and a clamped edge point would
    # show the pointer somewhere the click is not.
    aim = await _agent_cursor_aim(page, locator, budget)
    if aim:
        await agent_cursor("pointer_move", x=aim[0], y=aim[1])

    if double_click:
        center = aim or await _agent_cursor_center(locator, budget)
        if center and not aim:
            await agent_cursor("pointer_move", x=center[0], y=center[1])
        await locator.dblclick(button=button, timeout=budget.ms(10))
        if center:
            await agent_cursor("pointer_down", x=center[0], y=center[1])
            await agent_cursor("pointer_up", x=center[0], y=center[1])
        return f"Double-clicked on {target}", "ref"

    opens_new_tab = bool(await budget.run(
        locator.evaluate("el => el && el.target === '_blank'"), 3, False,
    ))
    if opens_new_tab and budget.remaining() > 0:
        center = aim or await _agent_cursor_center(locator, budget)
        try:
            async with page.context.expect_page(timeout=budget.ms(5)):
                if center and not aim:
                    await agent_cursor("pointer_move", x=center[0], y=center[1])
                await locator.click(button=button, timeout=budget.ms(10))
            if center:
                await agent_cursor("pointer_down", x=center[0], y=center[1])
                await agent_cursor("pointer_up", x=center[0], y=center[1])
            return f"Clicked on {target} (opened new tab)", "ref"
        except Exception:
            pass

    # A page the live view is not showing. Every pointer dispatch -- and even
    # Playwright's actionability trial -- goes through Juggler's global
    # activation chain and raises the page's window (measured 2026-10-04: a
    # trial click switched the screen, a key press did not). With several tabs
    # acting at once that chain queued mouse input for 14-17 s, and Camoufox's
    # humanized pointer adds 1-2 s per dispatch. A native control is operated
    # from the keyboard instead, exactly as a keyboard user would; anything
    # else takes the pointer ladder below.
    background = presented_state(page) == "background" and button == "left"
    if background:
        # The hit test is a plain evaluate: it neither selects the tab nor
        # sends input. A target something else covers is not operated from the
        # keyboard behind its cover; it takes the ordinary path below.
        early_hit = await budget.run(locator.evaluate(_HIT_TEST_SCRIPT), 2, None) or {}
        if early_hit.get("owns") or early_hit.get("shield") or early_hit.get("owner") == _NOTHING_AT_POINT:
            key = await _keyboard_activate(page, root, locator, budget, native_only=True)
            if key:
                tried.append("keyboard (background tab)")
                await _agent_cursor_click_ledge(agent_cursor, locator, budget, aim)
                return f"Clicked on {target} via keyboard ({key}); its window stayed in the background", "keyboard"

    # Trial first: Playwright runs every actionability check and sends no
    # input. A proxy input parked outside the viewport (Workday's date
    # segments) or a covered button otherwise costs the full click timeout and
    # comes back as an *uncertain* effect, though nothing was ever sent.
    # The trial asks Playwright whether the element is visible, enabled, stable
    # and hit-testable, and sends nothing. Five seconds of that was the single
    # largest cost of a click that could not land: measured, the same trial
    # passes in 0.3s when it can pass at all, and where it cannot (Camoufox's
    # virtual display) waiting longer changes nothing while the coordinate rung
    # below lands in 0.6s. Anything the shorter window rejects still goes
    # through the hit test before a pointer is moved, so nothing is clicked
    # blind. A background tab skips it: its stability wait needs animation
    # frames the tab is not painting, and the trial alone selects the tab.
    trial_ms = _trial_budget_ms(
        page, max(200, int(get_setting("browser_click_trial_ms", 1200) or 1200)),
    )
    # A context whose trial already stalled once is not asked again. On
    # Camoufox a stalled trial can still deliver its click well after it
    # timed out (humanized movement takes 1-2 s), and the owned-point mouse
    # rung then sent a second one: one Submit became two submissions in a
    # 2026-10-04 live trial. The hit test below proves the point is owned
    # without sending anything, so the pointer click is the only dispatch.
    skip_preflight = _trial_stalled(page)
    dispatch_started: float | None = None
    if not background and not skip_preflight:
        tried.append("actionability trial")
        dispatch_started = asyncio.get_event_loop().time()
        try:
            await locator.click(
                button=button, timeout=min(budget.ms(10), trial_ms), trial=True, no_wait_after=True,
            )
        except TypeError:
            pass  # an adapter without trial clicks; the real click decides
        except Exception as exc:
            if is_click_intercept_error(exc):
                first_exc = exc
            else:
                from ascended_browser._app.browser_execution import dispatch_withdrawn

                reason = _actionability_blocker(exc)
                if reason and "outside the visible page" in reason:
                    # A screen-reader link parked off the page (lichess's "Enable
                    # blind mode", a skip link) takes no pointer, but it is how
                    # keyboard users reach that control: focus it and press its
                    # key, as they would. Only links, buttons and checkables have
                    # a documented key; anything else still fails below.
                    key = await _keyboard_activate(page, root, locator, budget, minimum=0.0)
                    if key:
                        return f"Clicked on {target} via keyboard ({key}); it sits off the page", "keyboard"
                if reason:
                    dispatch_withdrawn(reason)
                    unreachable = ClickTargetUnreachable(
                        f"{target} cannot be clicked: {reason}. Nothing was clicked. "
                        f"{await _unreachable_next_step(locator, reason)}"
                    )
                    await _attach_inspection(unreachable, locator, budget, workspace_id=workspace_id, tab_id=tab_id)
                    raise unreachable from exc
                if _is_timeout_error(exc):
                    # This context cannot answer the question quickly; stop paying
                    # the full wait for it on every later click.
                    _remember_trial_stall(page)
                if "locator resolved to" in str(exc) and "Timeout" in str(exc):
                    # Found, but never reported visible, enabled and stable within
                    # the trial (Ashby, in the live app: every click waited 5 s + 10 s
                    # and failed). Nothing was sent, so click it where it is, after
                    # the hit test proves it owns that point — browser-use clicks
                    # this way always, without a stability wait.
                    first_exc = exc
                # Anything else is the preflight's own problem: the real click
                # decides, exactly as before the preflight existed.
        if first_exc is None:
            tried.append("direct click")
            center = aim or await _agent_cursor_center(locator, budget)
            if center and not aim:
                # The glide happens during the real click's actionability checks;
                # the press animation lands once the dispatch is accepted.
                await agent_cursor("pointer_move", x=center[0], y=center[1])
            try:
                dispatch_started = asyncio.get_event_loop().time()
                await locator.click(button=button, timeout=budget.ms(10), no_wait_after=True)
                if center:
                    await agent_cursor("pointer_down", x=center[0], y=center[1])
                    await agent_cursor("pointer_up", x=center[0], y=center[1])
                return f"Clicked on {target}", "ref"
            except Exception as exc:
                first_exc = exc
                # An interception is worth a fallback chain, and so is a timeout
                # that changed nothing. Camoufox's virtual display hangs
                # Playwright's hit-target check: the click never lands (measured —
                # `force` and a coordinate click both work immediately afterwards),
                # and two live sessions spent whole 20s budgets there. A timeout
                # that *did* land is caught by the signature check, so nothing is
                # dispatched twice. Anything else (a ref that does not resolve, a
                # detached node) fails the same way every time.
                if not is_click_intercept_error(exc) and not _is_timeout_error(exc):
                    raise
                if await already_landed():
                    await _agent_cursor_click_ledge(agent_cursor, locator, budget, aim)
                    return f"Clicked on {target}", "ref"

        budget.check(target, first_exc)

        # A rung that timed out may still have delivered its input (Camoufox's
        # trial click, measured above). Ask before any fallback: the ones below
        # each spend seconds on a target that a landed click may have removed.
        if first_exc is not None and _is_timeout_error(first_exc):
            if await already_landed():
                await _agent_cursor_click_ledge(agent_cursor, locator, budget, aim)
                return f"Clicked on {target}", "ref"
            if await _target_detached(locator, budget):
                raise RuntimeError(
                    f"Click on {target} is uncertain: the target left the page after "
                    "input may have been sent, and the address did not change. Observe "
                    "the tab to see what happened before clicking again."
                ) from first_exc

    # Hit-test first: it may scroll an off-screen target into view, it says
    # whether anything actually covers the target, and the box used for the
    # click must be read after that.
    hit = await budget.run(locator.evaluate(_HIT_TEST_SCRIPT), 2, None) or {}
    if hit and hit.get("edge") and not (hit.get("owns") or hit.get("shield")):
        # Something else owns the point. A sticky header or footer is cleared
        # by another scroll alignment; try those before touching the page. If
        # none clears it, put every scroll position back: the cover reported
        # (and inspected) must be the one that blocked the target, not
        # whatever the last alignment happened to slide under.
        first_hit = hit
        saved = await budget.run(locator.evaluate(_SAVE_SCROLL_SCRIPT), 1, None)
        for alignment in _SCROLL_ALIGNMENTS:
            await budget.run(locator.evaluate(
                "(el, block) => el.scrollIntoView({block, inline: 'nearest', behavior: 'instant'})", alignment,
            ), 1, None)
            hit = await budget.run(locator.evaluate(_HIT_TEST_SCRIPT), 2, None) or hit
            if hit.get("owns") or hit.get("shield"):
                break
        else:
            if isinstance(saved, dict):
                await budget.run(locator.evaluate(_RESTORE_SCROLL_SCRIPT, saved), 1, None)
            hit = first_hit
    if (
        not inside_dialog
        and not (hit.get("owns") or hit.get("shield"))
        and hit.get("owner") != _NOTHING_AT_POINT
    ):
        # Only now, with a named blocker, is anything dismissed -- and never
        # with Escape while the target sits in an open listbox or menu.
        in_popup = bool(await budget.run(locator.evaluate(_IN_OPEN_POPUP_SCRIPT), 1, False))
        await budget.run(dismiss_blocking_overlays(page, allow_escape=not in_popup), 5, None)
        hit = await budget.run(locator.evaluate(_HIT_TEST_SCRIPT), 2, None) or hit
    bbox = await budget.run(locator.bounding_box(timeout=budget.ms(3)), 3, None)
    pointer_timed_out = False
    if bbox and (hit.get("owns") or hit.get("shield")):
        # The trial rung is actionability-only in contract, but on Camoufox's
        # virtual display its hit-target check can stall past its deadline
        # with the input already committed (measured: two identical trusted
        # click pairs ~10ms apart, one per rung). Every rung re-checks the
        # effect signature before dispatching, so a click whose effect is
        # already on the page is never sent a second time. The committed
        # input can still be in flight when the deadline fires (measured
        # ~90ms of display latency after the timeout), so after a timeout
        # the check takes one bounded second look before deciding nothing
        # landed.
        if first_exc is not None and _is_timeout_error(first_exc):
            # Watch for the timed-out dispatch's late delivery for the whole
            # window it can arrive in, not a fixed 0.36 s: humanized pointer
            # input lands up to ~2 s after it was sent.
            window_end = (dispatch_started or 0.0) + _LATE_DELIVERY_SECONDS
            while True:
                if await already_landed():
                    break
                if asyncio.get_event_loop().time() >= window_end or budget.remaining() < 1.0:
                    break
                await asyncio.sleep(0.15)
        if await already_landed():
            await _agent_cursor_click_ledge(agent_cursor, locator, budget, aim)
            return f"Clicked on {target}", "ref"
        tried.append("mouse at an owned point")
        dispatch_started = asyncio.get_event_loop().time()
        try:
            # Playwright mouse operations accept no timeout. Bound the dispatch
            # by the shared click deadline so a stalled pointer input cannot
            # hold the per-owner action lock (and the live view) indefinitely.
            if hit.get("owns") and isinstance(hit.get("dx"), (int, float)):
                # The point the hit test proved the target owns, as whole
                # pixels inside the box: Firefox rounds fractional coordinates
                # and a small toggle's edge point can round off it.
                x = _inside(bbox["x"], bbox["width"], hit["dx"])
                y = _inside(bbox["y"], bbox["height"], hit["dy"])
                click = page.mouse.dblclick if double_click else page.mouse.click
                await agent_cursor("pointer_move", x=x, y=y)
                await agent_cursor("pointer_down", x=x, y=y)
                await asyncio.wait_for(click(x, y, button=button), timeout=_pointer_budget(budget))
                await agent_cursor("pointer_up", x=x, y=y)
                return f"Clicked on {target} via mouse at a point it owns", "mouse"
            await asyncio.wait_for(
                _mouse_click_bbox(
                    page, bbox, button=button, double_click=double_click,
                    workspace_id=workspace_id, tab_id=tab_id, detail=click_caption,
                ),
                timeout=_pointer_budget(budget),
            )
            return f"Clicked on {target} via mouse at center of bounding box", "mouse"
        except asyncio.TimeoutError:
            pointer_timed_out = True
        except Exception:
            pass
    if pointer_timed_out:
        # The input may still be queued behind Juggler's activation chain and
        # land late. Look for its effect for the whole late-delivery window
        # before the keyboard sends a second activation.
        window_end = (dispatch_started or 0.0) + _LATE_DELIVERY_SECONDS
        while not await already_landed():
            if asyncio.get_event_loop().time() >= window_end or budget.remaining() < 1.0:
                break
            await asyncio.sleep(0.15)
    if await already_landed():
        await _agent_cursor_click_ledge(agent_cursor, locator, budget, aim)
        return f"Clicked on {target}", "ref"

    if hit and not (hit.get("owns") or hit.get("shield")) and hit.get("owner") != _NOTHING_AT_POINT:
        from ascended_browser._app.browser_execution import dispatch_withdrawn

        owner = str(hit.get("owner") or "another element")
        dispatch_withdrawn(f"target point owned by {owner}")
        covered = RuntimeError(
            f"Click failed on {target}: {owner} covers it, so nothing was clicked. "
            "Close or dismiss what covers it, then click again."
        )
        await _attach_inspection(covered, locator, budget, workspace_id=workspace_id, tab_id=tab_id)
        raise covered from first_exc

    # Keyboard activation, last before giving up. A button, link or checkbox
    # reached by the keyboard is operated the way a keyboard user operates it,
    # and it is what worked when pointer input would not land at all. It runs
    # only after the page has been shown not to have changed, and never on a
    # target something else covers (above). "Nothing at that point" (a
    # zero-size or clipped box) is not a cover: it comes here too.
    if "keyboard (background tab)" not in tried:
        tried.append("keyboard")
        key = await _keyboard_activate(page, root, locator, budget)
        if key:
            await _agent_cursor_click_ledge(agent_cursor, locator, budget, aim)
            return f"Clicked on {target} via keyboard ({key})", "keyboard"

    budget.check(target, first_exc)

    href = str(await budget.run(
        locator.evaluate(
            "el => (el && el.href) ? el.href : (el && el.closest('a') ? el.closest('a').href : '')"
        ), 5, "",
    ) or "").strip()
    if href and href.startswith(("http://", "https://")):
        if opens_new_tab:
            try:
                async with page.context.expect_page(timeout=budget.ms(5)):
                    await page.goto(href, wait_until="domcontentloaded", timeout=budget.ms(30))
                return f"Navigated to {href} (link href fallback, new tab)", "navigate"
            except Exception:
                pass
        await page.goto(href, wait_until="domcontentloaded", timeout=budget.ms(30))
        return f"Navigated to {href} (link href fallback)", "navigate"

    # Name what was tried and why each failed, not just the first preflight's
    # timeout ("Timeout 240ms" told the model nothing about the real cause).
    detail = []
    if first_exc is not None:
        detail.append(f"first attempt: {str(first_exc).splitlines()[0][:160]}")
    if pointer_timed_out:
        detail.append("the pointer did not dispatch in time")
    if not bbox:
        detail.append("no box could be read for the target")
    if hit.get("owner") == _NOTHING_AT_POINT:
        detail.append("nothing is painted at the target's position")
    detail.append("it has no documented keyboard activation")
    raise RuntimeError(
        f"Click failed on {target}. Tried: {', '.join(tried) or 'nothing'}; "
        f"{'; '.join(detail)}. Observe the page again, or use the control's own "
        "verb (select, check, fill) if it is a form control."
    ) from first_exc


class _SnapshotBudget:
    """One deadline shared by every probe in a snapshot.

    Per-probe timeouts are additive: title 10 + a11y 20 + elements 45 + overlays
    15 + tabs 10 + viewport 10 allowed a single observe to run for nearly two
    minutes, which is long enough for the chat stream to tear the turn down and
    orphan the tool. A snapshot is a best-effort read, so it returns whatever it
    has when the budget is gone rather than staying complete at any cost.
    """

    def __init__(self, seconds: float) -> None:
        self.deadline = asyncio.get_event_loop().time() + max(1.0, seconds)

    def remaining(self) -> float:
        return max(0.0, self.deadline - asyncio.get_event_loop().time())

    async def run(self, awaitable, share: float, fallback):
        """Await within both this probe's share and the overall budget."""
        left = self.remaining()
        if left <= 0:
            awaitable.close() if hasattr(awaitable, "close") else None
            return fallback
        try:
            return await asyncio.wait_for(awaitable, timeout=min(share, left))
        except Exception:
            return fallback


async def _agent_cursor_event(page, kind, *, x=None, y=None, workspace_id=None, tab_id=None, detail=""):
    """Publish one agent pointer event for live-view spectators.

    Best-effort and bounded; publication can never fail, slow, or alter a
    tool action (see src/browser_agent_cursor.py). Without coordinates the
    event is simply not drawn — invented positions would mislead the watcher.
    """
    from ascended_browser._app import browser_agent_cursor

    await browser_agent_cursor.emit(
        page, kind, x=x, y=y, workspace_id=workspace_id, tab_id=tab_id, detail=detail,
    )


async def _agent_cursor_center(locator, budget, share: float = 5) -> tuple[float, float] | None:
    """Where the pointer would be acting: the target box's center, bounded."""
    from ascended_browser._app import browser_agent_cursor

    try:
        box = await budget.run(locator.bounding_box(timeout=budget.ms(share)), share, None)
    except (AttributeError, TypeError):
        return None
    return browser_agent_cursor.box_center(box)


async def _agent_cursor_aim(page, locator, budget) -> tuple[float, float] | None:
    """The target's center before dispatch, if it is inside the viewport."""
    from ascended_browser._app import browser_agent_cursor

    center = await _agent_cursor_center(locator, budget, share=0.3)
    if center is None:
        return None
    width, height = await browser_agent_cursor._page_size(page)
    if width > 0 and height > 0 and not (0 <= center[0] <= width and 0 <= center[1] <= height):
        return None
    return center


async def _agent_cursor_click_ledge(agent_cursor, locator, budget, aim=None) -> None:
    """Show the one observed click when dispatch succeeded but its reply timed out.

    Purely cosmetic, and the click has landed: a link that navigated has taken
    its target with it, and waiting five seconds for that box to reappear made
    every first click on Camoufox (whose trial rung delivers the input) cost
    eight seconds instead of three.
    """
    # The aim read before dispatch is where the click went; the target may be
    # gone now (a link that navigated), so prefer it over a fresh read.
    center = aim or await _agent_cursor_center(locator, budget, share=0.3)
    if center is None:
        return
    if not aim:
        await agent_cursor("pointer_move", x=center[0], y=center[1])
    await agent_cursor("pointer_down", x=center[0], y=center[1])
    await agent_cursor("pointer_up", x=center[0], y=center[1])


async def _bounded(awaitable, seconds: float, fallback):
    """Bound a single page probe. Prefer _SnapshotBudget when several run."""
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except Exception:
        return fallback


async def build_enhanced_snapshot(
    page,
    *,
    a11y_snapshot: str = "",
    register_refs: bool = True,
    workspace_id: str = _LEGACY_WORKSPACE,
    tab_id: str = _LEGACY_TAB,
    budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Build enhanced page snapshot with a11y tree, elements, overlays, and viewport.

    Bounded by one shared deadline: every caller, legacy MCP included, gets a
    snapshot back within budget_seconds even if the page has stopped answering.
    """
    workspace_id, tab_id = _resolved_scope(workspace_id, tab_id)
    if budget_seconds is None:
        from ascended_browser.runtime.settings import get_setting

        budget_seconds = float(get_setting("browser_snapshot_budget_seconds", 20) or 20)
    budget = _SnapshotBudget(budget_seconds)
    url = getattr(page, "url", "") or ""
    try:
        title = await budget.run(page.title(), 5, "")
    except Exception:
        title = ""
    if not a11y_snapshot:
        try:
            tree = await budget.run(page.accessibility.snapshot(), 8, None)
            if tree:
                from camoufox_mcp.operations import format_snapshot_node
                a11y_snapshot = format_snapshot_node(tree)
            else:
                a11y_snapshot = "No accessibility tree available for this page"
        except Exception:
            try:
                a11y_snapshot = await budget.run(
                    page.locator("body").inner_text(timeout=5000), 5, "")
            except Exception:
                a11y_snapshot = ""

    coverage: dict[str, Any] = {"elements_complete": False}
    diagnostics: list[dict[str, Any]] = []
    elements = await budget.run(
        collect_interactive_elements(
            page,
            register_refs=register_refs,
            workspace_id=workspace_id,
            tab_id=tab_id,
            frame_timeout_seconds=min(5.0, max(1.0, budget.remaining() / 2)),
            coverage=coverage, diagnostics=diagnostics,
        ),
        max(1.0, budget.remaining()), [],
    )
    overlays = await budget.run(detect_blocking_overlays(page), 5, {})
    tabs = await budget.run(collect_open_tabs(page), 3, [])
    viewport = await budget.run(viewport_metadata(page), 3, {})

    return {
        "url": url,
        "title": title,
        "accessibility_snapshot": a11y_snapshot,
        "elements": elements,
        "blocking_overlays": overlays.get("blocking_overlays") or [],
        "dismiss_candidates": overlays.get("dismiss_candidates") or [],
        "overlay_count": int(overlays.get("overlay_count") or 0),
        "open_tabs": tabs,
        "viewport": viewport,
        "budget_exhausted": budget.remaining() <= 0,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        **coverage,
        "collection_diagnostics": diagnostics,
    }


def format_enhanced_snapshot_text(data: dict[str, Any]) -> str:
    """Format enhanced snapshot as human/agent-readable text."""
    lines = [
        f"url: {data.get('url', '')}",
        f"title: {data.get('title', '')}",
        f"viewport: {data.get('viewport', {}).get('width', 0)}x{data.get('viewport', {}).get('height', 0)}",
        f"coordinate_space: {data.get('viewport', {}).get('coordinate_space', 'viewport_css_pixels')}",
        f"overlay_count: {data.get('overlay_count', 0)}",
    ]
    if data.get("blocking_overlays"):
        lines.append("blocking_overlays: " + json.dumps(data["blocking_overlays"], ensure_ascii=False)[:2000])
    if data.get("open_tabs"):
        lines.append("open_tabs: " + json.dumps(data["open_tabs"], ensure_ascii=False)[:1000])
    lines.append("")
    lines.append("## Interactive elements (use ref or center with browser_click_at)")
    for el in data.get("elements") or []:
        if not isinstance(el, dict):
            continue
        center = el.get("center") or {}
        lines.append(
            f"- ref={el.get('ref', '')} css_selector={el.get('css_selector', el.get('selector', ''))} "
            f"center=({center.get('x', '')},{center.get('y', '')}) text={el.get('text', '')!r} "
            f"href={el.get('href', '')}"
        )
    lines.append("")
    lines.append("## Accessibility snapshot")
    lines.append(str(data.get("accessibility_snapshot") or ""))
    return "\n".join(lines)


async def render_marked_screenshot(
    page,
    *,
    max_marks: int = 30,
    image_type: str = "jpeg",
    quality: int = 82,
) -> tuple[bytes, str, list[dict[str, Any]]]:
    """Capture screenshot with numbered Set-of-Mark overlays. Returns (image_bytes, mime, marks)."""
    from PIL import Image, ImageDraw, ImageFont

    elements = await collect_interactive_elements(page, max_elements=max_marks)
    marks: list[dict[str, Any]] = []
    for index, el in enumerate(elements[:max_marks], start=1):
        if not isinstance(el, dict):
            continue
        center = el.get("center") or {}
        bbox = el.get("bbox") or {}
        marks.append({
            "index": index,
            "ref": el.get("ref", ""),
            "selector": el.get("selector", ""),
            "text": el.get("text", ""),
            "center": center,
            "bbox": bbox,
        })

    image_type = (image_type or "jpeg").strip().lower()
    if image_type not in {"jpeg", "png"}:
        image_type = "jpeg"
    shot_kwargs: dict[str, Any] = {"type": image_type, "full_page": False, "timeout": 15000}
    if image_type == "jpeg":
        shot_kwargs["quality"] = max(35, min(int(quality or 82), 95))
    raw = await page.screenshot(**shot_kwargs)

    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    for mark in marks:
        bbox = mark.get("bbox") or {}
        x = int(bbox.get("x") or 0)
        y = int(bbox.get("y") or 0)
        w = int(bbox.get("width") or 0)
        h = int(bbox.get("height") or 0)
        if w < 2 or h < 2:
            continue
        idx = int(mark["index"])
        draw.rectangle([x, y, x + w, y + h], outline=(255, 64, 64, 255), width=2)
        label = str(idx)
        tx, ty = x + 2, max(0, y - 12)
        if font:
            draw.text((tx, ty), label, fill=(255, 255, 0, 255), font=font)
        else:
            draw.text((tx, ty), label, fill=(255, 255, 0, 255))

    out = io.BytesIO()
    mime = "image/jpeg" if image_type == "jpeg" else "image/png"
    save_fmt = "JPEG" if image_type == "jpeg" else "PNG"
    if save_fmt == "JPEG":
        img = img.convert("RGB")
    img.save(out, format=save_fmt, quality=shot_kwargs.get("quality", 82) if save_fmt == "JPEG" else None)
    return out.getvalue(), mime, marks


def format_marked_screenshot_map(marks: list[dict[str, Any]], viewport: dict[str, Any]) -> str:
    """Format SoM index map for the agent."""
    lines = [
        f"viewport: {viewport.get('width', 0)}x{viewport.get('height', 0)}",
        f"coordinate_space: {viewport.get('coordinate_space', 'viewport_css_pixels')}",
        "Use browser_click_at with the center coordinates for the chosen index.",
        "",
    ]
    for mark in marks:
        center = mark.get("center") or {}
        lines.append(
            f"{mark.get('index')}: {mark.get('text', '')!r} "
            f"center=({center.get('x', '')},{center.get('y', '')}) "
            f"ref={mark.get('ref', '')} selector={mark.get('selector', '')}"
        )
    return "\n".join(lines)


def screenshot_metadata_preamble(page, viewport: dict[str, Any] | None = None) -> str:
    """Text metadata to prepend to browser_screenshot results."""
    url = getattr(page, "url", "") or ""
    vp = viewport or {}
    return (
        f"Screenshot captured from {url or 'current page'}\n"
        f"viewport: {vp.get('width', 0)}x{vp.get('height', 0)}\n"
        f"device_scale_factor: {vp.get('device_scale_factor', 1)}\n"
        f"coordinate_space: {vp.get('coordinate_space', 'viewport_css_pixels')}\n"
        "Use viewport coordinates (not full-page offsets) with browser_click_at."
    )
