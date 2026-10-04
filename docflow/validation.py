"""Business-rule validation plus an LLM sanity check on the extracted fields."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import Settings
from .llm import LLM
from .models import DocType, Issue

log = logging.getLogger(__name__)

REQUIRED = {
    DocType.INVOICE: ["vendor_name", "invoice_number", "invoice_date", "total"],
    DocType.KYC_ID: ["first_name", "last_name", "document_number", "date_of_birth", "expiry_date"],
}
# The LLM may only normalise these (never invent numbers, dates or IDs).
NORMALISABLE = {"vendor_name", "customer_name", "currency", "first_name", "last_name", "country"}

VALIDATE_SYSTEM = """TASK:validate
You review structured fields extracted from a business document and flag anomalies.
Return JSON: {"risk": "low"|"medium"|"high", "notes": str, "anomalies": [str],
"normalized": {"vendor_name"?: str, "customer_name"?: str, "currency"?: ISO-4217, "first_name"?: str,
"last_name"?: str, "country"?: str}}.
Only normalise formatting (casing, whitespace, legal suffixes, currency symbols to ISO codes).
Never change numbers, dates or identifiers. Risk is "high" for implausible or suspicious combinations."""

DuplicateLookup = Callable[[str, str], bool]


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    llm_risk: str = "low"
    llm_notes: str = ""


def _d(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except ValueError:
        return None


def _num(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def dup_key(vendor: Any, number: Any) -> str:
    norm = re.sub(r"[^a-z0-9]", "", str(vendor or "").lower())
    return f"{norm}|{str(number or '').strip().upper()}"


def rule_checks(
    doc_type: DocType,
    values: dict[str, Any],
    settings: Settings,
    today: date,
    is_duplicate: DuplicateLookup | None = None,
) -> list[Issue]:
    issues: list[Issue] = []

    def err(code: str, msg: str, f: str | None = None) -> None:
        issues.append(Issue(code=code, severity="error", message=msg, field=f))

    def warn(code: str, msg: str, f: str | None = None) -> None:
        issues.append(Issue(code=code, severity="warning", message=msg, field=f))

    for f in REQUIRED.get(doc_type, []):
        if values.get(f) in (None, ""):
            err("missing_field", f"Required field '{f}' is missing", f)

    if doc_type == DocType.INVOICE:
        total, sub, tax = _num(values.get("total")), _num(values.get("subtotal")), _num(values.get("tax"))
        inv_date, due = _d(values.get("invoice_date")), _d(values.get("due_date"))
        if total is not None:
            if total <= 0:
                err("non_positive_total", "Invoice total must be greater than zero", "total")
            if total >= settings.high_value_threshold:
                warn("high_value", f"Invoice total {total:,.2f} is at or above the review threshold", "total")
            if sub is not None and tax is not None:
                if abs((sub + tax) - total) > max(0.01, settings.amount_tolerance * abs(total)):
                    err("total_mismatch", f"Subtotal {sub:,.2f} + tax {tax:,.2f} does not equal total {total:,.2f}", "total")
        if not values.get("currency"):
            warn("missing_currency", "No currency detected", "currency")
        if inv_date:
            if inv_date > today:
                err("future_date", "Invoice date is in the future", "invoice_date")
            elif inv_date < today - timedelta(days=730):
                warn("stale_invoice", "Invoice is more than two years old", "invoice_date")
            if due and due < inv_date:
                err("due_before_invoice", "Due date is earlier than the invoice date", "due_date")
        elif values.get("invoice_date"):
            err("unparseable_date", "Invoice date could not be parsed", "invoice_date")
        if is_duplicate and values.get("vendor_name") and values.get("invoice_number"):
            if is_duplicate(values["vendor_name"], values["invoice_number"]):
                err("duplicate_invoice", "An invoice with this vendor and number already exists", "invoice_number")

    elif doc_type == DocType.KYC_ID:
        dob, exp = _d(values.get("date_of_birth")), _d(values.get("expiry_date"))
        if exp:
            if exp < today:
                err("expired_document", "Identity document has expired", "expiry_date")
            elif exp < today + timedelta(days=30):
                warn("expiring_soon", "Identity document expires within 30 days", "expiry_date")
        elif values.get("expiry_date"):
            err("unparseable_date", "Expiry date could not be parsed", "expiry_date")
        if dob:
            age = (today - dob).days / 365.25
            if dob > today or age > 120:
                err("invalid_dob", "Date of birth is not plausible", "date_of_birth")
            elif age < 18:
                err("underage", "Customer is under 18", "date_of_birth")
        elif values.get("date_of_birth"):
            err("unparseable_date", "Date of birth could not be parsed", "date_of_birth")
        num = re.sub(r"[^A-Za-z0-9]", "", str(values.get("document_number") or ""))
        if values.get("document_number") and len(num) < 5:
            warn("short_document_number", "Document number looks too short", "document_number")
    return issues


def validate(
    doc_type: DocType,
    values: dict[str, Any],
    llm: LLM,
    settings: Settings,
    today: date | None = None,
    is_duplicate: DuplicateLookup | None = None,
) -> ValidationResult:
    today = today or date.today()
    res = ValidationResult(values=dict(values))
    res.issues = rule_checks(doc_type, values, settings, today, is_duplicate)

    try:
        # Only structured fields go to the model, never raw document text.
        out = llm.complete_json(VALIDATE_SYSTEM, f"doc_type={doc_type.value}\nfields={values}")
        res.llm_risk = out.get("risk") if out.get("risk") in {"low", "medium", "high"} else "unknown"
        res.llm_notes = str(out.get("notes", ""))[:500]
        for key, val in (out.get("normalized") or {}).items():
            if key in NORMALISABLE and isinstance(val, str) and key in res.values and val.strip():
                res.values[key] = val.strip()
        for a in out.get("anomalies") or []:
            res.issues.append(Issue(code="llm_anomaly", severity="warning", message=str(a)[:300]))
    except Exception as exc:  # noqa: BLE001 - fail closed: send to a human
        log.warning("LLM validation failed: %s", exc)
        res.llm_risk = "unknown"
        res.issues.append(
            Issue(code="llm_unavailable", severity="warning", message="LLM validation unavailable")
        )
    return res
