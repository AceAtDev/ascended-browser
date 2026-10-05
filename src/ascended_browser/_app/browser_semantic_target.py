"""Semantic target identity and conservative stale-node recovery.

This is the Firefox/Camoufox analogue of the pattern used by Playwright
locators and agent-browser refs: use the exact observed node as the fast path,
then, only after it becomes stale, re-resolve against the current page using
stable semantics. Recovery is score + margin based and intentionally refuses
ambiguous targets.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fold(value: Any) -> str:
    return _norm(value).casefold()


def _first(*values: Any) -> str:
    for value in values:
        text = _norm(value)
        if text:
            return text
    return ""


@dataclass(frozen=True)
class SemanticTarget:
    role: str = ""
    name: str = ""
    label: str = ""
    placeholder: str = ""
    text: str = ""
    tag: str = ""
    type: str = ""
    context: str = ""
    element_id: str = ""
    name_attr: str = ""
    frame_url: str = ""
    frame_name: str = ""
    in_form: bool | None = None

    @classmethod
    def from_element(cls, element: dict[str, Any]) -> "SemanticTarget":
        return cls(
            role=_norm(element.get("role")),
            name=_first(element.get("aria_label"), element.get("label"), element.get("placeholder"), element.get("text")),
            label=_norm(element.get("label")),
            placeholder=_norm(element.get("placeholder")),
            text=_norm(element.get("text")),
            tag=_norm(element.get("tag")).lower(),
            type=_norm(element.get("type")).lower(),
            context=_norm(element.get("context")),
            element_id=_norm(element.get("id")),
            name_attr=_norm(element.get("name")),
            frame_url=_norm(element.get("frame_url")),
            frame_name=_norm(element.get("frame_name")),
            in_form=bool(element.get("in_form")) if "in_form" in element else None,
        )

    @classmethod
    def from_action(
        cls,
        action: dict[str, Any],
        identity: dict[str, Any] | None = None,
    ) -> "SemanticTarget":
        identity = identity or {}
        semantic = action.get("_semantic_target") if isinstance(action.get("_semantic_target"), dict) else {}
        label = _first(
            action.get("label"), semantic.get("label"), identity.get("label"),
            semantic.get("name"), identity.get("aria_label"),
        )
        return cls(
            role=_first(action.get("role"), semantic.get("role"), identity.get("role")),
            name=_first(
                semantic.get("name"), identity.get("aria_label"), label,
                semantic.get("placeholder"), identity.get("placeholder"),
                semantic.get("text"), identity.get("text"),
            ),
            label=label,
            placeholder=_first(semantic.get("placeholder"), identity.get("placeholder")),
            text=_first(semantic.get("text"), identity.get("text")),
            tag=_first(action.get("tag"), semantic.get("tag"), identity.get("tag")).lower(),
            type=_first(action.get("input_type"), action.get("control_type"), semantic.get("type"), identity.get("type")).lower(),
            context=_first(action.get("context"), semantic.get("context"), identity.get("context")),
            element_id=_first(semantic.get("element_id"), identity.get("id")),
            name_attr=_first(semantic.get("name_attr"), identity.get("name")),
            frame_url=_first(semantic.get("frame_url"), identity.get("frame_url")),
            frame_name=_first(semantic.get("frame_name"), identity.get("frame_name")),
            in_form=(
                bool(semantic.get("in_form"))
                if "in_form" in semantic
                else bool(identity.get("in_form")) if "in_form" in identity else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, "")}

    def signature(self) -> str:
        return "|".join(
            _fold(value)
            for value in (self.role, self.name, self.context, self.name_attr, self.tag, self.type)
            if _norm(value)
        )


def semantic_target_for_action(
    action: dict[str, Any],
    *,
    identity: dict[str, Any] | None = None,
) -> SemanticTarget:
    return SemanticTarget.from_action(action, identity)


def hydrate_action_from_snapshot(action: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Attach the observed target semantics without changing the public tool contract."""
    output = dict(action)
    ref = str(output.get("ref") or "")
    matches = [
        element for element in snapshot.get("elements") or []
        if isinstance(element, dict) and str(element.get("ref") or "") == ref
    ]
    if len(matches) != 1:
        return output
    element = matches[0]
    target = SemanticTarget.from_element(element)
    output["_semantic_target"] = target.to_dict()
    if not _norm(output.get("label")):
        label = _first(target.label, target.name, target.placeholder, target.text)
        if label:
            output["label"] = label
    if not _norm(output.get("context")) and target.context:
        output["context"] = target.context
    return output


