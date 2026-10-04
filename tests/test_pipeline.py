import pytest
from conftest import invoice_text, item, kyc_text

from docflow.models import Status
from docflow.pipeline import SubmissionError


def run(pipe, text, name="doc.txt"):
    job = pipe.submit([item(text, name)])  # mock queue processes inline
    return pipe.docs.get_doc(job.doc_ids[0])


def test_clean_invoice_is_auto_approved_and_indexed(pipe):
    rec = run(pipe, invoice_text())
    assert rec.status == Status.AUTO_APPROVED
    assert rec.fields["total"] == 1050.0
    assert pipe.search("Acme")["results"][0]["reference"] == "INV-1"


def test_total_mismatch_goes_to_review(pipe):
    rec = run(pipe, invoice_text(Total="9999.00"))
    assert rec.status == Status.NEEDS_REVIEW
    assert any("does not equal" in r for r in rec.review_reasons)
    assert pipe.search("Acme")["results"] == []  # not indexed until approved


def test_low_confidence_goes_to_review(pipe):
    rec = run(pipe, invoice_text(Total="1050.00 (?)"))
    assert rec.status == Status.NEEDS_REVIEW
    assert any("Low extraction confidence" in r for r in rec.review_reasons)


def test_unknown_document_goes_to_review(pipe):
    rec = run(pipe, "just some random note about lunch")
    assert rec.status == Status.NEEDS_REVIEW and rec.doc_type.value == "unknown"


def test_kyc_number_is_masked_in_storage(pipe):
    rec = run(pipe, kyc_text())
    assert rec.status == Status.AUTO_APPROVED
    assert rec.fields["document_number"] == "****4567"
    assert "P1234567" not in rec.model_dump_json()


def test_reviewer_correction_resumes_workflow(pipe):
    rec = run(pipe, invoice_text(Total="9999.00"))
    out = pipe.review(rec.doc_id, "approve", corrections={"total": 1050.0, "evil_field": "x"}, reviewer="akash")
    assert out.status == Status.APPROVED and out.fields["total"] == 1050.0
    assert "evil_field" not in out.fields
    assert out.reviewer == "akash" and out.review_reasons == []
    assert pipe.search("Acme")["results"]  # indexed after approval


def test_reject_does_not_index(pipe):
    rec = run(pipe, invoice_text(Total="9999.00"))
    assert pipe.review(rec.doc_id, "reject").status == Status.REJECTED
    assert pipe.search("Acme")["results"] == []


def test_cannot_review_twice(pipe):
    rec = run(pipe, invoice_text(Total="9999.00"))
    pipe.review(rec.doc_id, "reject")
    with pytest.raises(SubmissionError):
        pipe.review(rec.doc_id, "approve")


def test_duplicate_invoice_is_flagged(pipe):
    run(pipe, invoice_text())
    dup = run(pipe, invoice_text())
    assert dup.status == Status.NEEDS_REVIEW
    assert any("already exists" in r for r in dup.review_reasons)


def test_job_status_reports_counts(pipe):
    job = pipe.submit([item(invoice_text(), "a.txt"), item(invoice_text(Total="1.00"), "b.txt")])
    st = pipe.job_status(job.job_id)
    assert st["status"] == "completed" and st["total"] == 2
    assert st["counts"] == {"auto_approved": 1, "needs_review": 1}


def test_submission_validation(pipe):
    with pytest.raises(SubmissionError):
        pipe.submit([])
    with pytest.raises(SubmissionError):
        pipe.submit([{"filename": "x", "content_base64": "!!!", "content_type": "text/plain"}])
    with pytest.raises(SubmissionError):
        pipe.submit([{"filename": "x.exe", "content_base64": "AA==", "content_type": "application/x-msdownload"}])


def test_processing_is_idempotent(pipe):
    rec = run(pipe, invoice_text())
    again = pipe.process(rec.doc_id)
    assert again.status == Status.AUTO_APPROVED
