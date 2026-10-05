"""Open shadow roots are part of the page the person sees; read through them.

Every page script the browser tools run used ``document.querySelectorAll`` and
``document.body.innerText``. Both stop at a shadow boundary. A page built from
web components (LinkedIn's shell renders the whole feed inside one open shadow
root; design systems such as Shoelace, Lit, Ionic, Vaadin and Salesforce
Lightning put every control in one) therefore observed as empty: no controls,
no content blocks, no text to extract. Measured on ten LinkedIn runs, the
agent spent 25-57 ``browser_evaluate`` calls per run writing its own
``shadowRoot`` walkers to find the posts the observation did not show.

One owner for the traversal, spliced into each collector as JavaScript source
so that the observation, the viewport reading, the selector query, the text
extraction and the postcondition checks all see the same document. Playwright
locators already pierce open shadow roots for the ``[data-odysseus-browser-ref]``
selector the collector stamps, so an element found here is also clickable.
Closed shadow roots stay closed: a page that hides a tree from scripts hides
it from this agent too.
"""
from __future__ import annotations

import asyncio
from typing import Any

#: JavaScript helpers, declared with ``function`` so they hoist wherever the
#: source is spliced. Names carry the ``ody`` prefix so no page global or
#: existing collector local is shadowed.
SHADOW_DOM_JS = r"""
  // Visit `root` and every open shadow root beneath it, hosts before their
  // shadow content, bounded so a 200k-node page cannot stall a probe.
  function odyRoots(root, limit) {
    const roots = [root || document];
    const cap = limit || 3000;
    for (let i = 0; i < roots.length && roots.length < cap; i += 1) {
      let hosts;
      try { hosts = roots[i].querySelectorAll('*'); } catch (e) { continue; }
      for (const host of hosts) {
        if (host.shadowRoot) {
          roots.push(host.shadowRoot);
          if (roots.length >= cap) break;
        }
      }
    }
    return roots;
  }
  // querySelectorAll across the light tree and every open shadow tree, in
  // document order (a host's shadow content follows the host).
  function odyQueryAll(root, selector) {
    const start = root || document;
    // Validates the selector first: an invalid one throws here exactly as
    // querySelectorAll would, so callers keep their error handling.
    const plain = start.querySelectorAll(selector);
    const all = start.querySelectorAll('*');
    let hosted = false;
    for (const el of all) { if (el.shadowRoot) { hosted = true; break; } }
    if (!hosted) return Array.from(plain);
    const out = [];
    const visit = (nodes) => {
      for (const el of nodes) {
        if (out.length >= 20000) return;
        if (el.matches(selector)) out.push(el);
        if (el.shadowRoot) visit(el.shadowRoot.querySelectorAll('*'));
      }
    };
    visit(all);
    return out;
  }
  // getElementById from the tree `from` lives in (a shadow root keeps its
  // own id scope), then the document.
  function odyById(from, id) {
    if (!id) return null;
    try {
      const root = from && from.getRootNode ? from.getRootNode() : document;
      if (root && typeof root.getElementById === 'function') {
        const hit = root.getElementById(id);
        if (hit) return hit;
      }
    } catch (e) {}
    return document.getElementById(id);
  }
  // Text nodes under `root`, entering open shadow roots, in document order.
  function odyTextNodes(root, limit) {
    const out = [];
    const cap = limit || 20000;
    const walk = (node) => {
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      let cur = walker.currentNode;
      if (cur && cur.nodeType === 3) out.push(cur);
      while ((cur = walker.nextNode()) && out.length < cap) {
        if (cur.nodeType === 3) { out.push(cur); continue; }
        if (cur.shadowRoot) walk(cur.shadowRoot);
      }
    };
    walk(root || document.body || document.documentElement);
    return out;
  }
  // The words a component shows for a control it renders in its shadow root
  // come through a <slot> from the host's light children (<sl-button>Save
  // </sl-button> renders <button><slot></slot></button>). innerText of the
  // inner button is empty because slotted nodes are not its descendants.
  function odySlottedText(el) {
    if (!el || !el.getRootNode || !el.getRootNode().host) return '';
    const parts = [];
    for (const slot of [el, ...el.querySelectorAll('slot')]) {
      if (slot.tagName !== 'SLOT' || typeof slot.assignedNodes !== 'function') continue;
      for (const node of slot.assignedNodes({flatten: true})) {
        if (node.nodeType === 3) parts.push(node.textContent || '');
        else if (node.nodeType === 1) parts.push(node.innerText || node.textContent || '');
      }
    }
    return parts.join(' ').replace(/\s+/g, ' ').trim();
  }
  // innerText that reads through open shadow roots. Subtrees without a shadow
  // host keep the engine's innerText (CSS-aware: hidden text stays out); a
  // host contributes its shadow tree's rendered text in place of its light
  // children, with <slot>s expanded to what they project.
  function odyInnerText(root) {
    const start = root || document.body;
    if (!start) return '';
    const hosts = new Set();
    for (const r of odyRoots(start)) {
      if (r.host) hosts.add(r.host);
    }
    if (!hosts.size) return start.innerText || '';
    const touched = new Set();
    for (const host of hosts) {
      for (let n = host; n; n = n.parentElement || (n.getRootNode && n.getRootNode().host) || null) {
        if (touched.has(n)) break;
        touched.add(n);
      }
    }
    const shown = (el) => {
      try {
        const s = getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden';
      } catch (e) { return true; }
    };
    const blockish = /^(ADDRESS|ARTICLE|ASIDE|BLOCKQUOTE|DD|DETAILS|DIV|DL|DT|FIELDSET|FIGCAPTION|FIGURE|FOOTER|FORM|H[1-6]|HEADER|HR|LI|MAIN|NAV|OL|P|PRE|SECTION|SUMMARY|TABLE|TR|TD|TH|UL|BR)$/;
    const skip = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/;
    const parts = [];
    const read = (node, depth) => {
      if (depth > 60) return;
      for (const child of node.childNodes) {
        if (child.nodeType === 3) { parts.push(child.textContent || ''); continue; }
        if (child.nodeType !== 1) continue;
        if (skip.test(child.tagName)) continue;
        if (!shown(child)) continue;
        const block = blockish.test(child.tagName);
        if (block) parts.push('\n');
        if (child.tagName === 'SLOT' && typeof child.assignedNodes === 'function') {
          for (const assigned of child.assignedNodes({flatten: true})) {
            if (assigned.nodeType === 3) parts.push(assigned.textContent || '');
            else if (assigned.nodeType === 1 && shown(assigned)) element(assigned, depth + 1);
          }
          continue;
        }
        element(child, depth + 1);
        if (block) parts.push('\n');
      }
    };
    const element = (el, depth) => {
      if (el.shadowRoot) { read(el.shadowRoot, depth); return; }
      if (!touched.has(el)) { parts.push(el.innerText || ''); return; }
      read(el, depth);
    };
    element(start, 0);
    return parts.join('').replace(/[ \t\f\v]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  }
"""

#: ``page.evaluate`` source for the page's readable text, through shadow roots.
DEEP_INNER_TEXT_SCRIPT = "() => {" + SHADOW_DOM_JS + " return odyInnerText(document.body); }"


def with_shadow_helpers(script: str) -> str:
    """Splice the helpers into an arrow-function script ``() => { ... }``."""
    marker = "{"
    head, sep, tail = script.partition(marker)
    if not sep:
        return script
    return head + sep + SHADOW_DOM_JS + tail


async def deep_inner_text(target: Any, *, timeout: float = 4.0) -> str:
    """The body's readable text including open shadow trees; the engine's
    ``innerText`` when the helper cannot run (a frameset, a closed page)."""
    try:
        text = await asyncio.wait_for(target.evaluate(DEEP_INNER_TEXT_SCRIPT), timeout)
        if isinstance(text, str) and text.strip():
            return text
    except Exception:
        pass
    try:
        return str(await target.locator("body").inner_text(timeout=int(timeout * 1000)))
    except Exception:
        return ""
