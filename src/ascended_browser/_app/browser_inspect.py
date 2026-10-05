"""Why an observed element behaves the way it does: box, hit target, styles.

The widget probe's worst class is a control that looks interactive and is
not: a readonly input behind a date picker, a button under a cookie banner,
a row parked outside its scroll container. Until now those read as a stalled
deadline or "cannot be clicked: it is not visible", and the model's next move
was a screenshot, which carries no numbers, or page JavaScript.

``inspect_locator`` asks the page one question about one element and
answers in the terms a developer would use at the devtools Elements panel:
the bounding box and whether it is in the viewport, the element that would
actually receive a click at its center (and what covers it when that is
something else), the ancestor that hides or clips it, disabled/readonly/
inert state, its scroll container chain, a computed style subset and the
handlers it declares. The Python side turns that into ``hittable`` plus plain
``reasons`` and a ``next_step``, and maps covering and clipping elements back
to refs the model can act on.

It is read-only (one ``evaluate`` with no side effects) and used in two
places: ``browser_extract read="inspect"`` on request, and automatically on a
click the control refused as unreachable, so the refusal says why.
"""
from __future__ import annotations

import asyncio
from typing import Any

#: One in-page read of one element. Arg: ``{sensitive: bool}``.
INSPECT_SCRIPT = r"""(el, opts) => {
  const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const tokens = window[Symbol.for('odysseus.browser.nodes')];
  const tokenOf = node => { try { return tokens && tokens.get ? String(tokens.get(node) || '') : ''; } catch (e) { return ''; } };
  const describe = node => {
    if (!node || node.nodeType !== 1) return null;
    let cs; try { cs = getComputedStyle(node); } catch (e) { cs = {}; }
    const out = {tag: node.tagName.toLowerCase()};
    if (node.id) out.id = node.id;
    const classes = Array.from(node.classList || []).slice(0, 4);
    if (classes.length) out.classes = classes;
    const role = node.getAttribute('role'); if (role) out.role = role;
    const text = clip(node.innerText || node.textContent, 60); if (text) out.text = text;
    if (cs.position && cs.position !== 'static') out.position = cs.position;
    if (cs.zIndex && cs.zIndex !== 'auto') out.z_index = cs.zIndex;
    const token = tokenOf(node); if (token) out.node_token = token;
    return out;
  };
  const cs = getComputedStyle(el);
  const vw = window.innerWidth, vh = window.innerHeight;
  const measure = () => {
    const r = el.getBoundingClientRect();
    return {rect: r, box: {x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height)},
      inViewport: r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw};
  };
  let m = measure();
  const before = m.box;
  // A click scrolls its target into view before the hit test, so answer the
  // question the click would face: scroll it into view, measure, scroll back.
  let scrolledToMeasure = false;
  const savedScroll = [];
  if (!m.inViewport && m.rect.width > 0 && m.rect.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden') {
    for (let node = el.parentElement; node; node = node.parentElement) savedScroll.push([node, node.scrollTop, node.scrollLeft]);
    savedScroll.push([window, window.scrollY, window.scrollX]);
    try { el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'}); } catch (e) { try { el.scrollIntoView(); } catch (e2) {} }
    m = measure();
    scrolledToMeasure = true;
  }
  const rect = m.rect, box = m.box, inViewport = m.inViewport;
  const fullyInViewport = inViewport && rect.top >= 0 && rect.left >= 0 && rect.bottom <= vh && rect.right <= vw;

  let hiddenBy = null, clippedBy = null, closedDetails = null, inert = false;
  const scrollParents = [];
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    let s; try { s = node === el ? cs : getComputedStyle(node); } catch (e) { continue; }
    if (!hiddenBy) {
      const why = s.display === 'none' ? 'display:none' : s.visibility === 'hidden' ? 'visibility:hidden'
        : s.visibility === 'collapse' ? 'visibility:collapse' : parseFloat(s.opacity) === 0 ? 'opacity:0' : '';
      if (why) hiddenBy = Object.assign(describe(node) || {}, {why, self: node === el});
    }
    if (node !== el) {
      const overflow = s.overflow + ' ' + s.overflowX + ' ' + s.overflowY;
      const scrolls = /(auto|scroll)/.test(overflow) && (node.scrollHeight > node.clientHeight + 1 || node.scrollWidth > node.clientWidth + 1);
      if (scrolls && scrollParents.length < 3) {
        scrollParents.push(Object.assign(describe(node) || {}, {
          scroll_top: Math.round(node.scrollTop), scroll_left: Math.round(node.scrollLeft),
          scroll_height: node.scrollHeight, client_height: node.clientHeight,
        }));
      }
      if (!clippedBy && /(hidden|auto|scroll|clip)/.test(overflow) && rect.width > 0 && rect.height > 0) {
        const r = node.getBoundingClientRect();
        if (rect.bottom <= r.top || rect.top >= r.bottom || rect.right <= r.left || rect.left >= r.right) {
          clippedBy = Object.assign(describe(node) || {}, {
            scrolls, scroll_top: Math.round(node.scrollTop), scroll_height: node.scrollHeight, client_height: node.clientHeight,
            box: {x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height)},
          });
        }
      }
      if (node.tagName === 'DETAILS' && !node.open) {
        const summary = node.querySelector(':scope > summary');
        if (!summary || !summary.contains(el)) closedDetails = describe(node);
      }
    }
    if (node.inert) inert = true;
  }

  const candidates = [
    [rect.left + rect.width / 2, rect.top + rect.height / 2],
    [rect.left + 2, rect.top + 2], [rect.right - 2, rect.top + 2],
    [rect.left + 2, rect.bottom - 2], [rect.right - 2, rect.bottom - 2],
  ];
  let hit = null;
  if (rect.width > 0 && rect.height > 0) {
    for (const [x, y] of candidates) {
      if (x < 0 || y < 0 || x >= vw || y >= vh) continue;
      let top; try { top = document.elementFromPoint(x, y); } catch (e) { top = null; }
      if (!top) continue;
      if (top.shadowRoot && typeof top.shadowRoot.elementFromPoint === 'function') {
        try { top = top.shadowRoot.elementFromPoint(x, y) || top; } catch (e) {}
      }
      const owns = top === el || el.contains(top)
        || (top.contains(el) && (top.tagName === 'LABEL' || (top.tagName === 'BUTTON' || top.tagName === 'A')));
      if (owns) { hit = {ok: true, at: [Math.round(x), Math.round(y)]}; break; }
      if (!hit) {
        const cover = describe(top);
        // What on the cover could dismiss it: its own buttons and links.
        try {
          const root = top.closest('[role=dialog], [role=alertdialog], dialog, [aria-modal=true]') || top;
          const scope = root.contains(el) ? top : root;
          cover.controls = Array.from(scope.querySelectorAll('button, a[href], [role=button], input[type=button], input[type=submit], summary'))
            .filter(c => c.getClientRects().length)
            .slice(0, 4)
            .map(c => ({tag: c.tagName.toLowerCase(), text: clip(c.innerText || c.getAttribute('aria-label') || c.value, 40), node_token: tokenOf(c)}));
        } catch (e) {}
        hit = {ok: false, at: [Math.round(x), Math.round(y)], covered_by: cover};
      }
    }
  }
  if (!hit) hit = {ok: false, reason: rect.width > 0 && rect.height > 0 ? 'no point of the element is inside the viewport' : 'the element has no size'};

  const type = (el.getAttribute('type') || '').toLowerCase();
  const sensitive = opts && opts.sensitive;
  let value;
  if ('value' in el && !el.matches('button, [type=button], [type=submit], [type=reset]')) {
    value = sensitive || type === 'password' || type === 'hidden' || type === 'file' ? '[redacted]' : clip(String(el.value == null ? '' : el.value), 120);
  }
  const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map(id => (document.getElementById(id) || {}).textContent || '').join(' ');
  const labels = el.labels && el.labels.length ? Array.from(el.labels).map(l => l.textContent || '').join(' ') : '';
  const name = el.getAttribute('aria-label') || labelledBy || labels || el.getAttribute('title')
    || el.getAttribute('alt') || el.getAttribute('placeholder') || el.innerText || el.textContent || '';
  const describedBy = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)
    .map(id => (document.getElementById(id) || {}).textContent || '').join(' ');
  const handlerNames = ['onclick', 'onmousedown', 'onpointerdown', 'onkeydown', 'onchange', 'oninput', 'onsubmit', 'onfocus'];
  const handlers = handlerNames.filter(h => typeof el[h] === 'function' || el.hasAttribute(h)).map(h => h.slice(2));
  const ancestors = [];
  for (let node = el.parentElement, depth = 0; node && depth < 6; node = node.parentElement, depth++) {
    let s = node.tagName.toLowerCase();
    if (node.id) s += '#' + node.id;
    else if (node.classList && node.classList.length) s += '.' + Array.from(node.classList).slice(0, 2).join('.');
    ancestors.push(s);
  }
  const form = el.form || el.closest('form');
  const aria = {};
  for (const key of ['expanded', 'controls', 'haspopup', 'pressed', 'selected', 'invalid', 'checked', 'hidden', 'busy', 'live']) {
    const v = el.getAttribute('aria-' + key); if (v != null) aria[key] = v;
  }
  const disabled = el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true';
  const readonly = el.readOnly === true || el.getAttribute('aria-readonly') === 'true';
  const focusable = !disabled && (el.tabIndex >= 0 || el.matches('a[href], button, input, select, textarea, summary, [contenteditable=""], [contenteditable="true"]'));
  const pointerEventsNone = cs.pointerEvents === 'none';
  if (scrolledToMeasure) {
    for (const [node, top, left] of savedScroll) {
      try { if (node === window) window.scrollTo(left, top); else { node.scrollTop = top; node.scrollLeft = left; } } catch (e) {}
    }
  }
  return {
    scrolled_to_measure: scrolledToMeasure, box_before_scroll: scrolledToMeasure ? before : undefined,
    tag: el.tagName.toLowerCase(), id: el.id || '', classes: Array.from(el.classList || []).slice(0, 6),
    role: el.getAttribute('role') || '', type, name: clip(name, 120), text: clip(el.innerText || el.textContent, 120),
    value, checked: 'checked' in el ? !!el.checked : undefined, href: el.href || undefined,
    box, in_viewport: inViewport, fully_in_viewport: fullyInViewport, viewport: {width: vw, height: vh},
    style: {
      display: cs.display, position: cs.position, z_index: cs.zIndex, opacity: cs.opacity,
      visibility: cs.visibility, pointer_events: cs.pointerEvents, cursor: cs.cursor, overflow: cs.overflow,
      color: cs.color, background_color: cs.backgroundColor, font_size: cs.fontSize,
      font_family: (cs.fontFamily || '').split(',')[0].replace(/["']/g, '').trim(), font_weight: cs.fontWeight,
      width: cs.width, height: cs.height, padding: cs.padding, margin: cs.margin, border: cs.border,
      border_radius: cs.borderRadius, transform: cs.transform,
    },
    hidden_by: hiddenBy, clipped_by: clippedBy, closed_details: closedDetails, inert, hit,
    disabled, readonly, focusable, focused: document.activeElement === el, pointer_events_none: pointerEventsNone,
    handlers, ancestors, aria, described_by: clip(describedBy, 200),
    form: form ? {id: form.id || '', name: form.getAttribute('name') || '', action: form.getAttribute('action') || '', method: (form.getAttribute('method') || 'get').toLowerCase()} : null,
    scroll_parents: scrollParents, node_token: tokenOf(el),
  };
}"""

