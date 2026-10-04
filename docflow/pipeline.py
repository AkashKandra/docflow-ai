"""Orchestration: submit -> queue -> process -> (review) -> index."""
from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

from .config import Settings
from .masking import redact_for_storage
from .models import (
    DONE_STATUSES,
    INDEXABLE_STATUSES,
    DocType,
    DocumentRecord,
    Issue,
    JobRecord,
    SourceInfo,
    Status,
    utcnow,
)
from .search_index import SearchIndex, parse_query
from .storage import BlobStore, DocStore, Queue
from .workflow import build_workflow

log = logging.getLogger(__name__)


class SubmissionError(ValueError):
    pass


class Pipeline:
    def __init__(self, settings: Settings, blobs: BlobStore, docs: DocStore, queue: Queue,
                 extractor, llm, index: SearchIndex):
        self.s, self.blobs, self.docs, self.queue, self.llm, self.index = settings, blobs, docs, queue, llm, index
        self.graph = build_workflow(settings, extractor, llm, docs)

    # ---------------------------------------------------------------- submit
    def submit(self, items: list[dict[str, Any]], enqueue: bool = True) -> JobRecord:
        """items: [{filename, content_base64, content_type, source?}] -> job (async processing).

        enqueue=False stores everything but leaves processing to the caller (used by scripts/evaluate.py).
        """
        if not items:
            raise SubmissionError("No documents supplied")
        job_id = uuid.uuid4().hex[:12]
        records: list[DocumentRecord] = []
        for item in items:
            ctype = item.get("content_type", "application/pdf")
            if ctype not in self.s.allowed_content_types:
                raise SubmissionError(f"Unsupported content type: {ctype}")
            try:
                data = base64.b64decode(item["content_base64"], validate=True)
            except Exception as exc:
                raise SubmissionError(f"Invalid base64 for {item.get('filename')}") from exc
            if len(data) > self.s.max_attachment_mb * 1024 * 1024:
                raise SubmissionError(f"{item.get('filename')} exceeds {self.s.max_attachment_mb} MB")
            doc_id = uuid.uuid4().hex[:12]
            blob_name = f"{job_id}/{doc_id}"
            self.blobs.put(blob_name, data, ctype)
            records.append(DocumentRecord(
                doc_id=doc_id, job_id=job_id, filename=item.get("filename", doc_id),
                blob_name=blob_name, content_type=ctype,
                source=SourceInfo(**(item.get("source") or {})),
            ))
        job = JobRecord(job_id=job_id, doc_ids=[r.doc_id for r in records])
        self.docs.save_job(job)
        for r in records:
            self.docs.save_doc(r)
        if enqueue:
            for r in records:  # enqueue only after everything is persisted
                self.queue.send({"doc_id": r.doc_id})
        return job

    # --------------------------------------------------------------- process
    def process(self, doc_id: str) -> DocumentRecord:
        rec = self.docs.get_doc(doc_id)
        if rec is None:
            raise KeyError(doc_id)
        if rec.status in DONE_STATUSES:
            return rec  # idempotent: queue messages can be delivered more than once
        rec.status = Status.PROCESSING
        self._save(rec)
        state = self.graph.invoke({
            "doc_id": rec.doc_id, "filename": rec.filename, "data": self.blobs.get(rec.blob_name),
        })
        self._apply_state(rec, state)
        self._save(rec)
        self._index_if_approved(rec)
        return rec

    def mark_failed(self, doc_id: str, error: str) -> None:
        rec = self.docs.get_doc(doc_id)
        if rec:
            rec.status, rec.error = Status.FAILED, error[:500]
            self._save(rec)

    # ---------------------------------------------------------------- review
    def review(self, doc_id: str, decision: str, corrections: dict | None = None,
               reviewer: str | None = None, comment: str | None = None) -> DocumentRecord:
        if decision not in {"approve", "reject"}:
            raise SubmissionError("decision must be 'approve' or 'reject'")
        rec = self.docs.get_doc(doc_id)
        if rec is None:
            raise KeyError(doc_id)
        if rec.status != Status.NEEDS_REVIEW:
            raise SubmissionError(f"Document is {rec.status.value}, not awaiting review")
        state = self.graph.invoke({
            "doc_id": rec.doc_id, "filename": rec.filename,
            "doc_type": rec.doc_type.value, "values": rec.fields, "confidences": rec.confidences,
            "review": {"decision": decision, "corrections": corrections or {},
                       "reviewer": reviewer, "comment": comment},
        })
        self._apply_state(rec, state)
        self._save(rec)
        self._index_if_approved(rec)
        return rec

    # ---------------------------------------------------------------- query
    def job_status(self, job_id: str) -> dict | None:
        job = self.docs.get_job(job_id)
        if job is None:
            return None
        docs = self.docs.list_docs(job_id=job_id, limit=len(job.doc_ids) + 1)
        counts: dict[str, int] = {}
        for d in docs:
            counts[d.status.value] = counts.get(d.status.value, 0) + 1
        pending = counts.get("queued", 0) + counts.get("processing", 0)
        return {
            "job_id": job_id,
            "status": "running" if pending else "completed",
            "total": len(job.doc_ids),
            "counts": counts,
            "documents": [{"doc_id": d.doc_id, "filename": d.filename, "status": d.status.value} for d in docs],
        }

    def search(self, query: str, top: int = 5) -> dict:
        parsed = parse_query(self.llm, query)
        return {"query": parsed.model_dump(exclude_none=True), "results": self.index.search(parsed, top)}

    # --------------------------------------------------------------- helpers
    def _save(self, rec: DocumentRecord) -> None:
        rec.updated_at = utcnow()
        self.docs.save_doc(rec)

    def _apply_state(self, rec: DocumentRecord, state: dict) -> None:
        rec.doc_type = DocType(state.get("doc_type", rec.doc_type.value))
        if "values" in state:
            rec.fields = redact_for_storage(rec.doc_type, state["values"])  # raw IDs never persisted
        rec.confidences = state.get("confidences", rec.confidences)
        rec.min_confidence = state.get("min_confidence", rec.min_confidence)
        rec.issues = [Issue(**i) for i in state.get("issues", [])] or rec.issues
        rec.llm_risk = state.get("llm_risk", rec.llm_risk)
        rec.llm_notes = state.get("llm_notes", rec.llm_notes)
        rec.review_reasons = state.get("reasons", rec.review_reasons) if state["status"] == Status.NEEDS_REVIEW.value else []
        rec.status = Status(state["status"])
        rec.reviewer = state.get("reviewer", rec.reviewer)
        rec.reviewer_comment = state.get("reviewer_comment", rec.reviewer_comment)

    def _index_if_approved(self, rec: DocumentRecord) -> None:
        if rec.status not in INDEXABLE_STATUSES:
            return
        try:
            self.index.upsert(rec)
        except Exception as exc:
            log.exception("Indexing failed for %s", rec.doc_id)
            rec.issues.append(Issue(code="index_failed", severity="warning", message=f"Indexing failed: {exc}"[:300]))
            self._save(rec)
