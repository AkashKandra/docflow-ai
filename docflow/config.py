"""Runtime settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    # "azure" uses real Azure services, "mock" runs fully offline for demos and tests.
    mode: str = "azure"
    local_data_dir: str = ".local_data"

    # Storage (documents, job/doc tables, queue)
    storage_connection: str = ""
    blob_container: str = "incoming"
    queue_name: str = "docflow-jobs"
    docs_table: str = "docflowdocs"
    jobs_table: str = "docflowjobs"

    # Azure AI Document Intelligence
    di_endpoint: str = ""
    di_key: str = ""

    # Azure OpenAI (key optional: falls back to Entra ID / managed identity)
    aoai_endpoint: str = ""
    aoai_key: str = ""
    aoai_api_version: str = "2024-10-21"
    aoai_chat_deployment: str = "gpt-4o-mini"
    aoai_embedding_deployment: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # Azure AI Search
    search_endpoint: str = ""
    search_key: str = ""
    search_index: str = "docflow-documents"
    search_use_semantic: bool = False

    # Business rules
    confidence_threshold: float = 0.85
    amount_tolerance: float = 0.01  # relative tolerance for subtotal + tax == total
    high_value_threshold: float = 100_000.0  # invoices at/above this always get reviewed
    max_attachment_mb: int = 15
    max_dequeue_count: int = 5
    allowed_content_types: tuple[str, ...] = field(
        default=(
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/tiff",
            "text/plain",  # used by mock mode samples
        )
    )

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        return cls(
            mode=e("DOCFLOW_MODE", "azure").lower(),
            local_data_dir=e("DOCFLOW_LOCAL_DIR", ".local_data"),
            storage_connection=e("AzureWebJobsStorage", ""),
            blob_container=e("DOCFLOW_BLOB_CONTAINER", "incoming"),
            queue_name=e("DOCFLOW_QUEUE_NAME", "docflow-jobs"),
            docs_table=e("DOCFLOW_DOCS_TABLE", "docflowdocs"),
            jobs_table=e("DOCFLOW_JOBS_TABLE", "docflowjobs"),
            di_endpoint=e("DOCUMENT_INTELLIGENCE_ENDPOINT", ""),
            di_key=e("DOCUMENT_INTELLIGENCE_KEY", ""),
            aoai_endpoint=e("AZURE_OPENAI_ENDPOINT", ""),
            aoai_key=e("AZURE_OPENAI_KEY", ""),
            aoai_api_version=e("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            aoai_chat_deployment=e("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o-mini"),
            aoai_embedding_deployment=e("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
            embedding_dim=int(e("EMBEDDING_DIM", "1536")),
            search_endpoint=e("AZURE_SEARCH_ENDPOINT", ""),
            search_key=e("AZURE_SEARCH_KEY", ""),
            search_index=e("AZURE_SEARCH_INDEX", "docflow-documents"),
            search_use_semantic=_b("AZURE_SEARCH_USE_SEMANTIC", False),
            confidence_threshold=_f("CONFIDENCE_THRESHOLD", 0.85),
            amount_tolerance=_f("AMOUNT_TOLERANCE", 0.01),
            high_value_threshold=_f("HIGH_VALUE_THRESHOLD", 100000),
            max_attachment_mb=int(e("MAX_ATTACHMENT_MB", "15")),
            max_dequeue_count=int(e("MAX_DEQUEUE_COUNT", "5")),
        )
