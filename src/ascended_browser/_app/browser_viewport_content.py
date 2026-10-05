"""Bounded reading evidence collected with the controls, in the same document scan.

Tokens are issued by the existing collector and registered in its scoped ref
registry. Text is a viewport reading, not a replacement for full extraction.
"""

from ascended_browser._app.browser_shadow_dom import SHADOW_DOM_JS

CONTENT_BLOCK_MAX_CHARS = 8_000

VIEWPORT_SIGNATURE_FUNCTION = r"""
  function viewportSignature() {
    // Read current DOM state rather than relying on observer callback delivery.
    // The fingerprint stays internal; neither markup nor secret values leave
    // this probe. Original WeakMap identity detects clones carrying old attrs.
    let hash = 2166136261;
    const add = value => { const text = String(value); for (let i=0;i<text.length;i++) {
      hash = Math.imul(hash ^ text.charCodeAt(i), 16777619); } };
    const markup = document.documentElement?.outerHTML || '';
    add(markup); add(innerWidth); add(innerHeight); add(scrollX); add(scrollY);
    const identities = window[Symbol.for('odysseus.browser.nodes')];
    for (const el of odyQueryAll(document, '[data-odysseus-browser-ref]')) {
      const rect = el.getBoundingClientRect();
      add(identities?.get(el) || 'replaced');
      add([rect.x,rect.y,rect.width,rect.height,el.scrollTop,el.scrollLeft].join(','));
    }
    // The document's markup stops at shadow boundaries: a feed that loads
    // more posts inside a shadow root changes nothing above. Fold in each
    // open shadow tree's size and the position of its marked elements.
    for (const root of odyRoots(document, 200)) {
      if (root.host) add((root.innerHTML || '').length);
    }
    return markup.length + ':' + (hash >>> 0).toString(16);
  }
"""
VIEWPORT_SIGNATURE_PROBE = "() => {" + SHADOW_DOM_JS + VIEWPORT_SIGNATURE_FUNCTION + "return viewportSignature();}"