_SENSITIVE_TOKENS = frozenset({
    "current-password", "new-password", "one-time-code", "cc-number", "cc-csc", "cc-exp",
    "cc-exp-month", "cc-exp-year",
})


def _label(node: dict | None) -> str:
    if not node:
        return "?"
    text = f"<{node.get('tag', '?')}"
    if node.get("id"):
        text += f"#{node['id']}"
    elif node.get("classes"):
        text += "." + ".".join(node["classes"][:2])
    text += ">"
    if node.get("role"):
        text += f" role={node['role']}"
    if node.get("text"):
        text += f" {node['text']!r}"
    if node.get("ref"):
        text += f" ref={node['ref']}"
    if node.get("position") in {"fixed", "sticky", "absolute"}:
        text += f" ({node['position']}" + (f", z-index {node['z_index']}" if node.get("z_index") else "") + ")"
    return text


def _attach_refs(data: dict, candidates: list[dict]) -> None:
    """Covering and clipping elements the model can act on by ref."""
    by_token = {str(c.get("node_token") or ""): str(c.get("ref") or "") for c in candidates if c.get("node_token")}
    nodes = [data.get("hit", {}).get("covered_by"), data.get("hidden_by"), data.get("clipped_by"), data.get("closed_details")]
    nodes.extend(data.get("scroll_parents") or [])
    for node in nodes:
        if isinstance(node, dict) and node.get("node_token") in by_token:
            node["ref"] = by_token[node["node_token"]]
        for control in (node.get("controls") or []) if isinstance(node, dict) else []:
            if control.get("node_token") in by_token:
                control["ref"] = by_token[control["node_token"]]
            control.pop("node_token", None)
    if data.get("node_token") in by_token:
        data["current_ref"] = by_token[data["node_token"]]


