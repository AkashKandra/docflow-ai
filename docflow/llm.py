"""LLM access: Azure OpenAI in production, a deterministic mock for offline demos/tests.

Prompts start with a `TASK:<name>` line so the mock can tell them apart.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Protocol

from .config import Settings


class LLM(Protocol):
    def complete_json(self, system: str, user: str) -> dict: ...

    def embed(self, text: str) -> list[float]: ...


class AzureOpenAILLM:
    def __init__(self, settings: Settings):
        from openai import AzureOpenAI

        self._s = settings
        if settings.aoai_key:
            self._client = AzureOpenAI(
                azure_endpoint=settings.aoai_endpoint,
                api_key=settings.aoai_key,
                api_version=settings.aoai_api_version,
                max_retries=5,
            )
        else:  # managed identity / az login
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
            self._client = AzureOpenAI(
                azure_endpoint=settings.aoai_endpoint,
                azure_ad_token_provider=provider,
                api_version=settings.aoai_api_version,
                max_retries=5,
            )

    def complete_json(self, system: str, user: str) -> dict:
        resp = self._client.chat.completions.create(
            model=self._s.aoai_chat_deployment,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content or "{}")

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(
            model=self._s.aoai_embedding_deployment, input=text[:8000]
        )
        return list(resp.data[0].embedding)


class MockLLM:
    """Rule-based stand-in so the whole pipeline runs without any Azure resources."""

    def complete_json(self, system: str, user: str) -> dict:
        task = system.splitlines()[0].strip() if system else ""
        if task == "TASK:classify":
            low = user.lower()
            if any(k in low for k in ("invoice", "bill to", "amount due")):
                return {"doc_type": "invoice", "confidence": 0.7}
            if any(k in low for k in ("passport", "identity", "date of birth")):
                return {"doc_type": "kyc_id", "confidence": 0.7}
            return {"doc_type": "unknown", "confidence": 0.2}
        if task == "TASK:validate":
            return {"risk": "low", "notes": "", "anomalies": [], "normalized": {}}
        if task == "TASK:parse_query":
            return _mock_parse_query(user)
        return {}

    def embed(self, text: str, dim: int = 32) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [b / 255 for b in digest[:dim]]


def _mock_parse_query(user: str) -> dict:
    q = user.split("QUERY:", 1)[-1].strip()
    low = q.lower()
    out: dict = {"text": q}
    if "invoice" in low:
        out["doc_type"] = "invoice"
    elif any(k in low for k in ("kyc", "passport", "id ")):
        out["doc_type"] = "kyc_id"
    m = re.search(r"(?:above|over|more than|>)\s*([\d,\.]+)", low)
    if m:
        out["min_amount"] = float(m.group(1).replace(",", ""))
    m = re.search(r"(?:below|under|less than|<)\s*([\d,\.]+)", low)
    if m:
        out["max_amount"] = float(m.group(1).replace(",", ""))
    m = re.search(r"vendor\s+([A-Za-z0-9&\.\- ]+?)(?:\s+(?:above|over|below|under|last|in)\b|$)", q, re.IGNORECASE)
    if m:
        out["party_name"] = m.group(1).strip()
    return out


def build_llm(settings: Settings) -> LLM:
    return MockLLM() if settings.mode == "mock" else AzureOpenAILLM(settings)
