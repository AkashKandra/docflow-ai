"""Field extraction with Azure AI Document Intelligence (plus an offline mock)."""
from __future__ import annotations

import re
from typing import Any, Protocol

from .config import Settings
from .models import DocType, ExtractionResult, FieldValue

INVOICE_FIELDS = {
    "VendorName": "vendor_name",
    "VendorAddress": "vendor_address",
    "InvoiceId": "invoice_number",
    "InvoiceDate": "invoice_date",
    "DueDate": "due_date",
    "CustomerName": "customer_name",
    "PurchaseOrder": "po_number",
    "SubTotal": "subtotal",
    "TotalTax": "tax",
    "InvoiceTotal": "total",
}
ID_FIELDS = {
    "FirstName": "first_name",
    "LastName": "last_name",
    "DocumentNumber": "document_number",
    "DateOfBirth": "date_of_birth",
    "DateOfExpiration": "expiry_date",
    "CountryRegion": "country",
    "Address": "address",
}


class Extractor(Protocol):
    def read_text(self, data: bytes) -> str: ...

    def extract(self, data: bytes, doc_type: DocType) -> ExtractionResult: ...


def _field_value(field: Any) -> Any:
    """Convert a Document Intelligence DocumentField into a plain JSON-friendly value."""
    if field is None:
        return None
    cur = getattr(field, "value_currency", None)
    if cur is not None:
        return float(cur.amount)
    for attr in ("value_date", "value_number", "value_integer", "value_string", "value_country_region"):
        v = getattr(field, attr, None)
        if v is not None:
            return v.isoformat() if hasattr(v, "isoformat") else v
    return getattr(field, "content", None)


class AzureExtractor:
    def __init__(self, settings: Settings):
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        if settings.di_key:
            cred: Any = AzureKeyCredential(settings.di_key)
        else:
            from azure.identity import DefaultAzureCredential

            cred = DefaultAzureCredential()
        self._client = DocumentIntelligenceClient(settings.di_endpoint, cred)

    def _analyze(self, model_id: str, data: bytes):
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

        poller = self._client.begin_analyze_document(
            model_id, AnalyzeDocumentRequest(bytes_source=data)
        )
        return poller.result()

    def read_text(self, data: bytes) -> str:
        return self._analyze("prebuilt-read", data).content or ""

    def extract(self, data: bytes, doc_type: DocType) -> ExtractionResult:
        model, mapping = (
            ("prebuilt-invoice", INVOICE_FIELDS)
            if doc_type == DocType.INVOICE
            else ("prebuilt-idDocument", ID_FIELDS)
        )
        result = self._analyze(model, data)
        fields: dict[str, FieldValue] = {}
        if result.documents:
            doc = result.documents[0]
            raw = doc.fields or {}
            for src, dst in mapping.items():
                if src in raw:
                    fields[dst] = FieldValue(value=_field_value(raw[src]), confidence=raw[src].confidence)
            if doc_type == DocType.INVOICE and "InvoiceTotal" in raw:
                cur = getattr(raw["InvoiceTotal"], "value_currency", None)
                if cur is not None and getattr(cur, "currency_code", None):
                    fields["currency"] = FieldValue(
                        value=cur.currency_code, confidence=raw["InvoiceTotal"].confidence
                    )
            if doc_type == DocType.INVOICE and "Items" in raw:
                items = getattr(raw["Items"], "value_array", None) or []
                fields["line_item_count"] = FieldValue(value=len(items), confidence=raw["Items"].confidence)
        return ExtractionResult(doc_type=doc_type, fields=fields, raw_text=result.content or "")


# --------------------------------------------------------------------------- mock

_MOCK_KEYS = {
    "vendor": "vendor_name",
    "invoice no": "invoice_number",
    "invoice date": "invoice_date",
    "due date": "due_date",
    "customer": "customer_name",
    "subtotal": "subtotal",
    "tax": "tax",
    "total": "total",
    "currency": "currency",
    "given name": "first_name",
    "surname": "last_name",
    "document no": "document_number",
    "date of birth": "date_of_birth",
    "date of expiry": "expiry_date",
    "country": "country",
}
_NUMERIC = {"subtotal", "tax", "total"}


class MockExtractor:
    """Reads `Key: Value` lines from a text file. A trailing `(?)` marks a low-confidence field."""

    def read_text(self, data: bytes) -> str:
        return data.decode("utf-8", errors="ignore")

    def extract(self, data: bytes, doc_type: DocType) -> ExtractionResult:
        text = self.read_text(data)
        fields: dict[str, FieldValue] = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            name = _MOCK_KEYS.get(key.strip().lower())
            if not name:
                continue
            low_conf = val.strip().endswith("(?)")
            val = val.replace("(?)", "").strip()
            value: Any | None = val or None
            if value is not None and name in _NUMERIC:
                value = float(re.sub(r"[^\d.\-]", "", value) or 0)
            fields[name] = FieldValue(value=value, confidence=0.5 if low_conf else 0.96)
        return ExtractionResult(doc_type=doc_type, fields=fields, raw_text=text)


def build_extractor(settings: Settings) -> Extractor:
    return MockExtractor() if settings.mode == "mock" else AzureExtractor(settings)
