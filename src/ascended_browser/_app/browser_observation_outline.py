"""The model-facing outline of a browser observation.

A browser observation is a structured projection (``_project_snapshot``), and
that dict remains the contract for every internal consumer: the delta module,
the compactor's retirement and bounding, the evidence archive, the UI, and the
tests. This module only decides what the *model reads* for it, and it is the
single owner of that text for both the fresh tool result
(``tool_execution``) and earlier observations re-rendered in later turns
(``context_compactor``). Nothing may parse its output back.

The shape follows what the major browser agents converged on: one indented
line per element, ``[ref] role "name" key=value [flag]``, shared context
(regions, frames) stated once as a header, on-screen elements first, and an
explicit statement of what was left out and how to reach it. See
``browser-observation-system-design.md``.
"""
from __future__ import annotations

import json
from typing import Any, Iterable
from urllib.parse import urlparse

OBSERVE_FORMAT_SETTING = "browser_workspace_observe_format"
GROUP_CONTEXT_SETTING = "browser_workspace_observe_group_context"
OUTLINE_BUDGET_SETTING = "browser_workspace_observe_outline_max_chars"
OUTLINE_BUDGET_DEFAULT = 13_000
_INDENT = "  "
_CHECKABLE = frozenset({"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"})
_IMPLIED_TYPES = frozenset({"", "text", "button", "submit"})
_NAME_KEYS = ("label", "aria_label", "name", "text", "placeholder")
#: Keys the outline renders from a page projection. Everything else in a result
#: is an outcome (success, error, receipt, fields, delta, ...) and is carried
#: as compact JSON ahead of the page, so receipts keep their existing contract.
_PAGE_OUTLINE_KEYS = frozenset({
    "url", "title", "observation_id", "element_count", "elements_omitted",
    "elements_complete", "relevant_elements", "elements", "regions", "frames",
    "coverage", "omitted_examples", "next_cursor", "view", "headings", "note",
    "content_blocks", "scroll_containers", "content_complete", "content_note",
    "text", "relevant_text", "coverage_complete", "projection_truncated",
})


def observe_format() -> str:
    """``outline`` or ``json``; anything unrecognised is the safe default."""
    try:
        from ascended_browser.runtime.settings import get_setting
        value = str(get_setting(OBSERVE_FORMAT_SETTING, "json") or "json").strip().casefold()
    except Exception:
        value = "json"
    return "outline" if value == "outline" else "json"


def group_context_enabled() -> bool:
    try:
        from ascended_browser.runtime.settings import get_setting
        return bool(get_setting(GROUP_CONTEXT_SETTING, True))
    except Exception:
        return True


def outline_budget() -> int:
    try:
        from ascended_browser.runtime.settings import get_setting
        return int(get_setting(OUTLINE_BUDGET_SETTING, OUTLINE_BUDGET_DEFAULT) or OUTLINE_BUDGET_DEFAULT)
    except Exception:
        return OUTLINE_BUDGET_DEFAULT


def _q(value: Any) -> str:
    return json.dumps(" ".join(str(value).split()), ensure_ascii=False)


def _display_name(element: dict) -> tuple[str, str]:
    for key in _NAME_KEYS:
        value = " ".join(str(element.get(key) or "").split())
        if value:
            return key, value
    return "", ""


def _href(href: Any, page_url: Any) -> str:
    # A shortened path or stripped query/fragment is a different destination.
    # The projection budgets whole elements and omits oversized identities;
    # the renderer must not turn a complete href into a plausible prefix.
    return str(href or "").strip()