def interpret(data: dict) -> dict[str, Any]:
    """``hittable``, ``reasons`` and ``next_step`` from the raw read."""
    reasons: list[str] = []
    steps: list[str] = []
    hit = data.get("hit") or {}
    box = data.get("box") or {}
    if data.get("hidden_by"):
        node = data["hidden_by"]
        where = "itself" if node.get("self") else _label(node)
        reasons.append(f"hidden: {node.get('why')} on {where}")
        if node.get("self") and data.get("tag") == "input" and data.get("type") in {"checkbox", "radio", "file"}:
            steps.append("A styled control: operate its visible label or the button beside it, or use kind=check / upload on this ref.")
        else:
            steps.append("Open or reveal the section that contains it first (a tab, accordion, menu or dialog), then observe again.")
    if data.get("closed_details"):
        reasons.append(f"inside a closed {_label(data['closed_details'])}")
        steps.append("Click that <details> summary first.")
    if data.get("inert"):
        reasons.append("inside an inert subtree (a modal elsewhere owns input)")
        steps.append("Close the open dialog or overlay, then observe again.")
    if not box.get("width") or not box.get("height"):
        if not data.get("hidden_by"):
            reasons.append("zero size")
    elif data.get("clipped_by"):
        node = data["clipped_by"]
        reasons.append(f"outside its scroll container {_label(node)}" if node.get("scrolls")
                       else f"clipped by {_label(node)} (overflow hidden)")
        if node.get("scrolls"):
            steps.append("Scroll that container (kind=scroll with its ref) until the element is inside it, then observe again.")
    elif not data.get("in_viewport"):
        reasons.append(f"outside the viewport even after scrolling to it (box {box.get('x')},{box.get('y')} {box.get('width')}x{box.get('height')}, viewport {data.get('viewport', {}).get('width')}x{data.get('viewport', {}).get('height')})")
        steps.append("Scroll its container (kind=scroll) until it is on screen, then observe again.")
    if box.get("width") and box.get("height") and data.get("in_viewport") and not hit.get("ok"):
        covered = hit.get("covered_by")
        if covered:
            reasons.append(f"covered by {_label(covered)}")
            dismissers = [c for c in covered.get("controls") or [] if c.get("ref")]
            named = ", ".join(f"{c.get('text') or c.get('tag')!r} ref={c['ref']}" for c in dismissers[:3])
            if dismissers and covered.get("position") in {"fixed", "sticky", "absolute"}:
                steps.append(f"Dismiss or close that overlay first: it has {named}. Or scroll so the element is clear of it.")
            elif covered.get("position") in {"fixed", "sticky", "absolute"}:
                steps.append("Dismiss or close that overlay first (its ref if shown), or scroll so the element is clear of it.")
            elif dismissers:
                steps.append(f"Act on the covering element if it is the real control ({named}), otherwise scroll or close what is on top.")
            else:
                steps.append("Act on the covering element if it is the real control, otherwise scroll or close what is on top.")
        elif hit.get("reason"):
            reasons.append(hit["reason"])
    if data.get("pointer_events_none"):
        reasons.append("pointer-events: none (clicks pass through it)")
        steps.append("Act on the element underneath or the one that owns the behavior.")
    if data.get("disabled"):
        reasons.append("disabled")
        steps.append("Complete the fields it depends on or wait for the page to enable it.")
    if data.get("readonly"):
        reasons.append("readonly")
        if data.get("tag") == "input":
            steps.append("It cannot be typed into: it displays a picker's value. Use kind=date or kind=select on it, or click it to open the picker.")
    hittable = bool(hit.get("ok")) and not data.get("hidden_by") and not data.get("disabled") and not data.get("inert")
    next_step = steps[0] if steps else ("" if hittable else "Observe the tab again and act on what is shown.")
    notes: list[str] = []
    if data.get("scrolled_to_measure"):
        before = data.get("box_before_scroll") or {}
        vp = data.get("viewport") or {}
        notes.append(
            f"off screen now (box {before.get('x')},{before.get('y')} {before.get('width')}x{before.get('height')}, "
            f"viewport {vp.get('width')}x{vp.get('height')}); measured after scrolling it into view and scrolled back. "
            "A click scrolls it into view itself."
        )
    return {"hittable": hittable, "reasons": reasons, "next_step": next_step, "notes": notes}


