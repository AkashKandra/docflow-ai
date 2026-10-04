"""Builds the Pipeline once per process from environment settings."""
from __future__ import annotations

from functools import lru_cache

from .config import Settings
from .extraction import build_extractor
from .llm import build_llm
from .pipeline import Pipeline
from .search_index import AzureSearchIndex, LocalSearchIndex
from .storage import (
    AzureBlobStore,
    AzureQueue,
    AzureTableDocStore,
    InlineQueue,
    LocalStorage,
    ensure_local_dir,
)


def build_pipeline(settings: Settings) -> Pipeline:
    llm = build_llm(settings)
    extractor = build_extractor(settings)
    if settings.mode == "mock":
        root = ensure_local_dir(settings)
        store = LocalStorage(root)
        queue = InlineQueue()
        pipe = Pipeline(settings, store, store, queue, extractor, llm, LocalSearchIndex(root))
        queue.handler = lambda m: pipe.process(m["doc_id"])
        return pipe
    return Pipeline(
        settings, AzureBlobStore(settings), AzureTableDocStore(settings), AzureQueue(settings),
        extractor, llm, AzureSearchIndex(settings, llm),
    )


@lru_cache(maxsize=1)
def get_pipeline() -> Pipeline:
    return build_pipeline(Settings.from_env())
