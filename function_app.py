"""Azure Functions (Python v2 programming model) entry point.

POST /api/documents         submit one or many documents -> 202 + job id (async)
GET  /api/jobs/{job_id}     poll job progress
GET  /api/documents/{id}    one document (fields, issues, status)
GET  /api/review            documents waiting for a human
POST /api/review/{id}       approve / reject (with optional field corrections)
GET  /api/search?q=...      natural-language search over approved documents
Queue trigger               processes one document per message
"""
from __future__ import annotations

import json
import logging
from typing import Any

import azure.functions as func
from pydantic import BaseModel, Field, ValidationError

from docflow.config import Settings
from docflow.container import get_pipeline
from docflow.pipeline import SubmissionError

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
log = logging.getLogger("docflow")


def _json(body: Any, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body, default=str), status_code=status, mimetype="application/json")


class _Doc(BaseModel):
    filename: str
    content_base64: str
    content_type: str = "application/pdf"
    source: dict = Field(default_factory=dict)


class _Submit(BaseModel):
    documents: list[_Doc] = Field(min_length=1, max_length=50)


class _Review(BaseModel):
    decision: str
    corrections: dict = Field(default_factory=dict)
    reviewer: str | None = None
    comment: str | None = None


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"status": "ok"})


@app.route(route="documents", methods=["POST"])
def submit_documents(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = _Submit.model_validate(req.get_json())
    except (ValueError, ValidationError) as exc:
        return _json({"error": "invalid request", "detail": str(exc)[:500]}, 400)
    try:
        job = get_pipeline().submit([d.model_dump() for d in payload.documents])
    except SubmissionError as exc:
        return _json({"error": str(exc)}, 400)
    return _json({"job_id": job.job_id, "status_url": f"/api/jobs/{job.job_id}", "documents": len(job.doc_ids)}, 202)


@app.route(route="jobs/{job_id}", methods=["GET"])
def job_status(req: func.HttpRequest) -> func.HttpResponse:
    status = get_pipeline().job_status(req.route_params["job_id"])
    return _json(status, 200) if status else _json({"error": "job not found"}, 404)


@app.route(route="documents/{doc_id}", methods=["GET"])
def get_document(req: func.HttpRequest) -> func.HttpResponse:
    rec = get_pipeline().docs.get_doc(req.route_params["doc_id"])
    return _json(json.loads(rec.model_dump_json()), 200) if rec else _json({"error": "not found"}, 404)


@app.route(route="review", methods=["GET"])
def review_queue(req: func.HttpRequest) -> func.HttpResponse:
    docs = get_pipeline().docs.list_docs(status="needs_review", limit=100)
    return _json([
        {"doc_id": d.doc_id, "filename": d.filename, "doc_type": d.doc_type.value,
         "reasons": d.review_reasons, "fields": d.fields, "source": d.source.model_dump()}
        for d in docs
    ])


@app.route(route="review/{doc_id}", methods=["POST"])
def review_document(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = _Review.model_validate(req.get_json())
        rec = get_pipeline().review(
            req.route_params["doc_id"], body.decision, body.corrections, body.reviewer, body.comment
        )
    except (ValueError, ValidationError, SubmissionError) as exc:
        return _json({"error": str(exc)[:500]}, 400)
    except KeyError:
        return _json({"error": "not found"}, 404)
    return _json(json.loads(rec.model_dump_json()))


@app.route(route="search", methods=["GET"])
def search(req: func.HttpRequest) -> func.HttpResponse:
    q = req.params.get("q", "").strip()
    if not q:
        return _json({"error": "q is required"}, 400)
    top = max(1, min(int(req.params.get("top", "5")), 20))
    return _json(get_pipeline().search(q, top))


@app.queue_trigger(arg_name="msg", queue_name="%DOCFLOW_QUEUE_NAME%", connection="AzureWebJobsStorage")
def process_document(msg: func.QueueMessage) -> None:
    doc_id = json.loads(msg.get_body().decode())["doc_id"]
    try:
        get_pipeline().process(doc_id)
    except Exception as exc:
        # Let the runtime retry; on the last attempt record the failure instead of poisoning silently.
        if msg.dequeue_count >= Settings.from_env().max_dequeue_count:
            log.exception("Giving up on %s", doc_id)
            get_pipeline().mark_failed(doc_id, str(exc))
        else:
            log.warning("Attempt %s failed for %s: %s", msg.dequeue_count, doc_id, exc)
            raise
