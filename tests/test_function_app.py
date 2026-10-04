"""HTTP layer tests against the real function handlers in mock mode."""
import json
import os

import pytest

pytest.importorskip("azure.functions")
import azure.functions as func
from conftest import invoice_text, item


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCFLOW_MODE", "mock")
    monkeypatch.setenv("DOCFLOW_LOCAL_DIR", str(tmp_path))
    import function_app
    from docflow.container import get_pipeline

    get_pipeline.cache_clear()
    yield function_app
    get_pipeline.cache_clear()


def call(fn, method="GET", body=None, params=None, route=None):
    req = func.HttpRequest(
        method=method, url="/api/x", params=params or {}, route_params=route or {},
        body=json.dumps(body).encode() if body is not None else b"",
        headers={"Content-Type": "application/json"},
    )
    resp = fn.build().get_user_function()(req)
    return resp.status_code, json.loads(resp.get_body())


def test_submit_poll_review_search(app_module):
    code, out = call(app_module.submit_documents, "POST",
                     {"documents": [item(invoice_text(Total="9999.00"))]})
    assert code == 202
    code, job = call(app_module.job_status, route={"job_id": out["job_id"]})
    assert code == 200 and job["counts"] == {"needs_review": 1}

    code, queue = call(app_module.review_queue)
    doc_id = queue[0]["doc_id"]
    code, rec = call(app_module.review_document, "POST",
                     {"decision": "approve", "corrections": {"total": 1050.0}}, route={"doc_id": doc_id})
    assert code == 200 and rec["status"] == "approved"

    code, res = call(app_module.search, params={"q": "Acme"})
    assert code == 200 and res["results"]


def test_bad_requests(app_module):
    assert call(app_module.submit_documents, "POST", {"documents": []})[0] == 400
    assert call(app_module.search)[0] == 400
    assert call(app_module.job_status, route={"job_id": "nope"})[0] == 404
    assert call(app_module.review_document, "POST", {"decision": "approve"}, route={"doc_id": "nope"})[0] == 404
    assert os.environ["DOCFLOW_MODE"] == "mock"
