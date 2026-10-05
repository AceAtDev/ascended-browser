"""What a page looks like, in the terms needed to rebuild it.

Recreating a site's front end was a guessing game: a screenshot carries no
numbers, ``read=inspect`` describes one element, and the agent fell back to
``browser_evaluate`` loops of ``getComputedStyle``. ``read_design`` asks the
page once and answers with what a designer would copy off it: the CSS custom
properties, colors ranked by how much of the page they paint (named by the
variable that defines them, when one does), the type scale with a sample of
each step, radii, shadows, spacing, the layout regions with their boxes and
the inline SVG icons.

It is read-only (one ``evaluate``; colors are normalized through an
unattached canvas, so nothing is written to the page) and served by
``browser_extract read="design"``, optionally scoped to one component with
``target_ref``. The inline answer is compact; ``save_to`` keeps everything,
including icon markup and font sources.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

#: One in-page read. Called as ``(root, opts)``; ``root`` null means the page.
DESIGN_SCRIPT = r"""(root, opts) => {
  opts = opts || {};
  const scoped = !!root;
  root = root || document.documentElement;
  const maxNodes = opts.max_nodes || 4000;
  const vw = window.innerWidth, vh = window.innerHeight;
  const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const canvas = document.createElement('canvas').getContext('2d');
  const hexCache = new Map();
  // Any CSS color → '#rrggbb' or '#rrggbbaa'; '' for transparent or unparseable.
  const hex = value => {
    value = String(value || '').trim();
    if (!value || value === 'transparent' || value === 'none') return '';
    if (hexCache.has(value)) return hexCache.get(value);
    let out = '';
    try {
      canvas.fillStyle = '#010203';
      canvas.fillStyle = value;
      const got = String(canvas.fillStyle);
      if (got !== '#010203' || /^#0*10*20*3$/i.test(value)) {
        if (got.startsWith('#')) out = got.toLowerCase();
        else {
          const m = got.match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?/);
          if (m) {
            const a = m[4] === undefined ? 1 : parseFloat(m[4]);
            if (a === 0) out = '';
            else {
              const h = n => Math.round(parseFloat(n)).toString(16).padStart(2, '0');
              out = '#' + h(m[1]) + h(m[2]) + h(m[3]) + (a < 1 ? Math.round(a * 255).toString(16).padStart(2, '0') : '');
            }
          }
        }
      }
    } catch (e) { out = ''; }
    hexCache.set(value, out);
    return out;
  };
  const bump = (map, key, by, sample) => {
    if (!key) return;
    const entry = map.get(key) || {n: 0, w: 0};
    entry.n += 1; entry.w += by;
    if (sample && !entry.sample) entry.sample = sample;
    map.set(key, entry);
  };

  // CSS custom properties: declared names from readable stylesheets, plus the
  // ones the computed style of <html> and <body> enumerates (cross-origin
  // sheets cannot be read, but their variables still resolve here).
  const varNames = new Set();
  const fontSources = [];
  const scanRules = (rules, depth) => {
    if (!rules || depth > 4) return;
    for (const rule of rules) {
      try {
        if (rule.style) {
          for (let i = 0; i < rule.style.length; i++) {
            const name = rule.style[i];
            if (name.startsWith('--')) varNames.add(name);
          }
        }
        if (rule.constructor && rule.constructor.name === 'CSSFontFaceRule' || rule.type === 5) {
          const family = clip(rule.style.getPropertyValue('font-family'), 80).replace(/["']/g, '');
          const src = clip(rule.style.getPropertyValue('src'), 400);
          if (fontSources.length < 40) fontSources.push({family, weight: rule.style.getPropertyValue('font-weight') || '', src});
        }
        if (rule.cssRules) scanRules(rule.cssRules, depth + 1);
      } catch (e) {}
    }
  };
  let unreadableSheets = 0;
  for (const sheet of Array.from(document.styleSheets || [])) {
    try { scanRules(sheet.cssRules, 0); } catch (e) { unreadableSheets += 1; }
  }
  for (const el of [document.documentElement, document.body, scoped ? root : null]) {
    if (!el) continue;
    try { const cs = getComputedStyle(el); for (let i = 0; i < cs.length; i++) if (cs[i].startsWith('--')) varNames.add(cs[i]); } catch (e) {}
  }
  const varStyle = getComputedStyle(scoped ? root : document.documentElement);
  const variables = [];
  const colorVar = new Map();  // hex → most readable variable name
  // '--main-surface-primary' says what it is; build-hashed '--x17e9krr' does not.
  const readable = name => /^--[a-z]+(-[a-z0-9]+)*$/i.test(name)
    && !/[0-9][a-z]|[a-z][0-9]{2,}/i.test(name.replace(/-(50|[1-9]00)\b/g, ''))
    && !/^--x[a-z0-9]{6,7}$/.test(name);  // StyleX atomic hashes, letters only
  // A role ('--page-bg') beats a palette step ('--gray-870') when both hold the color.
  const step = name => /-\d+$/.test(name);
  const better = (a, b) => step(a) !== step(b) ? !step(a) : a.length < b.length;
  for (const name of varNames) {
    const value = clip(varStyle.getPropertyValue(name), 160);
    if (!value) continue;
    const asColor = /^(#|rgb|hsl|oklch|oklab|lab|lch|color\()/i.test(value) ? hex(value) : '';
    variables.push(asColor ? {name, value, hex: asColor} : {name, value});
    if (asColor && readable(name)) {
      const prev = colorVar.get(asColor);
      if (!prev || better(name, prev)) colorVar.set(asColor, name);
    }
  }

  const visible = (el, cs) => {
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
    if (parseFloat(cs.opacity) === 0) return false;
    return true;
  };
  const roleOf = el => {
    const tag = el.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag)) return tag;
    const role = el.getAttribute('role') || '';
    if (tag === 'button' || role === 'button' || tag === 'summary') return 'button';
    if (tag === 'a') return 'link';
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable) return 'input';
    if (tag === 'label') return 'label';
    if (tag === 'code' || tag === 'pre' || tag === 'kbd') return 'code';
    return 'body';
  };
  const px = v => { const n = parseFloat(v); return Number.isFinite(n) ? n : 0; };
  const fmtPx = v => (Math.round(px(v) * 100) / 100) + 'px';

  const bg = new Map(), fg = new Map(), border = new Map(), type = new Map();
  const radius = new Map(), shadow = new Map(), spacing = new Map(), families = new Map();
  const icons = [], iconSeen = new Set(), images = [], backgrounds = [], canvases = [];
  const nameOf = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id
    : (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/)[0] : ''));
  // An icon drawn from a sprite (<svg><use href="#id">) is empty markup on
  // its own: inline the referenced <symbol> into a detached copy.
  const resolveSvg = svg => {
    const uses = svg.querySelectorAll('use');
    if (!uses.length) return {markup: svg.outerHTML, sprite: false, external: ''};
    const copy = svg.cloneNode(true);
    let external = '';
    for (const use of Array.from(copy.querySelectorAll('use'))) {
      const ref = use.getAttribute('href') || use.getAttribute('xlink:href') || '';
      if (!ref.startsWith('#')) {
        try { external = new URL(ref, location.href).href; } catch (e) { external = ref; }
        continue;
      }
      const target = document.getElementById(ref.slice(1));
      if (!target) continue;
      const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      for (const child of Array.from(target.childNodes)) group.appendChild(child.cloneNode(true));
      for (const attr of ['transform', 'fill', 'stroke']) if (use.getAttribute(attr)) group.setAttribute(attr, use.getAttribute(attr));
      if (target.getAttribute('viewBox') && !copy.getAttribute('viewBox')) copy.setAttribute('viewBox', target.getAttribute('viewBox'));
      use.replaceWith(group);
    }
    return {markup: copy.outerHTML, sprite: true, external};
  };
  const screenArea = vw * vh;
  let nodes = 0, truncated = false;
  const styles = new WeakMap();
  const styleOf = el => { let cs = styles.get(el); if (!cs) { try { cs = getComputedStyle(el); } catch (e) { cs = null; } styles.set(el, cs); } return cs; };
  // A hidden element rejects its whole subtree; <head>, scripts and styles paint nothing.
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {acceptNode: el => {
    if (/^(HEAD|SCRIPT|STYLE|NOSCRIPT|TEMPLATE|LINK|META)$/.test(el.tagName) || el.ownerSVGElement) return NodeFilter.FILTER_REJECT;
    const cs = styleOf(el);
    return cs && visible(el, cs) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
  }});
  for (let el = root; el; el = walker.nextNode()) {
    if (++nodes > maxNodes) { truncated = true; break; }
    const cs = styleOf(el);
    if (!cs || !visible(el, cs)) { if (el === root) break; continue; }
    const r = el.getBoundingClientRect();
    const area = Math.max(0, r.width) * Math.max(0, r.height);
    if (area <= 0 && el !== root) continue;
    const tag = el.tagName.toLowerCase();

    const b = hex(cs.backgroundColor);
    if (b) bump(bg, b, Math.min(area, screenArea));
    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      if (px(cs['border' + side + 'Width']) > 0 && cs['border' + side + 'Style'] !== 'none') {
        const color = hex(cs['border' + side + 'Color']);
        if (color) bump(border, color + ' ' + fmtPx(cs['border' + side + 'Width']), 1);
        break;
      }
    }
    if (cs.borderTopLeftRadius && px(cs.borderTopLeftRadius) > 0) {
      const rad = [cs.borderTopLeftRadius, cs.borderTopRightRadius, cs.borderBottomRightRadius, cs.borderBottomLeftRadius];
      bump(radius, rad.every(v => v === rad[0]) ? rad[0] : rad.join(' '), 1);
    }
    if (cs.boxShadow && cs.boxShadow !== 'none') bump(shadow, clip(cs.boxShadow, 120), 1);
    for (const prop of ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'rowGap', 'columnGap']) {
      const v = px(cs[prop]);
      if (v > 0) bump(spacing, fmtPx(v), 1);
    }

    // Text set directly in this element (not inherited by a child that sets its own).
    let own = '';
    for (const child of el.childNodes) if (child.nodeType === 3) own += child.nodeValue;
    own = own.replace(/\s+/g, ' ').trim();
    if (own || tag === 'input' || tag === 'textarea') {
      const weight = own.length || 8;
      const color = hex(cs.color);
      if (color) bump(fg, color, weight);
      const family = clip(cs.fontFamily, 120);
      bump(families, family, weight);
      const key = [fmtPx(cs.fontSize), cs.fontWeight, cs.lineHeight === 'normal' ? 'normal' : fmtPx(cs.lineHeight),
                   cs.letterSpacing === 'normal' ? '' : cs.letterSpacing, family].join('|');
      const sample = tag === 'input' || tag === 'textarea'
        ? clip(el.getAttribute('placeholder') || el.getAttribute('aria-label') || '', 50) : clip(own, 50);
      bump(type, key, weight, sample);
      const entry = type.get(key);
      entry.roles = entry.roles || {};
      const role = roleOf(el);
      entry.roles[role] = (entry.roles[role] || 0) + 1;
      if (cs.textTransform && cs.textTransform !== 'none') entry.transform = cs.textTransform;
    }

    if (tag === 'svg' && icons.length < 60) {
      const {markup, sprite, external} = resolveSvg(el);
      const sig = markup || (el.getAttribute('viewBox') || '') + r.width;
      if (!iconSeen.has(sig)) {
        iconSeen.add(sig);
        const owner = el.closest('button,a,[role=button],[aria-label]');
        const icon = {
          label: clip(el.getAttribute('aria-label') || (el.querySelector('title') || {}).textContent
                      || (owner && (owner.getAttribute('aria-label') || owner.textContent)) || '', 50),
          size: Math.round(r.width) + 'x' + Math.round(r.height),
          color: hex(cs.color), markup: markup.length > 8000 ? '' : markup,
        };
        if (sprite) icon.sprite = true;
        if (external) icon.sprite_url = external;
        icons.push(icon);
      }
    }
    if (tag === 'img' && images.length < 30 && el.currentSrc) {
      images.push({alt: clip(el.alt, 60), src: el.currentSrc.slice(0, 600), size: Math.round(r.width) + 'x' + Math.round(r.height)});
    }
    // Painted backgrounds (hero images, gradients) and script-drawn canvases:
    // a flat background color says nothing about either.
    if (area >= 2000 && cs.backgroundImage && cs.backgroundImage !== 'none' && backgrounds.length < 40) {
      const value = clip(cs.backgroundImage, 600);
      if (!backgrounds.some(b => b.value === value)) {
        backgrounds.push({value, on: nameOf(el), size: Math.round(r.width) + 'x' + Math.round(r.height), area,
                          first_screen: r.top + window.scrollY < vh, kind: /url\(/.test(value) ? 'image' : 'gradient'});
      }
    }
    if (tag === 'canvas' && area >= 2000 && canvases.length < 10) {
      const canvas = {on: nameOf(el), size: Math.round(r.width) + 'x' + Math.round(r.height),
                      at: Math.round(r.left + window.scrollX) + ',' + Math.round(r.top + window.scrollY),
                      first_screen: r.top + window.scrollY < vh};
      // Animated heroes usually ship a still image beside the canvas (often
      // hidden while the animation runs): the picture to reuse.
      for (let up = el.parentElement, depth = 0; up && depth < 3 && !canvas.fallback_image; up = up.parentElement, depth++) {
        const img = up.querySelector('img');
        const source = up.querySelector('picture source[srcset]');
        const src = (img && (img.currentSrc || img.src)) || (source && source.getAttribute('srcset').split(/[\s,]+/)[0]) || '';
        if (src) { try { canvas.fallback_image = new URL(src, location.href).href.slice(0, 600); } catch (e) {} }
      }
      canvases.push(canvas);
    }
  }

  // Layout: the page's landmark regions, or the scoped element and its children.
  const describeBox = (el, cs) => {
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const out = {tag, box: {x: Math.round(r.left + window.scrollX), y: Math.round(r.top + window.scrollY), width: Math.round(r.width), height: Math.round(r.height)}};
    const role = el.getAttribute('role'); if (role) out.role = role;
    const label = el.getAttribute('aria-label') || (el.id ? '#' + el.id : '');
    if (label) out.label = clip(label, 50);
    if (cs.display === 'flex' || cs.display === 'inline-flex') {
      out.layout = 'flex ' + (cs.flexDirection || 'row') + (px(cs.columnGap) || px(cs.rowGap) ? ' gap ' + fmtPx(cs.flexDirection.startsWith('column') ? cs.rowGap : cs.columnGap) : '');
    } else if (cs.display === 'grid' || cs.display === 'inline-grid') {
      // '86.5px 86.5px … 86.5px' → 'repeat(14, 86.5px)'
      const cols = String(cs.gridTemplateColumns || '').split(/\s+/).filter(Boolean);
      const compact = [];
      for (let i = 0; i < cols.length;) {
        let j = i; while (j < cols.length && cols[j] === cols[i]) j++;
        compact.push(j - i > 2 ? `repeat(${j - i}, ${cols[i]})` : cols.slice(i, j).join(' '));
        i = j;
      }
      out.layout = 'grid ' + clip(compact.join(' '), 80);
    }
    if (cs.position === 'fixed' || cs.position === 'sticky') out.position = cs.position;
    if (cs.maxWidth && cs.maxWidth !== 'none') out.max_width = cs.maxWidth;
    const b = hex(cs.backgroundColor); if (b) out.background = b;
    if (px(cs.borderTopLeftRadius) > 0) out.radius = cs.borderTopLeftRadius;
    if (cs.boxShadow && cs.boxShadow !== 'none') out.shadow = clip(cs.boxShadow, 80);
    const pad = [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].map(fmtPx);
    if (pad.some(v => v !== '0px')) out.padding = pad.every(v => v === pad[0]) ? pad[0] : pad.join(' ');
    return out;
  };
  const regions = [];
  const seenRegion = new Set();
  const addRegion = el => {
    if (!el || seenRegion.has(el) || regions.length >= 14) return;
    let cs; try { cs = getComputedStyle(el); } catch (e) { return; }
    if (!visible(el, cs)) return;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return;
    seenRegion.add(el);
    regions.push(describeBox(el, cs));
  };
  if (scoped) {
    addRegion(root);
    for (const child of Array.from(root.children)) addRegion(child);
  } else {
    const selector = 'header,nav,main,aside,footer,dialog[open],form,[role=banner],[role=navigation],[role=main],'
      + '[role=complementary],[role=contentinfo],[role=dialog],[role=search],textarea,[contenteditable=true]';
    for (const el of Array.from(document.querySelectorAll(selector))) {
      // Skip a landmark nested in one already listed unless it is a text input
      // (a composer inside <main> is the region people want to copy).
      const tag = el.tagName.toLowerCase();
      const editable = tag === 'textarea' || el.isContentEditable;
      if (!editable && regions.length && Array.from(seenRegion).some(p => p !== el && p.contains(el) && p.tagName.toLowerCase() !== 'main' && p.tagName.toLowerCase() !== 'body')) continue;
      addRegion(editable ? (el.closest('form') || el) : el);
    }
  }

  const rank = (map, n) => Array.from(map.entries()).sort((a, b) => b[1].w - a[1].w || b[1].n - a[1].n).slice(0, n);
  const totalW = map => Array.from(map.values()).reduce((s, e) => s + e.w, 0) || 1;
  const colorRows = (map, n) => { const t = totalW(map); return rank(map, n).map(([value, e]) => {
    const row = {hex: value, share: Math.round(1000 * e.w / t) / 10, count: e.n};
    const v = colorVar.get(value); if (v) row.variable = v; return row; }); };
  const rootStyle = getComputedStyle(document.documentElement);
  const bodyStyle = document.body ? getComputedStyle(document.body) : rootStyle;
  return {
    url: location.href, title: document.title, scoped,
    viewport: {width: vw, height: vh},
    color_scheme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    root_font_size: rootStyle.fontSize,
    page_background: hex(bodyStyle.backgroundColor) || hex(rootStyle.backgroundColor) || '',
    variables: variables.sort((a, b) => readable(b.name) - readable(a.name)).map(v => readable(v.name) ? v : {...v, hashed: true}),
    variable_count: variables.length, unreadable_stylesheets: unreadableSheets,
    colors: {background: colorRows(bg, 10), text: colorRows(fg, 8),
             border: rank(border, 6).map(([v, e]) => { const [h, w] = v.split(' '); const row = {hex: h, width: w, count: e.n}; const n = colorVar.get(h); if (n) row.variable = n; return row; })},
    fonts: {families: rank(families, 5).map(([v, e]) => ({family: v, chars: e.w})), sources: fontSources,
            loaded: Array.from(document.fonts || []).filter(f => f.status === 'loaded').slice(0, 30).map(f => ({family: f.family.replace(/["']/g, ''), weight: f.weight, style: f.style}))},
    type_scale: rank(type, 12).map(([key, e]) => { const [size, weight, line, letter, family] = key.split('|');
      const row = {size, weight, line_height: line, family, chars: e.w, count: e.n, sample: e.sample || '',
                   roles: Object.entries(e.roles || {}).sort((a, b) => b[1] - a[1]).map(([k]) => k)};
      if (letter) row.letter_spacing = letter; if (e.transform) row.transform = e.transform; return row; }),
    radii: rank(radius, 8).map(([v, e]) => ({value: v, count: e.n})),
    shadows: rank(shadow, 5).map(([v, e]) => ({value: v, count: e.n})),
    spacing: rank(spacing, 10).map(([v, e]) => ({value: v, count: e.n})),
    layout: regions,
    icons, icon_count: icons.length, images,
    // The first screen first: that is what a clone is judged on.
    backgrounds: backgrounds.sort((a, b) => (b.first_screen - a.first_screen) || (b.area - a.area)).slice(0, 12).map(({area, ...rest}) => rest),
    canvases: canvases.sort((a, b) => b.first_screen - a.first_screen),
    nodes_read: Math.min(nodes, maxNodes), truncated,
  };
}"""


#: Inline answer budget; save_to keeps the whole read.
TEXT_BUDGET = 3800


def _color(row: dict) -> str:
    text = str(row.get("hex") or "")
    if row.get("variable"):
        text += f" ({row['variable']})"
    return text


def format_design(data: dict[str, Any], *, budget: int = TEXT_BUDGET) -> str:
    """The read as a compact brief. Over budget, whole low-value sections go
    first (variables, spacing, shadows, ...) so the layout and icons survive."""
    vp = data.get("viewport") or {}
    sections: dict[str, list[str]] = {}
    sections["head"] = [
        f"Design of {data.get('url') or 'the page'}"
        + (" (scoped to the target element)" if data.get("scoped") else "")
        + f": viewport {vp.get('width')}x{vp.get('height')}, {data.get('color_scheme')} scheme, "
        f"root font {data.get('root_font_size')}, page background {data.get('page_background') or 'transparent'}"
    ]
    colors = data.get("colors") or {}
    sections["colors"] = []
    for kind in ("background", "text"):
        rows = [r for r in colors.get(kind) or [] if (r.get("share") or 0) >= 0.5][:6]
        if rows:
            sections["colors"].append(f"{kind} colors: " + ", ".join(f"{_color(r)} {r.get('share')}%" for r in rows))
    sections["backgrounds"] = []
    for bg in (data.get("backgrounds") or [])[:3]:
        value = re.sub(r'url\("?([^")]+)"?\)', r"url(\1)", str(bg.get("value") or ""))
        if len(value) > 220:
            value = value[:219] + "…"
        sections["backgrounds"].append(f"background {bg.get('kind')}: {value} on {bg.get('on')} {bg.get('size')}")
    for canvas in (data.get("canvases") or [])[:2]:
        sections["backgrounds"].append(
            f"canvas {canvas.get('on')} {canvas.get('size')} at {canvas.get('at')} (script-drawn, colors not readable)"
            + (f"; still image beside it: {canvas['fallback_image']}" if canvas.get("fallback_image") else ""))
    if colors.get("border"):
        sections["borders"] = ["borders: " + ", ".join(
            f"{r.get('width')} {_color(r)} ×{r.get('count')}" for r in colors["border"][:4])]
    families = (data.get("fonts") or {}).get("families") or []
    if families:
        sections["fonts"] = ["fonts: " + " | ".join(str(f.get("family")) for f in families[:3])]
    scale = data.get("type_scale") or []
    if scale:
        main_family = str(families[0].get("family")) if families else ""
        rows = []
        for row in scale[:10]:
            extras = []
            if row.get("letter_spacing"):
                extras.append(f"letter-spacing {row['letter_spacing']}")
            if row.get("transform"):
                extras.append(row["transform"])
            if row.get("family") and row.get("family") != main_family:
                extras.append(str(row["family"]).split(",")[0])
            rows.append(
                f"  {row.get('size')}/{row.get('weight')}/{row.get('line_height')} "
                f"{','.join(row.get('roles') or [])} ×{row.get('count')}"
                + (f" [{'; '.join(extras)}]" if extras else "")
                + (f" {row.get('sample')!r}" if row.get("sample") else "")
            )
        sections["type"] = ["type scale (size/weight/line-height, roles, sample):", *rows[:6]]
        sections["type_more"] = rows[6:]
    if data.get("radii"):
        sections["radii"] = ["radii: " + ", ".join(f"{r['value']} ×{r['count']}" for r in data["radii"][:6])]
    if data.get("shadows"):
        sections["shadows"] = ["shadows: " + "; ".join(f"{r['value']} ×{r['count']}" for r in data["shadows"][:3])]
    if data.get("spacing"):
        sections["spacing"] = ["spacing: " + ", ".join(f"{r['value']} ×{r['count']}" for r in data["spacing"][:8])]
    regions = data.get("layout") or []
    if regions:
        sections["layout"] = ["layout:"]
        # Identical repeated regions (a row of cards) read as one line.
        merged: list[tuple[dict, int]] = []
        for region in regions:
            box = region.get("box") or {}
            if merged:
                last, n = merged[-1]
                lbox = last.get("box") or {}
                if (last.get("tag"), lbox.get("width"), lbox.get("height"), lbox.get("y")) == (
                        region.get("tag"), box.get("width"), box.get("height"), box.get("y")):
                    merged[-1] = (last, n + 1)
                    continue
            merged.append((region, 1))
        for region, repeats in merged:
            box = region.get("box") or {}
            name = region.get("tag", "")
            if region.get("role"):
                name += f"[{region['role']}]"
            if region.get("label"):
                name += f" {region['label']!r}"
            detail = [f"{box.get('width')}x{box.get('height')} at {box.get('x')},{box.get('y')}"]
            for key in ("layout", "position", "max_width", "background", "radius", "padding", "shadow"):
                if region.get(key):
                    detail.append(f"{key.replace('_', '-')} {region[key]}" if key not in {"layout", "position"} else str(region[key]))
            sections["layout"].append(f"  {name}" + (f" ×{repeats} in a row" if repeats > 1 else "") + ": " + ", ".join(detail))
    icons = data.get("icons") or []
    if icons:
        seen: dict[tuple[str, str], int] = {}
        for icon in icons:
            if icon.get("label"):
                key = (str(icon["label"]), str(icon.get("size")))
                seen[key] = seen.get(key, 0) + 1
        listed = list(seen.items())[:10]
        shown = ", ".join(f"{label!r} {size}" + (f" ×{n}" if n > 1 else "") for (label, size), n in listed)
        rest = len(icons) - sum(n for _key, n in listed)
        sprites = sum(1 for i in icons if i.get("sprite"))
        sections["icons"] = [
            f"icons ({len(icons)} inline SVG" + (f", {sprites} from a <use> sprite, resolved" if sprites else "") + "): "
            + (shown or "none labelled") + (f" and {rest} more" if rest > 0 else "")
            + "; save_to writes each one's markup"
        ]
    variables = data.get("variables") or []
    if variables:
        named = [v for v in variables if not v.get("hashed")]
        colored = [v for v in named if v.get("hex")]
        other = [v for v in named if not v.get("hex")]
        hashed = len(variables) - len(named)
        sections["variables"] = [
            f"CSS variables: {len(variables)}"
            + (f" ({hashed} with build-generated names, not listed)" if hashed else "")
            + (": " + ", ".join(f"{v['name']} {v['hex']}" for v in colored[:16]) if colored else "")
        ]
        if other:
            sections["variables"].append("  others: " + ", ".join(f"{v['name']}={v['value']}" for v in other[:10]))
    notes = []
    tail = []
    if data.get("images"):
        tail.append(f"{len(data['images'])} images")
    if (data.get("fonts") or {}).get("sources"):
        tail.append(f"{len(data['fonts']['sources'])} @font-face sources")
    if tail:
        notes.append(", ".join(tail) + " (save_to writes their URLs).")
    if data.get("truncated"):
        notes.append(f"Read the first {data.get('nodes_read')} elements only; pass target_ref to read one component in full.")
    if data.get("unreadable_stylesheets"):
        notes.append(f"{data['unreadable_stylesheets']} cross-origin stylesheets could not be listed; variables they define still appear if <html> or <body> resolves them.")
    sections["notes"] = notes

    order = ["head", "colors", "backgrounds", "borders", "fonts", "type", "type_more", "radii", "shadows",
             "spacing", "layout", "icons", "variables", "notes"]
    dropped: list[str] = []
    for victim in ("variables", "notes", "spacing", "shadows", "type_more", "borders", "radii", "icons"):
        # Room for the "left out" note itself.
        if len("\n".join(line for name in order for line in sections.get(name, []))) <= budget - 160:
            break
        if sections.get(victim):
            sections[victim] = []
            dropped.append(victim.replace("_more", " (rows past 6)"))
    lines = [line for name in order for line in sections.get(name, [])]
    if dropped:
        lines.append(f"(left out to fit: {', '.join(dropped)}; save_to keeps the full read)")
    text = "\n".join(lines)
    if len(text) > budget:
        text = text[: budget - 80].rsplit("\n", 1)[0] + "\n… (cut to fit; save_to keeps the full read)"
    return text


#: Region roles that name an ARIA role Playwright can query by name.
_REGION_TAGS = {
    "form": "form", "navigation": "nav", "banner": "header", "contentinfo": "footer",
    "main": "main", "complementary": "aside", "dialog": "dialog,[role=dialog]",
    "region": "section", "section": "section", "search": "[role=search],search",
}


async def locate_region(page: Any, entry: dict[str, Any]) -> Any | None:
    """The live element an observation's region entry ({role, label, id}) names."""
    dom_id = str(entry.get("id") or "")
    role = str(entry.get("role") or "region")
    label = str(entry.get("label") or "").strip()
    candidates = []
    if dom_id:
        candidates.append(page.locator(f'[id="{dom_id.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"]'))
    if label:
        aria_role = "region" if role == "section" else role
        try:
            candidates.append(page.get_by_role(aria_role, name=label, exact=True))
        except Exception:  # noqa: BLE001 - not an ARIA role Playwright knows
            pass
        tags = _REGION_TAGS.get(role)
        # A label taken from the region's own text can hold live numbers (a
        # ticking counter): match its words in order, not its exact text.
        words = re.findall(r"[^\W\d_]{3,}", label)[:6]
        if tags and words:
            pattern = re.compile(r"[\s\S]*?".join(re.escape(w) for w in words), re.I)
            # The innermost match: an enclosing section contains the same words.
            candidates.append((page.locator(tags).filter(has_text=pattern), "last"))
    for item in candidates:
        locator, pick = item if isinstance(item, tuple) else (item, "first")
        try:
            if await asyncio.wait_for(locator.count(), timeout=3.0):
                return locator.last if pick == "last" else locator.first
        except Exception:  # noqa: BLE001 - try the next way of finding it
            continue
    return None


async def read_design(target: Any, *, scoped: bool, timeout: float = 8.0) -> dict[str, Any]:
    """Run the read on a page (``scoped=False``) or one element locator."""
    if scoped:
        call = target.evaluate(DESIGN_SCRIPT, {}, timeout=int(timeout * 1000))
    else:
        call = target.evaluate(f"(opts) => ({DESIGN_SCRIPT})(null, opts)", {})
    data = await asyncio.wait_for(call, timeout=timeout + 1.0)
    if not isinstance(data, dict):
        raise RuntimeError("The page returned no design read.")
    return data
