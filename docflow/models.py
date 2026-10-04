"""Shared data models."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocType(str, Enum):
    INVOICE = "invoice"
    KYC_ID = "kyc_id"
    UNKNOWN = "unknown"


class Status(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"  # approved by a human reviewer
    REJECTED = "rejected"
    FAILED = "failed"


# Statuses after which no further automatic processing happens.
DONE_STATUSES = {
    Status.AUTO_APPROVED,
    Status.NEEDS_REVIEW,
    Status.APPROVED,
    Status.REJECTED,
    Status.FAILED,
}
# Only these documents are searchable.
INDEXABLE_STATUSES = {Status.AUTO_APPROVED, Status.APPROVED}


class FieldValue(BaseModel):
    value: Any = None
    confidence: float | None = None


class ExtractionResult(BaseModel):
    doc_type: DocType
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    raw_text: str = ""

    def values(self) -> dict[str, Any]:
        return {k: v.value for k, v in self.fields.items()}

    def confidences(self) -> dict[str, float | None]:
        return {k: v.confidence for k, v in self.fields.items()}


class Issue(BaseModel):
    code: str
    severity: str  # "error" | "warning"
    message: str
    field: str | None = None


class SourceInfo(BaseModel):
    """Where the document came from (filled in by the Logic App)."""

    sender: str | None = None
    subject: str | None = None
    message_id: str | None = None


class DocumentRecord(BaseModel):
    doc_id: str
    job_id: str
    filename: str
    blob_name: str
    content_type: str = "application/pdf"
    source: SourceInfo = Field(default_factory=SourceInfo)
    doc_type: DocType = DocType.UNKNOWN
    status: Status = Status.QUEUED
    fields: dict[str, Any] = Field(default_factory=dict)
    confidences: dict[str, float | None] = Field(default_factory=dict)
    min_confidence: float | None = None
    issues: list[Issue] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    llm_risk: str | None = None
    llm_notes: str | None = None
    reviewer: str | None = None
    reviewer_comment: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class JobRecord(BaseModel):
    job_id: str
    doc_ids: list[str]
    created_at: datetime = Field(default_factory=utcnow)


class ParsedQuery(BaseModel):
    """A natural-language search request turned into structured filters."""

    text: str = ""
    doc_type: DocType | None = None
    party_name: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    date_from: str | None = None  # YYYY-MM-DD
    date_to: str | None = None  # YYYY-MM-DD
