"""Query a live page by CSS selector without a model call or a full snapshot.

Ported from browser-use's ``find_elements`` action (``browser_use/tools/
service.py``, MIT): "Zero LLM cost, instant. Returns matching elements with
tag, text, and attributes." Counting rows, listing every product link or
reading one attribute across a list is a single cheap read here, where the
alternative was a full observation or model-written JavaScript.

Two things differ from upstream, both owned by existing Odysseus contracts:

- A match that is also in the tab's current observation carries its ``ref``,
  so the agent can act on a row it found without observing again.
- Secret values never leave the page: ``value`` of a password, one-time-code,
  card or hidden field reads as redacted (the collector's ``sensitive`` rule).
"""
from __future__ import annotations

from typing import Any

from ascended_browser._app.browser_shadow_dom import SHADOW_DOM_JS

MAX_RESULTS_CAP = 200
DEFAULT_MAX_RESULTS = 50
_TEXT_CHARS = 300
_ATTR_CHARS = 500
_URL_IDENTITY_CHARS = 8192
_MAX_ATTRIBUTES = 12

QUERY_SCRIPT = r"""(args) => {
  /*SHADOW_DOM*/
  const {selector, attributes, maxResults, includeText, textChars, attrChars, identityChars, start} = args;
  let nodes;
  try {
    nodes = odyQueryAll(document, selector);
  } catch (e) {
    return {error: 'Invalid CSS selector: ' + e.message, elements: [], total: 0};
  }
  // Same rule as the observation collector: these values never reach the model.
  const sensitive = el => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['password', 'hidden'].includes(type)) return true;
    const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
    return autocomplete.split(/\s+/).some(token => [
      'current-password', 'new-password', 'one-time-code', 'cc-number', 'cc-csc',
      'cc-exp', 'cc-exp-month', 'cc-exp-year',
    ].includes(token));
  };
  const clip = (value, limit) => value.length > limit ? value.slice(0, limit) + '...' : value;
  const tokens = window[Symbol.for('odysseus.browser.nodes')];
  const total = nodes.length;
  const limit = Math.min(Math.max(0, total-start), maxResults);
  const elements = [];
  for (let i = start; i < start+limit; i++) {
    const el = nodes[i];
    const item = {index: i, tag: el.tagName.toLowerCase(), children_count: el.children.length};
    let shown = false;
    try {
      const style = getComputedStyle(el);
      shown = el.getClientRects().length > 0 && style.visibility !== 'hidden';
    } catch (e) {}
    item.visible = shown;
    if (includeText) {
      // A component's inner control carries its words through a <slot>.
      item.text = clip((el.textContent || odySlottedText(el) || '').replace(/\s+/g, ' ').trim(), textChars);
    }
    if (attributes && attributes.length) {
      item.attrs = {};
      for (const name of attributes) {
        let value;
        if (name === 'value' && 'value' in el) {
          // The live value, not the markup default, unless it is a secret.
          value = sensitive(el) || (el.getAttribute('type') || '').toLowerCase() === 'file'
            ? '[redacted]' : String(el.value ?? '');
        } else if ((name === 'src' || name === 'href') && typeof el[name] === 'string' && el[name] !== '') {
          // The resolved property gives an absolute URL.
          value = el[name];
        } else {
          value = el.getAttribute(name);
        }
        if (value !== null && value !== undefined) {
          // URLs identify the destination. A shortened URL is a different,
          // unusable identity. Omit huge identities (e.g. embedded image data)
          // explicitly; presentation budgets trim whole matches later.
          if (['href', 'src'].includes(name) && String(value).length > identityChars) {
            item.identity_omitted = true;
            (item.attrs_omitted ||= []).push(name);
          } else {
            // A field's live value is what was typed: a 500-character preview
            // of a cover letter could not confirm it, so live runs read
            // textareas with browser_evaluate in slices instead.
            item.attrs[name] = ['href', 'src'].includes(name) ? String(value)
              : clip(String(value), name === 'value' ? identityChars : attrChars);
          }
        }
      }
    }
    const token = tokens && tokens.get ? tokens.get(el) : undefined;
    if (token) item.node_token = String(token);
    elements.push(item);
  }
  return {elements, total, showing: limit};
}""".replace("/*SHADOW_DOM*/", SHADOW_DOM_JS)


