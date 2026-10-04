"""Search over approved documents: Azure AI Search (hybrid vector + keyword) or a local fallback.

Natural-language queries ("invoices from vendor X above 50,000 last quarter") are turned into
structured filters by the LLM, then executed as filter + hybrid search.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .llm import LLM
from .models import DocType, DocumentRecord, ParsedQuery

PARSE_SYSTEM = """TASK:parse_query
Convert a search request about invoices / KYC documents into JSON filters.
Return: {"text": str, "doc_type": "invoice"|"kyc_id"|null, "party_name": str|null,
"min_amount": number|null, "max_amount": number|null, "date_from": "YYYY-MM-DD"|null,
"date_to": "YYYY-MM-DD"|null}.
Use the supplied TODAY and PREVIOUS_QUARTER values to resolve relative dates.
`text` is the free-text part left after removing the structured filters."""


def previous_quarter(today: date) -> tuple[date, date]:
    q = (today.month - 1) // 3  # 0..3 current quarter index
    year, q = (today.year - 1, 3) if q == 0 else (today.year, q - 1)
    start = date(year, q * 3 + 1, 1)
    next_start = date(year + (1 if q == 3 else 0), 1 if q == 3 else q * 3 + 4, 1)
    return start, next_start - timedelta(days=1)


def parse_query(llm: LLM, query: str, today: date | None = None) -> ParsedQuery:
    today = today or date.today()
    start, end = previous_quarter(today)
    user = f"TODAY={today}\nPREVIOUS_QUARTER={start}..{end}\nQUERY:{query}"
    try:
        data = llm.complete_json(PARSE_SYSTEM, user)
        data.setdefault("text", query)
        # Allow-list the keys: model output is untrusted.
        allowed = {k: data[k] for k in ParsedQuery.model_fields if k in data and data[k] not in ("", None)}
        return ParsedQuery(**allowed)
    except Exception:  # noqa: BLE001 - fall back to plain keyword search
        return ParsedQuery(text=query)


def _q(s: str) -> str:
    return s.replace("'", "''")


def build_odata_filter(p: ParsedQuery) -> str | None:
    parts: list[str] = []
    if p.doc_type:
        parts.append(f"doc_type eq '{_q(p.doc_type.value)}'")
    if p.party_name:
        parts.append(f"search.ismatch('\"{_q(p.party_name)}\"', 'party_name', 'simple', 'all')")
    if p.min_amount is not None:
        parts.append(f"amount ge {float(p.min_amount)}")
    if p.max_amount is not None:
        parts.append(f"amount le {float(p.max_amount)}")
    if p.date_from:
        parts.append(f"doc_date ge {date.fromisoformat(p.date_from)}T00:00:00Z")
    if p.date_to:
        parts.append(f"doc_date le {date.fromisoformat(p.date_to)}T23:59:59Z")
    return " and ".join(parts) or None


def build_index_document(rec: DocumentRecord) -> tuple[dict[str, Any], str]:
    """Return (index document without vector, text to embed). KYC identifiers are already masked."""
    f = rec.fields
    if rec.doc_type == DocType.INVOICE:
        party, ref = f.get("vendor_name"), f.get("invoice_number")
        doc_date, amount = f.get("invoice_date"), f.get("total")
        summary = (
            f"Invoice {ref} from {party} to {f.get('customer_name') or 'n/a'}, dated {doc_date}, "
            f"total {amount} {f.get('currency') or ''}, due {f.get('due_date') or 'n/a'}."
        )
    else:
        party = " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x)
        ref, doc_date, amount = f.get("document_number"), f.get("date_of_birth"), None
        summary = f"KYC identity document for {party}, number {ref}, country {f.get('country')}, expires {f.get('expiry_date')}."
    doc = {
        "id": rec.doc_id,
        "doc_id": rec.doc_id,
        "doc_type": rec.doc_type.value,
        "party_name": party,
        "reference": ref,
        "filename": rec.filename,
        "summary": summary,
        "amount": float(amount) if amount not in (None, "") else None,
        "currency": f.get("currency"),
        "doc_date": f"{doc_date}T00:00:00Z" if doc_date else None,
        "expiry_date": f"{f['expiry_date']}T00:00:00Z" if f.get("expiry_date") else None,
        "country": f.get("country"),
        "status": rec.status.value,
    }
    return doc, summary


class SearchIndex(Protocol):
    def upsert(self, rec: DocumentRecord) -> None: ...
    def search(self, q: ParsedQuery, top: int = 5) -> list[dict]: ...


class AzureSearchIndex:
    def __init__(self, settings: Settings, llm: LLM):
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient

        self._s, self._llm = settings, llm
        if settings.search_key:
            cred: Any = AzureKeyCredential(settings.search_key)
        else:
            from azure.identity import DefaultAzureCredential

            cred = DefaultAzureCredential()
        self._index_client = SearchIndexClient(settings.search_endpoint, cred)
        self._client = SearchClient(settings.search_endpoint, settings.search_index, cred)
        self.ensure_index()

    def ensure_index(self) -> None:
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            SearchableField,
            SearchField,
            SearchFieldDataType,
            SemanticConfiguration,
            SemanticField,
            SemanticPrioritizedFields,
            SemanticSearch,
            SimpleField,
            VectorSearch,
            VectorSearchProfile,
        )
        from azure.search.documents.indexes.models import (
            SearchIndex as Idx,
        )

        S, D = SearchFieldDataType.String, SearchFieldDataType
        fields = [
            SimpleField(name="id", type=S, key=True),
            SimpleField(name="doc_id", type=S, filterable=True),
            SimpleField(name="doc_type", type=S, filterable=True, facetable=True),
            SearchableField(name="party_name", filterable=True, sortable=True),
            SearchableField(name="reference", filterable=True),
            SimpleField(name="filename", type=S),
            SearchableField(name="summary"),
            SimpleField(name="amount", type=D.Double, filterable=True, sortable=True),
            SimpleField(name="currency", type=S, filterable=True),
            SimpleField(name="doc_date", type=D.DateTimeOffset, filterable=True, sortable=True),
            SimpleField(name="expiry_date", type=D.DateTimeOffset, filterable=True),
            SimpleField(name="country", type=S, filterable=True),
            SimpleField(name="status", type=S, filterable=True),
            SearchField(
                name="content_vector",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=self._s.embedding_dim,
                vector_search_profile_name="vec-profile",
            ),
        ]
        index = Idx(
            name=self._s.search_index,
            fields=fields,
            vector_search=VectorSearch(
                algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
                profiles=[VectorSearchProfile(name="vec-profile", algorithm_configuration_name="hnsw")],
            ),
            semantic_search=SemanticSearch(
                configurations=[
                    SemanticConfiguration(
                        name="default",
                        prioritized_fields=SemanticPrioritizedFields(
                            title_field=SemanticField(field_name="party_name"),
                            content_fields=[SemanticField(field_name="summary")],
                        ),
                    )
                ]
            ),
        )
        self._index_client.create_or_update_index(index)

    def upsert(self, rec: DocumentRecord) -> None:
        doc, text = build_index_document(rec)
        doc["content_vector"] = self._llm.embed(text)
        self._client.merge_or_upload_documents([doc])

    def search(self, q: ParsedQuery, top: int = 5) -> list[dict]:
        from azure.search.documents.models import VectorizedQuery

        text = q.text or "*"
        kwargs: dict[str, Any] = {
            "search_text": text,
            "vector_queries": [
                VectorizedQuery(vector=self._llm.embed(text), k_nearest_neighbors=top, fields="content_vector")
            ],
            "filter": build_odata_filter(q),
            "top": top,
            "select": ["doc_id", "doc_type", "party_name", "reference", "amount", "currency", "doc_date", "summary"],
        }
        if self._s.search_use_semantic:
            kwargs.update(query_type="semantic", semantic_configuration_name="default")
        return [
            {**{k: v for k, v in r.items() if not k.startswith("@")}, "score": r.get("@search.score")}
            for r in self._client.search(**kwargs)
        ]


class LocalSearchIndex:
    """Token-overlap search with the same filters; JSON file on disk."""

    def __init__(self, root: str):
        self._path = Path(root) / "index.json"
        self._docs: dict[str, dict] = json.loads(self._path.read_text()) if self._path.exists() else {}

    def upsert(self, rec: DocumentRecord) -> None:
        doc, _ = build_index_document(rec)
        self._docs[rec.doc_id] = doc
        self._path.write_text(json.dumps(self._docs))

    def search(self, q: ParsedQuery, top: int = 5) -> list[dict]:
        words = set(re.findall(r"\w+", (q.text or "").lower())) - {"show", "me", "all", "the", "from", "invoices", "invoice", "above", "below"}
        results = []
        for d in self._docs.values():
            if q.doc_type and d["doc_type"] != q.doc_type.value:
                continue
            if q.party_name and q.party_name.lower() not in (d["party_name"] or "").lower():
                continue
            if q.min_amount is not None and (d["amount"] is None or d["amount"] < q.min_amount):
                continue
            if q.max_amount is not None and (d["amount"] is None or d["amount"] > q.max_amount):
                continue
            day = (d["doc_date"] or "")[:10]
            if q.date_from and (not day or day < q.date_from):
                continue
            if q.date_to and (not day or day > q.date_to):
                continue
            hay = set(re.findall(r"\w+", d["summary"].lower()))
            results.append({**d, "score": len(words & hay)})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top]