def format_inspection(result: dict) -> str:
    """What the model reads: a verdict, then the facts behind it."""
    element = result.get("element") or {}
    lines = []
    head = f"{_label(element)}"
    if element.get("name") and element.get("name") != element.get("text"):
        head += f" name={element['name']!r}"
    lines.append(head)
    verdict = "hittable" if result.get("hittable") else "NOT hittable"
    if result.get("reasons"):
        verdict += (" (note: " if result.get("hittable") else ": ") + "; ".join(result["reasons"]) + (")" if result.get("hittable") else "")
    lines.append(verdict)
    if result.get("next_step"):
        lines.append("next: " + result["next_step"])
    for note in result.get("notes") or []:
        lines.append("note: " + note)
    box = element.get("box") or {}
    vp = element.get("viewport") or {}
    lines.append(
        f"box: x={box.get('x')} y={box.get('y')} {box.get('width')}x{box.get('height')} "
        f"(viewport {vp.get('width')}x{vp.get('height')}, "
        + ("fully visible" if element.get("fully_in_viewport") else "partly visible" if element.get("in_viewport") else "off screen") + ")"
    )
    hit = element.get("hit") or {}
    if hit.get("ok"):
        lines.append(f"hit test: receives the click at {hit.get('at')}")
    elif hit.get("covered_by"):
        lines.append(f"hit test at {hit.get('at')}: {_label(hit['covered_by'])} is on top")
        controls = [c for c in hit["covered_by"].get("controls") or [] if c.get("ref")]
        if controls:
            lines.append("its controls: " + ", ".join(f"{c.get('text') or c.get('tag')!r} ref={c['ref']}" for c in controls))
    state = [k for k in ("disabled", "readonly", "focused", "inert") if element.get(k)]
    if element.get("checked") is not None:
        state.append("checked" if element.get("checked") else "unchecked")
    if element.get("value") not in (None, ""):
        state.append(f"value={element['value']!r}")
    if state:
        lines.append("state: " + ", ".join(state))
    style = element.get("style") or {}
    shown = {k: style.get(k) for k in ("display", "position", "z_index", "opacity", "pointer_events", "cursor",
                                        "color", "background_color", "font_size", "font_family", "font_weight",
                                        "padding", "margin", "border_radius") if style.get(k) not in (None, "", "auto", "none", "normal", "0px", "static")}
    if shown:
        lines.append("style: " + ", ".join(f"{k}={v}" for k, v in shown.items()))
    if element.get("handlers"):
        lines.append("handlers: " + ", ".join(element["handlers"]))
    aria = element.get("aria") or {}
    if aria:
        lines.append("aria: " + ", ".join(f"{k}={v}" for k, v in aria.items()))
    if element.get("described_by"):
        lines.append(f"described by: {element['described_by']}")
    if element.get("form"):
        form = element["form"]
        lines.append(f"form: {form.get('method', 'get').upper()} {form.get('action') or '(same page)'}" + (f" #{form['id']}" if form.get("id") else ""))
    if element.get("scroll_parents"):
        parts = []
        for node in element["scroll_parents"]:
            parts.append(f"{_label(node)} at {node.get('scroll_top')}/{max(0, int(node.get('scroll_height') or 0) - int(node.get('client_height') or 0))}")
        lines.append("scroll containers: " + "; ".join(parts))
    if element.get("ancestors"):
        lines.append("ancestors: " + " > ".join(reversed(element["ancestors"])))
    return "\n".join(lines)


