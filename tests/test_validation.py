from datetime import date

from docflow.config import Settings
from docflow.llm import MockLLM
from docflow.models import DocType
from docflow.validation import dup_key, rule_checks, validate

S = Settings(mode="mock")
TODAY = date(2026, 10, 4)


def codes(issues):
    return {i.code for i in issues}


def good_invoice(**over):
    d = {"vendor_name": "Acme", "invoice_number": "1", "invoice_date": "2026-08-01", "due_date": "2026-08-31",
         "subtotal": 100.0, "tax": 5.0, "total": 105.0, "currency": "USD"}
    d.update(over)
    return d


def test_clean_invoice_has_no_issues():
    assert rule_checks(DocType.INVOICE, good_invoice(), S, TODAY) == []


def test_total_mismatch_and_missing_field():
    c = codes(rule_checks(DocType.INVOICE, good_invoice(total=200.0, invoice_number=None), S, TODAY))
    assert {"total_mismatch", "missing_field"} <= c


def test_date_rules():
    c = codes(rule_checks(DocType.INVOICE, good_invoice(invoice_date="2026-12-01", due_date="2026-11-01"), S, TODAY))
    assert {"future_date"} <= c
    c = codes(rule_checks(DocType.INVOICE, good_invoice(due_date="2026-07-01"), S, TODAY))
    assert "due_before_invoice" in c


def test_high_value_is_a_warning():
    issues = rule_checks(DocType.INVOICE, good_invoice(subtotal=200_000.0, tax=0.0, total=200_000.0), S, TODAY)
    assert [i.severity for i in issues if i.code == "high_value"] == ["warning"]


def test_duplicate_detection():
    issues = rule_checks(DocType.INVOICE, good_invoice(), S, TODAY, is_duplicate=lambda v, n: True)
    assert "duplicate_invoice" in codes(issues)


def test_kyc_rules():
    base = {"first_name": "A", "last_name": "B", "document_number": "P1234567",
            "date_of_birth": "1990-01-01", "expiry_date": "2031-01-01"}
    assert rule_checks(DocType.KYC_ID, base, S, TODAY) == []
    assert "expired_document" in codes(rule_checks(DocType.KYC_ID, {**base, "expiry_date": "2020-01-01"}, S, TODAY))
    assert "underage" in codes(rule_checks(DocType.KYC_ID, {**base, "date_of_birth": "2015-01-01"}, S, TODAY))
    assert "invalid_dob" in codes(rule_checks(DocType.KYC_ID, {**base, "date_of_birth": "2030-01-01"}, S, TODAY))


def test_dup_key_normalises():
    assert dup_key("Acme, Inc.", " inv-1 ") == dup_key("ACME INC", "INV-1")


def test_llm_failure_fails_closed():
    class Broken:
        def complete_json(self, *a):
            raise RuntimeError("down")

    res = validate(DocType.INVOICE, good_invoice(), Broken(), S, TODAY)
    assert res.llm_risk == "unknown" and "llm_unavailable" in codes(res.issues)


def test_llm_can_only_normalise_allowed_fields():
    class Sneaky:
        def complete_json(self, *a):
            return {"risk": "low", "normalized": {"vendor_name": "ACME CORP", "total": 1.0}, "anomalies": []}

    res = validate(DocType.INVOICE, good_invoice(), Sneaky(), S, TODAY)
    assert res.values["vendor_name"] == "ACME CORP" and res.values["total"] == 105.0
    assert MockLLM().complete_json("TASK:validate", "x")["risk"] == "low"
