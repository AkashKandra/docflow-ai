"""Measure the pipeline on a labelled sample set. Use this to get *real* numbers for your README/CV.

    DOCFLOW_MODE=mock  python scripts/evaluate.py --ext txt     # offline smoke test
    DOCFLOW_MODE=azure python scripts/evaluate.py --ext pdf     # real Azure services

Reports: classification accuracy, routing accuracy (did it send the right docs to a human?),
auto-approval rate, field accuracy on auto-approved invoices, seconds per document and the
estimated reviewer time saved (assumption set by --manual-minutes).
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.config import Settings
from docflow.container import build_pipeline
from docflow.models import Status

CTYPES = {"pdf": "application/pdf", "txt": "text/plain", "png": "image/png", "jpg": "image/jpeg"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="samples/generated")
    ap.add_argument("--ext", default="txt", choices=list(CTYPES))
    ap.add_argument("--manual-minutes", type=float, default=4.0, help="assumed manual handling time per document")
    a = ap.parse_args()

    settings = Settings.from_env()
    pipe = build_pipeline(settings)
    rows = list(csv.DictReader(open(Path(a.dir) / "ground_truth.csv")))

    stats = {"n": 0, "type_ok": 0, "route_ok": 0, "auto": 0, "field_total": 0, "field_ok": 0, "seconds": 0.0}
    mismatches = []
    for row in rows:
        path = Path(a.dir) / f"{row['filename']}.{a.ext}"
        item = {"filename": path.name, "content_type": CTYPES[a.ext],
                "content_base64": base64.b64encode(path.read_bytes()).decode()}
        job = pipe.submit([item], enqueue=False)
        t0 = time.perf_counter()
        rec = pipe.process(job.doc_ids[0])
        stats["seconds"] += time.perf_counter() - t0
        stats["n"] += 1
        stats["type_ok"] += rec.doc_type.value == row["doc_type"]
        auto = rec.status == Status.AUTO_APPROVED
        stats["auto"] += auto
        stats["route_ok"] += (("auto_approved" if auto else "needs_review") == row["expected_outcome"])
        if ("auto_approved" if auto else "needs_review") != row["expected_outcome"]:
            mismatches.append({"file": path.name, "expected": row["expected_outcome"], "got": rec.status.value,
                               "reasons": rec.review_reasons})
        if auto and row["doc_type"] == "invoice":
            for key in ("vendor_name", "invoice_number", "total"):
                stats["field_total"] += 1
                got, want = rec.fields.get(key), row[key]
                stats["field_ok"] += (abs(float(got) - float(want)) < 0.01) if key == "total" else (str(got) == want)

    n = stats["n"]
    report = {
        "documents": n,
        "classification_accuracy": round(stats["type_ok"] / n, 3),
        "routing_accuracy": round(stats["route_ok"] / n, 3),
        "auto_approval_rate": round(stats["auto"] / n, 3),
        "invoice_field_accuracy_on_auto_approved": round(stats["field_ok"] / stats["field_total"], 3) if stats["field_total"] else None,
        "avg_seconds_per_document": round(stats["seconds"] / n, 2),
        "estimated_manual_minutes_saved": round(stats["auto"] * a.manual_minutes, 1),
        "assumption_manual_minutes_per_doc": a.manual_minutes,
        "routing_mismatches": mismatches,
    }
    Path("eval_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