def element_line(element: dict, *, page_url: Any = "") -> str:
    """One element: ``[ref] role "name" key=value … [flag] …``.

    Identity-bearing values (ref, name) are never shortened; the projection
    already bounded prose, and an element that cannot fit is omitted whole.
    """
    role = str(element.get("role") or "")
    # browser-use marks elements that appeared since the last step with `*`;
    # here "new" leads the line so it is the first thing read.
    parts = (["new"] if element.get("new") is True else []) + [
        f"[{element.get('ref', '?')}]", role or f"<{element.get('tag') or 'element'}>",
    ]
    if role and element.get("tag"):
        parts.append(f"<{element['tag']}>")
    used, name = _display_name(element)
    if name:
        parts.append(_q(name))
    for key in ("text", "placeholder", "name"):
        value = " ".join(str(element.get(key) or "").split())
        if value and key != used and value != name:
            parts.append(f"{key}={_q(value)}")
    kind = str(element.get("type") or "").casefold()
    if kind not in _IMPLIED_TYPES:
        parts.append(f"type={kind}")
    # A multi-select's value is only its first choice; selected= lists them all.
    if element.get("value") not in (None, "") and not element.get("selected_options"):
        parts.append(f"value={_q(element['value'])}")
    if element.get("selected_text"):
        parts.append(f"selected={_q(element['selected_text'])}")
    options = element.get("options")
    if isinstance(options, list) and options:
        labels = [str(o.get("text") if isinstance(o, dict) else o) for o in options[:8]]
        more = len(options) - len(labels)
        parts.append("options=" + json.dumps(labels, ensure_ascii=False) + (f"+{more}" if more > 0 else ""))
    controls = element.get("controls")
    if isinstance(controls, list) and controls:
        parts.append("controls=" + ",".join(str(c) for c in controls))
    elif element.get("aria_controls"):
        parts.append(f"controls={element['aria_controls']}")
    for key, label in (("aria_activedescendant", "active"), ("aria_autocomplete", "autocomplete")):
        if element.get(key):
            parts.append(f"{label}={element[key]}")
    href = _href(element.get("href"), page_url)
    if href:
        parts.append(f"→ {href}")
    if element.get("nearby"):
        parts.append(f"near={_q(element['nearby'])}")
    if element.get("context"):
        parts.append(f"in={_q(element['context'])}")
    if role.casefold() in _CHECKABLE and element.get("checked") is False:
        parts.append("[unchecked]")
    for flag in ("checked", "disabled", "required", "hidden", "invalid", "identity_omitted"):
        if element.get(flag) is True:
            parts.append(f"[{flag}]")
    if element.get("invalid") is True and element.get("error"):
        parts.append(f"error={_q(element['error'])}")
    if str(element.get("aria_checked") or "").casefold() == "mixed":
        parts.append("[mixed]")
    expanded = str(element.get("aria_expanded") or "").casefold()
    if expanded in ("true", "false"):
        parts.append("[expanded]" if expanded == "true" else "[collapsed]")
    return " ".join(parts)


def region_header(rid: str, region: dict | None, *, first: bool) -> str:
    if not first or not isinstance(region, dict):
        return f"region {rid}"
    label = str(region.get("label") or region.get("text") or "")[:80]
    return f"region {rid} {region.get('role') or 'region'}" + (f" {_q(label)}" if label else "")


def frame_header(fid: str, frame: dict | None, *, first: bool) -> str:
    if not first or not isinstance(frame, dict):
        return f"frame {fid}"
    where = str(frame.get("url") or frame.get("name") or "")[:80]
    return f"frame {fid}" + (f" {_q(where)}" if where else "")


class OutlineLayout:
    """Lays elements out exactly as ``page_lines`` renders them.

    ``_project_snapshot`` asks it what the next element would cost before
    placing it, so the budget is spent on the text the model will actually
    read — including the frame and region headers the element pulls in.
    """

    def __init__(self, regions: dict | None, frames: dict | None, *, page_url: Any = ""):
        self.regions = regions or {}
        self.frames = frames or {}
        self.page_url = page_url
        self.section: bool | None = None
        self.frame: str | None = None
        self.region: str | None = None
        self.defined: set[str] = set()

    def _lines_for(self, element: dict) -> list[str]:
        lines: list[str] = []
        on_screen = element.get("on_screen") is True
        section_changes = self.section is None or on_screen != self.section
        if section_changes:
            lines.append("on screen" if on_screen else "off screen")
        frame, region = element.get("frame"), element.get("region")
        frame_changes = section_changes or frame != self.frame
        if frame and frame_changes:
            lines.append(_INDENT + frame_header(frame, self.frames.get(frame), first=frame not in self.defined))
        depth = 2 if frame else 1
        if region and (frame_changes or region != self.region):
            lines.append(_INDENT * depth + region_header(region, self.regions.get(region), first=region not in self.defined))
        indent = depth + (1 if region else 0)
        lines.append(_INDENT * indent + element_line(element, page_url=self.page_url))
        return lines

    def cost(self, element: dict) -> int:
        return sum(len(line) + 1 for line in self._lines_for(element))

    def place(self, element: dict) -> list[str]:
        lines = self._lines_for(element)
        self.section = element.get("on_screen") is True
        self.frame = element.get("frame")
        self.region = element.get("region")
        for key in (self.frame, self.region):
            if key:
                self.defined.add(key)
        return lines


def _page_of(projection: dict) -> dict | None:
    page = projection.get("page")
    if isinstance(page, dict):
        return page
    if any(key in projection for key in ("relevant_elements", "elements", "observation_id")):
        return projection
    return None


