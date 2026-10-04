"""Confidence-based routing: auto-approve or send to a human reviewer."""
from __future__ import annotations

from .models import DocType, Issue
from .validation import REQUIRED


def min_required_confidence(
    doc_type: DocType, confidences: dict[str, float | None]
) -> float | None:
    scores = [confidences[f] for f in REQUIRED.get(doc_type, []) if confidences.get(f) is not None]
    return min(scores) if scores else None


def decide(
    doc_type: DocType,
    issues: list[Issue],
    min_confidence: float | None,
    llm_risk: str,
    threshold: float,
) -> tuple[bool, list[str]]:
    """Return (auto_approve, reasons). Anything uncertain goes to a human."""
    reasons: list[str] = []
    if doc_type == DocType.UNKNOWN:
        reasons.append("Document type could not be determined")
    reasons += [i.message for i in issues if i.severity == "error"]
    reasons += [i.message for i in issues if i.code == "high_value"]
    if min_confidence is None and doc_type != DocType.UNKNOWN:
        reasons.append("No extraction confidence available")
    elif min_confidence is not None and min_confidence < threshold:
        reasons.append(f"Low extraction confidence ({min_confidence:.2f} < {threshold:.2f})")
    if llm_risk in {"high", "unknown"}:
        reasons.append(f"LLM validation risk: {llm_risk}")
    return (not reasons, reasons)
