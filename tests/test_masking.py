from docflow.masking import mask_identifier, redact_for_storage
from docflow.models import DocType


def test_mask_identifier():
    assert mask_identifier("P1234567") == "****4567"
    assert mask_identifier("1234") == "****"
    assert mask_identifier("****4567") == "****4567"  # idempotent
    assert mask_identifier(None) is None


def test_redact_only_touches_kyc_identifier():
    out = redact_for_storage(DocType.KYC_ID, {"document_number": "AB123456", "first_name": "A"})
    assert out == {"document_number": "****3456", "first_name": "A"}
    inv = {"invoice_number": "INV-1"}
    assert redact_for_storage(DocType.INVOICE, inv) == inv