def _names(element: dict[str, Any]) -> set[str]:
    return {
        _fold(element.get(key))
        for key in ("label", "aria_label", "placeholder", "text")
        if _fold(element.get(key))
    }


def score_target(target: SemanticTarget, element: dict[str, Any]) -> tuple[int, list[str]]:
    """Return confidence score plus evidence labels; negative means incompatible."""
    if not isinstance(element, dict) or element.get("hidden") or element.get("visible") is False:
        return -1, []

    score = 0
    evidence: list[str] = []
    role = _fold(element.get("role"))
    tag = _fold(element.get("tag"))
    typ = _fold(element.get("type"))

    if target.type and typ and _fold(target.type) != typ:
        return -1, []
    # Frame order is transient. A named frame remains an identity constraint;
    # another frame cannot win merely by accumulating a higher name/ID score.
    for field, wanted in (("frame_url", target.frame_url), ("frame_name", target.frame_name)):
        observed = _norm(element.get(field))
        if wanted and observed and wanted != observed:
            return -1, []
    if target.tag and tag and _fold(target.tag) != tag:
        if not (target.role and role and _fold(target.role) == role):
            return -1, []

    if target.role and role == _fold(target.role):
        score += 4
        evidence.append("role")
    if target.tag and tag == _fold(target.tag):
        score += 2
        evidence.append("tag")
    if target.type and typ == _fold(target.type):
        score += 3
        evidence.append("type")

    names = _names(element)
    for evidence_name, value, weight in (
        ("label", target.label, 8),
        ("name", target.name, 7),
        ("placeholder", target.placeholder, 5),
        ("text", target.text, 4),
    ):
        wanted = _fold(value)
        if wanted and wanted in names:
            score += weight
            evidence.append(evidence_name)
            break

    if target.element_id and _fold(element.get("id")) == _fold(target.element_id):
        score += 10
        evidence.append("id")
    if target.name_attr and _fold(element.get("name")) == _fold(target.name_attr):
        score += 9
        evidence.append("name_attr")
    if target.context and _fold(element.get("context")) == _fold(target.context):
        score += 7
        evidence.append("context")
    if target.frame_url and _fold(element.get("frame_url")) == _fold(target.frame_url):
        score += 4
        evidence.append("frame")
    if target.frame_name and _norm(element.get("frame_name")) == target.frame_name:
        score += 4
        evidence.append("frame_name")
    if target.in_form is not None and bool(element.get("in_form")) == target.in_form:
        score += 1
        evidence.append("form")
    return score, evidence


def resolve_semantic_target(
    target: SemanticTarget,
    snapshot: dict[str, Any],
    *,
    min_score: int = 8,
    min_margin: int = 3,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Resolve one current element only when confidence is unique and explicit."""
    ranked: list[tuple[int, dict[str, Any], list[str]]] = []
    for element in snapshot.get("elements") or []:
        if not isinstance(element, dict):
            continue
        score, evidence = score_target(target, element)
        if score >= 0:
            ranked.append((score, element, evidence))
    ranked.sort(key=lambda item: item[0], reverse=True)

    if not ranked:
        return None, {"reason": "no_candidates", "target": target.to_dict()}
    best_score, best, evidence = ranked[0]
    next_score = ranked[1][0] if len(ranked) > 1 else -1
    if best_score < min_score:
        return None, {
            "reason": "low_confidence", "score": best_score,
            "next_score": next_score, "target": target.to_dict(),
        }
    if next_score >= 0 and best_score - next_score < min_margin:
        return None, {
            "reason": "ambiguous", "score": best_score, "next_score": next_score,
            "target": target.to_dict(),
        }
    return best, {
        "reason": "resolved", "score": best_score, "next_score": next_score,
        "evidence": evidence, "target": target.to_dict(),
    }


def recover_action_from_snapshot(
    action: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    target: SemanticTarget,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    element, diagnostic = resolve_semantic_target(target, snapshot)
    if not element:
        return None, diagnostic
    ref = str(element.get("ref") or "")
    if not ref:
        return None, {**diagnostic, "reason": "resolved_without_ref"}
    recovered = {
        **action,
        "ref": ref,
        "observation_id": str(snapshot.get("observation_id") or element.get("observation_id") or ""),
        "label": _first(action.get("label"), element.get("label"), element.get("aria_label"), element.get("placeholder"), element.get("text")),
        "_semantic_target": target.to_dict(),
    }
    return recovered, diagnostic