VIEWPORT_CONTENT_JS = VIEWPORT_SIGNATURE_FUNCTION + r"""
  function readingIdentity(el, kind) {
    let token = nodes.get(el);
    if (!token) {
      const bytes = new Uint32Array(4); crypto.getRandomValues(bytes);
      token = Array.from(bytes, n => n.toString(16).padStart(8, '0')).join('');
      nodes.set(el, token);
    }
    el.setAttribute('data-odysseus-browser-ref', token);
    return {_reading_kind: kind, node_token: token,
      selector: '[data-odysseus-browser-ref="' + token + '"]',
      tag: el.tagName.toLowerCase(), id: el.id || '', visible: true,
      in_viewport: true, text: '', bbox: readingBox(el)};
  }
  function readingBox(el) {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.left), y: Math.round(r.top),
      width: Math.round(r.width), height: Math.round(r.height)};
  }
  function readingVisible(el, rect) {
    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
    let left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
    let top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (p.hidden || p.inert || s.display === 'none' || s.visibility === 'hidden'
          || s.visibility === 'collapse' || Number(s.opacity) === 0) return false;
      if (p !== el && /auto|scroll|hidden|clip/.test(s.overflow + s.overflowX + s.overflowY)) {
        const r = p.getBoundingClientRect();
        left = Math.max(left, r.left); right = Math.min(right, r.right);
        top = Math.max(top, r.top); bottom = Math.min(bottom, r.bottom);
      }
    }
    return right > left && bottom > top;
  }
  const readingGroups = new Map();
  let readingComplete = true, readingChars = 0, readingNodes = 0;
  function readingGroup(el) {
    const root = el.closest('article,[role="article"],[role="listitem"],li')
      || el.closest('p,pre,blockquote,h1,h2,h3,h4,h5,h6,td,th') || el;
    if (!readingGroups.has(root)) {
      if (readingGroups.size >= 40) { readingComplete = false; return null; }
      readingGroups.set(root, {root, text: [], media: [], chars: 0, truncated: false});
    }
    return readingGroups.get(root);
  }
  for (const textNode of odyTextNodes(document.body || document.documentElement, 20001)) {
    if (++readingNodes > 20000) { readingComplete = false; break; }
    const el = textNode.parentElement;
    if (!el || el.closest('script,style,noscript,template,textarea,input,select,[contenteditable="true"]')) continue;
    const text = (textNode.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const range = document.createRange(); range.selectNodeContents(textNode);
    if (!Array.from(range.getClientRects()).some(r => readingVisible(el, r))) continue;
    const group = readingGroup(el); if (!group) continue;
    const available = Math.max(0, Math.min(CONTENT_BLOCK_LIMIT - group.chars, 10000 - readingChars));
    if (text.length > available) { group.truncated = true; readingComplete = false; }
    if (available) {
      group.text.push(text.slice(0, available));
      group.chars += Math.min(text.length, available); readingChars += Math.min(text.length, available);
    }
  }
  for (const el of odyQueryAll(document, 'img,video,canvas,[role="img"]')) {
    if (!readingVisible(el, el.getBoundingClientRect())) continue;
    const group = readingGroup(el); if (!group || group.media.length >= 8) continue;
    group.media.push({type: el.tagName.toLowerCase(),
      description: (el.getAttribute('alt') || el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 300)});
  }
  const contentBlocks = Array.from(readingGroups.values()).filter(g => g.text.length || g.media.length).map(g => ({
    ...readingIdentity(g.root, 'content'), text: g.text.join('\n'),
    truncated: g.truncated, media: g.media,
    control_tokens: kept.filter(el => g.root.contains(el) && inViewport(el)).map(el => nodes.get(el)).filter(Boolean).slice(0, 40)
  }));
  const scrollContainers = [];
  const scrollingElement = document.scrollingElement;
  for (const el of [scrollingElement, ...(document.body ? odyQueryAll(document.body, '*') : [])]) {
    if (!el || (el !== scrollingElement && !/auto|scroll/.test(getComputedStyle(el).overflowY))) continue;
    if (el.scrollHeight <= el.clientHeight + 1 || (el !== scrollingElement && !readingVisible(el, el.getBoundingClientRect()))) continue;
    if (scrollContainers.length >= 16) { readingComplete = false; break; }
    scrollContainers.push({...readingIdentity(el, 'scroll'),
      text: (el.getAttribute('aria-label') || '').slice(0, 120),
      top: Math.round(el.scrollTop), max_top: Math.max(0, el.scrollHeight - el.clientHeight),
      height: el.clientHeight, document: el === scrollingElement,
      at_start: el.scrollTop <= 1, at_end: el.scrollTop >= el.scrollHeight - el.clientHeight - 1});
  }
""".replace("CONTENT_BLOCK_LIMIT", str(CONTENT_BLOCK_MAX_CHARS))


def project_viewport_content(snapshot: dict) -> dict:
    """Bound whole reading entries, preserving exact refs and honest coverage."""
    import json

    blocks = []
    spent = 0
    complete = bool(snapshot.get("content_complete", False))
    raw_blocks = snapshot.get("content_blocks") or []
    for block in raw_blocks:
        item = {k: block[k] for k in ("ref", "text", "media", "truncated", "control_refs", "frame_index") if k in block}
        cost = len(json.dumps(item, ensure_ascii=False))
        if spent + cost > 12000:
            complete = False
            break
        blocks.append(item)
        spent += cost
    scrollers = [{k: item[k] for k in ("ref", "text", "top", "max_top", "height", "document", "at_start", "at_end", "frame_index") if k in item}
                 for item in (snapshot.get("scroll_containers") or [])[:16]]
    complete &= len(snapshot.get("scroll_containers") or []) <= 16
    return {"content_blocks": blocks, "scroll_containers": scrollers,
            "content_complete": complete, "content_blocks_omitted": len(raw_blocks) - len(blocks)}
