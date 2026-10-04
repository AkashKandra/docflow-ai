"""LangGraph workflow with a human-in-the-loop pause.

    classify -> extract -> validate -> decide --auto--> auto_approve -> END
                   \\                      \\--review--> human_review -> END (paused)
                    \\--unknown type------------------> human_review

The graph "pauses" at human_review by ending the run; the pipeline persists the state in the
document record. When the reviewer responds, the graph is invoked again with a `review` payload and
enters at `apply_review`. No in-memory checkpointer is needed, so it survives function restarts and
scale-out on Azure Functions.
"""
from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .classifier import classify_document
from .config import Settings
from .extraction import Extractor
from .llm import LLM
from .masking import mask_identifier
from .models import DocType, Issue, Status
from .routing import decide, min_required_confidence
from .storage import DocStore
from .validation import rule_checks, validate

# Reviewers may correct these fields; anything else in `corrections` is ignored.
CORRECTABLE = {
    DocType.INVOICE: {
        "vendor_name", "invoice_number", "invoice_date", "due_date", "customer_name",
        "subtotal", "tax", "total", "currency", "po_number",
    },
    DocType.KYC_ID: {
        "first_name", "last_name", "document_number", "date_of_birth", "expiry_date", "country", "address",
    },
}


class State(TypedDict, total=False):
    doc_id: str
    filename: str
    data: bytes
    doc_type: str
    raw_text: str
    values: dict[str, Any]
    confidences: dict[str, float | None]
    issues: list[dict]
    llm_risk: str
    llm_notes: str
    min_confidence: float | None
    reasons: list[str]
    auto_approve: bool
    status: str
    review: dict | None
    reviewer: str | None
    reviewer_comment: str | None


def build_workflow(settings: Settings, extractor: Extractor, llm: LLM, docs: DocStore):
    def classify(state: State) -> dict:
        text = extractor.read_text(state["data"])
        return {"doc_type": classify_document(state["filename"], text, llm).value, "raw_text": text}

    def after_classify(state: State) -> str:
        return "human_review" if state["doc_type"] == DocType.UNKNOWN.value else "extract"

    def extract(state: State) -> dict:
        res = extractor.extract(state["data"], DocType(state["doc_type"]))
        return {"values": res.values(), "confidences": res.confidences()}

    def validate_node(state: State) -> dict:
        dt = DocType(state["doc_type"])
        res = validate(
            dt, state["values"], llm, settings,
            is_duplicate=lambda v, n: docs.has_duplicate(v, n, state["doc_id"]),
        )
        return {
            "values": res.values,
            "issues": [i.model_dump() for i in res.issues],
            "llm_risk": res.llm_risk,
            "llm_notes": res.llm_notes,
        }

    def decide_node(state: State) -> dict:
        dt = DocType(state["doc_type"])
        issues = [Issue(**i) for i in state.get("issues", [])]
        min_conf = min_required_confidence(dt, state.get("confidences", {}))
        ok, reasons = decide(dt, issues, min_conf, state.get("llm_risk", "unknown"), settings.confidence_threshold)
        return {"min_confidence": min_conf, "reasons": reasons, "auto_approve": ok}

    def after_decide(state: State) -> str:
        return "auto_approve" if state["auto_approve"] else "human_review"

    def auto_approve(state: State) -> dict:
        return {"status": Status.AUTO_APPROVED.value}

    def human_review(state: State) -> dict:
        reasons = state.get("reasons") or ["Document type could not be determined"]
        return {"status": Status.NEEDS_REVIEW.value, "reasons": reasons}

    def apply_review(state: State) -> dict:
        review = state["review"] or {}
        dt = DocType(state["doc_type"])
        values = dict(state.get("values", {}))
        for k, v in (review.get("corrections") or {}).items():
            if k in CORRECTABLE.get(dt, set()):
                values[k] = v
        # Re-run the deterministic rules on the corrected values for the audit trail.
        # (Identifiers may be masked in storage, so the duplicate check only runs on invoices.)
        issues = rule_checks(
            dt, values, settings, date.today(),
            is_duplicate=lambda v, n: docs.has_duplicate(v, n, state["doc_id"]),
        )
        approved = review.get("decision") == "approve"
        return {
            "values": values,
            "issues": [i.model_dump() for i in issues],
            "status": (Status.APPROVED if approved else Status.REJECTED).value,
            "reviewer": review.get("reviewer"),
            "reviewer_comment": review.get("comment"),
        }

    g = StateGraph(State)
    g.add_node("classify", classify)
    g.add_node("extract", extract)
    g.add_node("validate", validate_node)
    g.add_node("decide", decide_node)
    g.add_node("auto_approve", auto_approve)
    g.add_node("human_review", human_review)
    g.add_node("apply_review", apply_review)

    g.add_conditional_edges(
        START, lambda s: "apply_review" if s.get("review") else "classify",
        {"apply_review": "apply_review", "classify": "classify"},
    )
    g.add_conditional_edges("classify", after_classify, {"human_review": "human_review", "extract": "extract"})
    g.add_edge("extract", "validate")
    g.add_edge("validate", "decide")
    g.add_conditional_edges("decide", after_decide, {"auto_approve": "auto_approve", "human_review": "human_review"})
    g.add_edge("auto_approve", END)
    g.add_edge("human_review", END)
    g.add_edge("apply_review", END)
    return g.compile()


__all__ = ["CORRECTABLE", "State", "build_workflow", "mask_identifier"]
