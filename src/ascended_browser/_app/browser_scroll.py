"""Targeted native scrolling with independent position readback.

Called under the manager's existing tab lock and runtime effect barrier. Native
scrollBy fires normal scroll events without depending on the last mouse position.
"""
from __future__ import annotations

SCROLL_SCRIPT = r"""async (target, args) => {
  const doc = target.ownerDocument, win = doc.defaultView, root = doc.scrollingElement;
  function scrollable(el) {
    return el && el.scrollHeight > el.clientHeight + 1 &&
      (el === root || /auto|scroll/.test(win.getComputedStyle(el).overflowY));
  }
  let container;
  if (args.targeted) {
    for (let el = target; el; el = el.parentElement) {
      if (scrollable(el)) { container = el; break; }
    }
  } else if (scrollable(root)) {
    container = root;
  } else {
    const candidates = Array.from(doc.querySelectorAll('body *')).filter(el => {
      if (!scrollable(el)) return false;
      const r = el.getBoundingClientRect(), s = win.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
        r.top < win.innerHeight && r.left < win.innerWidth && s.visibility !== 'hidden';
    });
    candidates.sort((a,b) => {
      const area = el => { const r = el.getBoundingClientRect();
        return Math.max(0, Math.min(r.right,win.innerWidth)-Math.max(r.left,0)) *
          Math.max(0, Math.min(r.bottom,win.innerHeight)-Math.max(r.top,0)); };
      return area(b)-area(a);
    });
    container = candidates[0];
  }
  // A short document still has a meaningful boundary; an explicit target
  // never silently switches to an unrelated nested container.
  container = container || root;
  if (!container || !container.isConnected) throw new Error('Scroll target is unavailable; observe this tab again.');
  const measure = () => ({top: container.scrollTop,
    max_top: Math.max(0, container.scrollHeight-container.clientHeight), height: container.clientHeight});
  const before = measure();
  container.scrollBy({top: args.delta_y, left: 0, behavior: 'instant'});
  await new Promise(resolve => win.setTimeout(resolve, 100));
  if (!container.isConnected) throw new Error('Scroll container was replaced; observe this tab again before continuing.');
  const after = measure(), moved = after.top-before.top;
  const boundary = args.delta_y < 0 ? after.top <= 1 : after.top >= after.max_top-1;
  return {before, after, moved, changed: Math.abs(moved) > 0.5,
    state: Math.abs(moved) > 0.5 ? 'moved' : boundary ? (args.delta_y < 0 ? 'at_start' : 'at_end') : 'stalled',
    document: container === root,
    node_token: win[Symbol.for('odysseus.browser.nodes')]?.get(container) || ''};
}"""


async def scroll_once(root, selector: str, *, delta_y: int, targeted: bool) -> dict:
    return await root.locator(selector).evaluate(
        SCROLL_SCRIPT, {"delta_y": delta_y, "targeted": targeted}, timeout=3000,
    )