def normalize_query_args(
    selector: Any, attributes: Any = None, max_results: Any = None, include_text: Any = True, *, cursor: Any = 0,
) -> dict[str, Any]:
    """Validate the model's arguments; raise ValueError with the fix."""
    selector = str(selector or "").strip()
    if not selector:
        raise ValueError("selector is required: a CSS selector such as 'table tr' or 'a.product-link'.")
    if len(selector) > 500:
        raise ValueError("selector is longer than 500 characters; use a shorter CSS selector.")
    if isinstance(attributes, str):
        attributes = [part.strip() for part in attributes.split(",")]
    # HTML attribute names are case-insensitive; normalise before the page
    # sees them so "VALUE" cannot bypass the secret redaction for "value".
    names = [str(name).strip().lower() for name in (attributes or []) if str(name or "").strip()]
    names = list(dict.fromkeys(names))[:_MAX_ATTRIBUTES]
    try:
        limit = int(max_results) if max_results not in (None, "") else DEFAULT_MAX_RESULTS
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_RESULTS
    limit = max(1, min(MAX_RESULTS_CAP, limit))
    try:
        start = int(cursor or 0)
    except (TypeError, ValueError):
        raise ValueError("selector cursor must be a non-negative match offset from next_cursor.") from None
    if start < 0:
        raise ValueError("selector cursor must be a non-negative match offset from next_cursor.")
    return {
        "start": start,
        "selector": selector,
        "attributes": names,
        "maxResults": limit,
        "includeText": include_text is not False,
        "textChars": _TEXT_CHARS,
        "attrChars": _ATTR_CHARS,
        "identityChars": _URL_IDENTITY_CHARS,
    }


def attach_refs(elements: list[dict], candidates: list[dict], *, frame_url: str = "",
                main_frame: bool = True) -> None:
    """Give each match the ref it has in the current observation, if any.

    Identity is the node token the collector stored on the live element, so a
    ref is only attached to the very node that observation saw.
    """
    by_token: dict[str, str] = {}
    for candidate in candidates:
        token = str(candidate.get("node_token") or "")
        if not token:
            continue
        in_main = int(candidate.get("frame_index") or 0) == 0
        if main_frame != in_main:
            continue
        if not main_frame and str(candidate.get("frame_url") or "") != frame_url:
            continue
        by_token[token] = str(candidate.get("ref") or "")
    for element in elements:
        token = str(element.pop("node_token", "") or "")
        ref = by_token.get(token)
        if ref:
            element["ref"] = ref


def format_query_result(data: dict[str, Any], selector: str) -> str:
    """The readable summary the model sees, in upstream's shape."""
    elements = data.get("matches") or data.get("elements") or []
    total = int(data.get("total") or 0)
    showing = int(data.get("showing") or len(elements))
    if total == 0:
        return f'No elements found matching "{selector}".'
    lines = [f'Found {total} element{"s" if total != 1 else ""} matching "{selector}":', ""]
    for element in elements:
        parts = [f"[{element.get('index', 0)}]"]
        if element.get("ref"):
            parts.append(f"ref={element['ref']}")
        parts.append(f"<{element.get('tag', '?')}>")
        text = " ".join(str(element.get("text") or "").split())
        if text:
            parts.append(f'"{text}"')
        attrs = element.get("attrs") or {}
        if attrs:
            parts.append("{" + ", ".join(f'{key}="{value}"' for key, value in attrs.items()) + "}")
        if element.get("attrs_omitted"):
            parts.append("(oversized URL identity omitted: " + ", ".join(element["attrs_omitted"]) + ")")
        if element.get("frame"):
            parts.append(f"(in frame {element['frame']})")
        if element.get("visible") is False:
            parts.append("(hidden)")
        children = int(element.get("children_count") or 0)
        parts.append(f"({children} {'child' if children == 1 else 'children'})")
        lines.append(" ".join(parts))
    if data.get("next_cursor") is not None:
        lines.append(f"\nShowing {showing} of {total} total elements. Continue browser_extract with the same selector and cursor={data['next_cursor']}.")
    return "\n".join(lines)
