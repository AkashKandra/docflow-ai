from datetime import date

from docflow.models import DocType, ParsedQuery
from docflow.search_index import build_odata_filter, parse_query, previous_quarter


def test_previous_quarter():
    assert previous_quarter(date(2026, 10, 4)) == (date(2026, 7, 1), date(2026, 9, 30))
    assert previous_quarter(date(2026, 2, 10)) == (date(2025, 10, 1), date(2025, 12, 31))
    assert previous_quarter(date(2026, 5, 1)) == (date(2026, 1, 1), date(2026, 3, 31))


def test_odata_filter_building_and_escaping():
    p = ParsedQuery(doc_type=DocType.INVOICE, party_name="O'Brien Ltd", min_amount=50000,
                    date_from="2026-07-01", date_to="2026-09-30")
    f = build_odata_filter(p)
    assert "doc_type eq 'invoice'" in f
    assert "O''Brien Ltd" in f
    assert "amount ge 50000.0" in f
    assert "doc_date ge 2026-07-01T00:00:00Z" in f and "doc_date le 2026-09-30T23:59:59Z" in f
    assert build_odata_filter(ParsedQuery(text="hello")) is None


def test_parse_query_ignores_unknown_keys_and_survives_errors():
    class Evil:
        def complete_json(self, *a):
            return {"text": "x", "min_amount": 5, "filter": "1 eq 1", "doc_type": "invoice"}

    p = parse_query(Evil(), "x")
    assert p.min_amount == 5 and p.doc_type == DocType.INVOICE

    class Broken:
        def complete_json(self, *a):
            raise RuntimeError

    assert parse_query(Broken(), "find acme").text == "find acme"