def page_lines(page: dict) -> list[str]:
    """The page section: header, controls by section, then what was left out."""
    elements = page.get("relevant_elements")
    if not isinstance(elements, list):
        elements = page.get("elements") if isinstance(page.get("elements"), list) else []
    elements = [e for e in elements if isinstance(e, dict)]
    lines: list[str] = []
    title, url = page.get("title"), page.get("url")
    if title or url:
        head = "page" + (f" {_q(title)}" if title else "")
        if url:
            parsed = urlparse(str(url))
            head += f" · {(parsed.netloc + parsed.path) if parsed.netloc else url}"
        lines.append(head)
    coverage = page.get("coverage") if isinstance(page.get("coverage"), dict) else {}
    total = coverage.get("total", page.get("element_count"))
    facts = []
    if page.get("observation_id"):
        facts.append(f"observation {page['observation_id']}")
    if total is not None:
        facts.append(f"{total} elements")
    if coverage.get("on_screen") is not None:
        on_screen = coverage["on_screen"]
        shown_on_screen = coverage.get("on_screen_shown", on_screen)
        facts.append(f"{on_screen} on screen" + (f" ({shown_on_screen} shown)" if shown_on_screen != on_screen else ""))
    facts.append(f"{len(elements)} shown")
    lines.append(" · ".join(facts))
    view = page.get("view") if isinstance(page.get("view"), dict) else {}
    if view:
        lines.append("view " + " · ".join(f"{k}={_q(v)}" for k, v in view.items() if v not in (None, "")))
    headings = page.get("headings")
    if isinstance(headings, list) and headings:
        lines.append("headings")
        for heading in headings:
            if isinstance(heading, dict):
                lines.append(_INDENT * max(1, int(heading.get("level") or 1)) + f"h{heading.get('level') or '?'} {_q(heading.get('text') or '')}")
    for scroller in page.get("scroll_containers") or []:
        lines.append(f"[{scroller['ref']}] scroll container top={scroller['top']} max={scroller['max_top']}" +
                     (" [at_end]" if scroller.get("at_end") else ""))
    for block in page.get("content_blocks") or []:
        lines.append(f"[{block['ref']}] visible content" + (" [truncated; extract target_ref]" if block.get("truncated") else ""))
        lines.append(str(block.get("text") or ""))
        if block.get("media"):
            lines.append("  media " + json.dumps(block["media"], ensure_ascii=False))
        if block.get("control_refs"):
            lines.append("  controls " + " ".join(block["control_refs"]))
    if page.get("content_note"):
        lines.append(str(page["content_note"]))
    layout = OutlineLayout(page.get("regions"), page.get("frames"), page_url=url)
    for element in elements:
        lines.extend(layout.place(element))
    omitted = int(page.get("elements_omitted") or 0)
    if omitted:
        examples = [str(x) for x in page.get("omitted_examples") or [] if str(x).strip()][:5]
        line = f"not shown: {omitted}"
        if examples:
            line += " — e.g. " + ", ".join(_q(x) for x in examples)
        lines.append(line)
        reach = ['{query: "…"}']
        # Suggest a region the model can actually scope into: one with shown
        # members, not a closed popup known only through its owning control.
        member_region = next((e.get("region") for e in elements if e.get("region")), None)
        if member_region:
            reach.append(f'{{within: "{member_region}"}}')
        if page.get("next_cursor"):
            reach.append(f'{{cursor: "{page["next_cursor"]}"}}')
        lines.append(_INDENT + "reach them: browser_observe " + " · ".join(reach))
    if page.get("note"):
        lines.append(f"note: {page['note']}")
    text = page.get("text") or page.get("relevant_text")
    if text:
        lines.append("page text:")
        lines.append(str(text))
    return lines


def render_page(page: dict) -> str:
    return "\n".join(page_lines(page))


def render_projection(projection: dict) -> str:
    """A whole browser result: outcome first as compact JSON, then the page.

    Outcomes (success, error, receipts, field results, deltas) keep the JSON
    contract they already have; only the page becomes an outline.
    """
    if not isinstance(projection, dict):
        return json.dumps(projection, ensure_ascii=False)
    page = _page_of(projection)
    outcome = {k: v for k, v in projection.items() if k != "page"}
    if page is projection:
        outcome = {k: v for k, v in projection.items() if k not in _PAGE_OUTLINE_KEYS}
    parts = []
    if outcome:
        parts.append("```json\n" + json.dumps(outcome, ensure_ascii=False, separators=(", ", ": ")) + "\n```")
    if page is not None:
        parts.append("```text\n" + render_page(page) + "\n```")
    return "\n".join(parts)


def iter_region_ids(elements: Iterable[dict]) -> list[str]:
    seen: list[str] = []
    for element in elements:
        rid = element.get("region") if isinstance(element, dict) else None
        if rid and rid not in seen:
            seen.append(rid)
    return seen
