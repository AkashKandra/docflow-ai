"""Generate synthetic invoices and fake KYC documents with known defects + a ground-truth CSV.

Everything is fictional. Output goes to samples/generated/:
  *.txt  key/value files used by DOCFLOW_MODE=mock
  *.pdf  simple PDFs for the real Azure pipeline (needs `pip install reportlab`)
  ground_truth.csv
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

VENDORS = ["Northwind Traders", "Contoso Supplies", "Fabrikam Logistics", "Tailspin Toys", "Adatum Corp",
           "Litware Systems", "Proseware Inc", "Wingtip Hardware"]
CUSTOMERS = ["Globex Holdings", "Initech Ltd", "Umbrella Services"]
FIRST = ["Aarav", "Meera", "Liam", "Sofia", "Omar", "Chen", "Nora", "Ravi"]
LAST = ["Sharma", "Nair", "Walker", "Rossi", "Haddad", "Wei", "Berg", "Iyer"]

TODAY = date.today()


def invoice(i: int, rng: random.Random, defect: str | None) -> tuple[dict, str]:
    vendor = rng.choice(VENDORS)
    inv_date = TODAY - timedelta(days=rng.randint(10, 200))
    sub = round(rng.uniform(500, 40_000), 2)
    tax = round(sub * 0.05, 2)
    total = round(sub + tax, 2)
    number = f"INV-{2026}-{1000 + i}"
    due = inv_date + timedelta(days=30)
    if defect == "total_mismatch":
        total = round(total + rng.uniform(50, 500), 2)
    elif defect == "future_date":
        inv_date = TODAY + timedelta(days=20)
        due = inv_date + timedelta(days=30)
    elif defect == "high_value":
        sub = round(rng.uniform(120_000, 400_000), 2)
        tax = round(sub * 0.05, 2)
        total = round(sub + tax, 2)
    elif defect == "missing_number":
        number = ""
    d = {
        "vendor_name": vendor, "invoice_number": number, "invoice_date": inv_date.isoformat(),
        "due_date": due.isoformat(), "customer_name": rng.choice(CUSTOMERS),
        "subtotal": sub, "tax": tax, "total": total, "currency": "USD",
    }
    return d, "needs_review" if defect and defect != "duplicate" else "auto_approved"


def kyc(i: int, rng: random.Random, defect: str | None) -> tuple[dict, str]:
    dob = date(rng.randint(1960, 2000), rng.randint(1, 12), rng.randint(1, 28))
    exp = TODAY + timedelta(days=rng.randint(200, 2500))
    if defect == "expired":
        exp = TODAY - timedelta(days=rng.randint(10, 400))
    elif defect == "underage":
        dob = TODAY - timedelta(days=365 * 16)
    d = {
        "first_name": rng.choice(FIRST), "last_name": rng.choice(LAST),
        "document_number": f"P{rng.randint(1_000_000, 9_999_999)}",
        "date_of_birth": dob.isoformat(), "expiry_date": exp.isoformat(), "country": "IND",
    }
    return d, "needs_review" if defect else "auto_approved"


def to_text(kind: str, d: dict, low_conf: str | None) -> str:
    def mark(k: str, v) -> str:
        return f"{v} (?)" if low_conf == k else f"{v}"

    if kind == "invoice":
        total_s = f"{d['total']:.2f}"
        lines = ["INVOICE", f"Vendor: {mark('vendor_name', d['vendor_name'])}",
                 f"Invoice No: {d['invoice_number']}", f"Invoice Date: {d['invoice_date']}",
                 f"Due Date: {d['due_date']}", "Bill To:", f"Customer: {d['customer_name']}",
                 f"Subtotal: {d['subtotal']:.2f}", f"Tax: {d['tax']:.2f}",
                 f"Total: {mark('total', total_s)}", f"Currency: {d['currency']}"]
    else:
        lines = ["PASSPORT (SAMPLE - FICTIONAL)", f"Given Name: {d['first_name']}", f"Surname: {d['last_name']}",
                 f"Document No: {d['document_number']}", f"Date of Birth: {mark('date_of_birth', d['date_of_birth'])}",
                 f"Date of Expiry: {d['expiry_date']}", f"Country: {d['country']}"]
    return "\n".join(lines) + "\n"


def to_pdf(path: Path, text: str) -> bool:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        return False
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for line in text.replace(" (?)", "").splitlines():
        c.setFont("Helvetica-Bold" if y == 800 else "Helvetica", 16 if y == 800 else 11)
        c.drawString(60, y, line)
        y -= 22 if y == 800 else 18
    c.save()
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="samples/generated")
    ap.add_argument("--invoices", type=int, default=30)
    ap.add_argument("--kyc", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    inv_defects = [None] * 18 + ["total_mismatch"] * 3 + ["future_date"] * 2 + ["high_value"] * 2 \
        + ["missing_number"] * 2 + ["low_conf"] * 3
    rng.shuffle(inv_defects)
    kyc_defects = [None] * 6 + ["expired"] * 2 + ["underage"] + ["low_conf"]
    rng.shuffle(kyc_defects)

    rows, pdf_ok = [], True
    for kind, n, defects, fn in (("invoice", a.invoices, inv_defects, invoice), ("kyc_id", a.kyc, kyc_defects, kyc)):
        for i in range(n):
            defect = defects[i % len(defects)]
            d, outcome = fn(i, rng, None if defect == "low_conf" else defect)
            if defect == "low_conf":
                outcome = "needs_review"
            name = f"{kind}_{i:03d}"
            low = ("total" if kind == "invoice" else "date_of_birth") if defect == "low_conf" else None
            text = to_text(kind, d, low)
            (out / f"{name}.txt").write_text(text)
            pdf_ok = to_pdf(out / f"{name}.pdf", text) and pdf_ok
            rows.append({"filename": name, "doc_type": kind, "expected_outcome": outcome, "defect": defect or "",
                         "vendor_name": d.get("vendor_name", ""), "invoice_number": d.get("invoice_number", ""),
                         "total": d.get("total", "")})
    with open(out / "ground_truth.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} documents to {out} (PDFs: {'yes' if pdf_ok else 'no - install reportlab'})")


if __name__ == "__main__":
    main()
