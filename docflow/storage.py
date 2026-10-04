"""Storage backends: Azure (Blob + Table + Queue) and a file-based local/mock version."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .config import Settings
from .models import DocumentRecord, JobRecord
from .validation import dup_key


class BlobStore(Protocol):
    def put(self, name: str, data: bytes, content_type: str) -> None: ...
    def get(self, name: str) -> bytes: ...


class DocStore(Protocol):
    def save_doc(self, rec: DocumentRecord) -> None: ...
    def get_doc(self, doc_id: str) -> DocumentRecord | None: ...
    def list_docs(self, status: str | None = None, job_id: str | None = None, limit: int = 100) -> list[DocumentRecord]: ...
    def has_duplicate(self, vendor: str, number: str, exclude_doc_id: str) -> bool: ...
    def save_job(self, job: JobRecord) -> None: ...
    def get_job(self, job_id: str) -> JobRecord | None: ...


class Queue(Protocol):
    def send(self, message: dict) -> None: ...


# ------------------------------------------------------------------ local / mock

class LocalStorage:
    """Blobs and records as files under a directory. Good for demos and tests."""

    def __init__(self, root: str):
        self.root = Path(root)
        for sub in ("blobs", "docs", "jobs"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # BlobStore
    def put(self, name: str, data: bytes, content_type: str) -> None:
        path = self.root / "blobs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, name: str) -> bytes:
        return (self.root / "blobs" / name).read_bytes()

    # DocStore
    def save_doc(self, rec: DocumentRecord) -> None:
        (self.root / "docs" / f"{rec.doc_id}.json").write_text(rec.model_dump_json(indent=2))

    def get_doc(self, doc_id: str) -> DocumentRecord | None:
        p = self.root / "docs" / f"{doc_id}.json"
        return DocumentRecord.model_validate_json(p.read_text()) if p.exists() else None

    def list_docs(self, status=None, job_id=None, limit=100):
        out = []
        for p in sorted((self.root / "docs").glob("*.json")):
            rec = DocumentRecord.model_validate_json(p.read_text())
            if status and rec.status.value != status:
                continue
            if job_id and rec.job_id != job_id:
                continue
            out.append(rec)
        return out[:limit]

    def has_duplicate(self, vendor, number, exclude_doc_id):
        key = dup_key(vendor, number)
        return any(
            d.doc_id != exclude_doc_id
            and d.status.value in {"auto_approved", "approved"}
            and dup_key(d.fields.get("vendor_name"), d.fields.get("invoice_number")) == key
            for d in self.list_docs(limit=10_000)
        )

    def save_job(self, job: JobRecord) -> None:
        (self.root / "jobs" / f"{job.job_id}.json").write_text(job.model_dump_json(indent=2))

    def get_job(self, job_id: str) -> JobRecord | None:
        p = self.root / "jobs" / f"{job_id}.json"
        return JobRecord.model_validate_json(p.read_text()) if p.exists() else None


class InlineQueue:
    """Mock queue: processes the message immediately in-process."""

    def __init__(self) -> None:
        self.handler: Callable[[dict], None] | None = None

    def send(self, message: dict) -> None:
        if self.handler:
            self.handler(message)


# ------------------------------------------------------------------ azure

class AzureBlobStore:
    def __init__(self, settings: Settings):
        from azure.storage.blob import BlobServiceClient

        svc = BlobServiceClient.from_connection_string(settings.storage_connection)
        self._c = svc.get_container_client(settings.blob_container)
        if not self._c.exists():
            self._c.create_container()

    def put(self, name: str, data: bytes, content_type: str) -> None:
        from azure.storage.blob import ContentSettings

        self._c.upload_blob(name, data, overwrite=True, content_settings=ContentSettings(content_type=content_type))

    def get(self, name: str) -> bytes:
        return self._c.download_blob(name).readall()


class AzureTableDocStore:
    """One table for documents, one for jobs. Record JSON lives in `payload`;
    status/job_id/dup_key are separate columns so we can filter server-side."""

    def __init__(self, settings: Settings):
        from azure.data.tables import TableServiceClient

        svc = TableServiceClient.from_connection_string(settings.storage_connection)
        self._docs = svc.create_table_if_not_exists(settings.docs_table)
        self._jobs = svc.create_table_if_not_exists(settings.jobs_table)

    def save_doc(self, rec: DocumentRecord) -> None:
        self._docs.upsert_entity(
            {
                "PartitionKey": "doc",
                "RowKey": rec.doc_id,
                "job_id": rec.job_id,
                "status": rec.status.value,
                "dup_key": dup_key(rec.fields.get("vendor_name"), rec.fields.get("invoice_number")),
                "payload": rec.model_dump_json(),
            }
        )

    def get_doc(self, doc_id: str) -> DocumentRecord | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return DocumentRecord.model_validate_json(self._docs.get_entity("doc", doc_id)["payload"])
        except ResourceNotFoundError:
            return None

    def list_docs(self, status=None, job_id=None, limit=100):
        parts = ["PartitionKey eq 'doc'"]
        params: dict = {}
        if status:
            parts.append("status eq @status")
            params["status"] = status
        if job_id:
            parts.append("job_id eq @job_id")
            params["job_id"] = job_id
        rows = self._docs.query_entities(" and ".join(parts), parameters=params, results_per_page=limit)
        out = []
        for row in rows:
            out.append(DocumentRecord.model_validate_json(row["payload"]))
            if len(out) >= limit:
                break
        return out

    def has_duplicate(self, vendor, number, exclude_doc_id):
        key = dup_key(vendor, number)
        rows = self._docs.query_entities(
            "PartitionKey eq 'doc' and dup_key eq @k",
            parameters={"k": key},
            select=["RowKey", "status"],
        )
        return any(r["RowKey"] != exclude_doc_id and r["status"] in {"auto_approved", "approved"} for r in rows)

    def save_job(self, job: JobRecord) -> None:
        self._jobs.upsert_entity({"PartitionKey": "job", "RowKey": job.job_id, "payload": job.model_dump_json()})

    def get_job(self, job_id: str) -> JobRecord | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return JobRecord.model_validate_json(self._jobs.get_entity("job", job_id)["payload"])
        except ResourceNotFoundError:
            return None


class AzureQueue:
    def __init__(self, settings: Settings):
        from azure.storage.queue import QueueClient

        self._q = QueueClient.from_connection_string(settings.storage_connection, settings.queue_name)
        try:
            self._q.create_queue()
        except Exception:  # noqa: BLE001 - already exists
            pass

    def send(self, message: dict) -> None:
        # host.json sets messageEncoding=none, so plain JSON text is what the trigger expects.
        self._q.send_message(json.dumps(message))


def ensure_local_dir(settings: Settings) -> str:
    os.makedirs(settings.local_data_dir, exist_ok=True)
    return settings.local_data_dir