def _is_sensitive(identity: dict | None) -> bool:
    if not identity:
        return False
    if identity.get("sensitive") is True or str(identity.get("type") or "").casefold() == "password":
        return True
    autocomplete = str(identity.get("autocomplete") or "").casefold().split()
    return any(token in _SENSITIVE_TOKENS for token in autocomplete)


async def inspect_locator(locator: Any, *, candidates: list[dict] | None = None,
                          sensitive: bool = False, timeout: float = 5.0) -> dict[str, Any]:
    """One read of the element the locator names, interpreted."""
    data = await asyncio.wait_for(
        locator.evaluate(INSPECT_SCRIPT, {"sensitive": bool(sensitive)}, timeout=int(timeout * 1000)),
        timeout=timeout + 1.0,
    )
    if not isinstance(data, dict):
        raise RuntimeError("The page returned no inspection result.")
    _attach_refs(data, candidates or [])
    data.pop("node_token", None)
    verdict = interpret(data)
    return {**verdict, "element": data}


def compact_inspection(result: dict) -> dict[str, Any]:
    """The part worth carrying on a failed click: verdict, box, what is on top."""
    element = result.get("element") or {}
    out: dict[str, Any] = {
        "hittable": result.get("hittable"),
        "reasons": list(result.get("reasons") or []),
    }
    if result.get("next_step"):
        out["next_step"] = result["next_step"]
    if result.get("notes"):
        out["notes"] = list(result["notes"])
    if element.get("box"):
        out["box"] = element["box"]
    hit = element.get("hit") or {}
    if hit.get("covered_by"):
        covered = {k: v for k, v in hit["covered_by"].items() if k != "node_token"}
        out["covered_by"] = covered
    for key in ("hidden_by", "clipped_by", "closed_details"):
        if element.get(key):
            out[key] = {k: v for k, v in element[key].items() if k != "node_token"}
    return out
