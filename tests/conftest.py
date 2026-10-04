import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.config import Settings
from docflow.extraction import MockExtractor
from docflow.llm import MockLLM
from docflow.pipeline import Pipeline
from docflow.search_index import LocalSearchIndex
from docflow.storage import InlineQueue, LocalStorage

os.environ.setdefault("DOCFLOW_MODE", "mock")


def invoice_text(**over) -> str:
    d = {"Vendor": "Acme Corp", "Invoice No": "INV-1", "Invoice Date": "2026-08-01", "Due Date": "2026-08-31",
         "Customer": "Globex", "Subtotal": "1000.00", "Tax": "50.00", "Total": "1050.00", "Currency": "USD"}
    d.update(over)
    return "INVOICE\nBill To:\n" + "\n".join(f"{k}: {v}" for k, v in d.items() if v is not None) + "\n"


def kyc_text(**over) -> str:
    d = {"Given Name": "Meera", "Surname": "Nair", "Document No": "P1234567", "Date of Birth": "1990-05-04",
         "Date of Expiry": "2031-01-01", "Country": "IND"}
    d.update(over)
    return "PASSPORT\n" + "\n".join(f"{k}: {v}" for k, v in d.items()) + "\n"


def item(text: str, name: str = "doc.txt") -> dict:
    return {"filename": name, "content_type": "text/plain", "content_base64": base64.b64encode(text.encode()).decode()}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(mode="mock", local_data_dir=str(tmp_path))


@pytest.fixture
def pipe(settings, tmp_path) -> Pipeline:
    store = LocalStorage(str(tmp_path))
    queue = InlineQueue()
    p = Pipeline(settings, store, store, queue, MockExtractor(), MockLLM(), LocalSearchIndex(str(tmp_path)))
    queue.handler = lambda m: p.process(m["doc_id"])
    return p
