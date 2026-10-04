"""PII minimisation. KYC identifiers are masked before they are stored or indexed."""
from __future__ import annotations

from typing import Any

from .models import DocType

MASKED_KEYS = {DocType.KYC_ID: ("document_number",)}


def mask_identifier(value: Any, keep: int = 4) -> Any:
    """'P1234567' -> '****4567'. Already-masked and short values are handled safely."""
    if value is None:
        return None
    s = str(value)
    if "*" in s:
        return s
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def redact_for_storage(doc_type: DocType, values: dict[str, Any]) -> dict[str, Any]:
    out = dict(values)
    for key in MASKED_KEYS.get(doc_type, ()):
        if key in out:
            out[key] = mask_identifier(out[key])
    return out
