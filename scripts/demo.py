"""End-to-end offline demo (mock mode): submit samples, review one, search.

    python scripts/generate_samples.py && python scripts/demo.py
"""
from __future__ import annotations

import base64
import os
import shutil
import sys
from pathlib import Path

os.environ["DOCFLOW_MODE"] = "mock"
os.environ["DOCFLOW_LOCAL_DIR"] = ".local_data/demo"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.config import Settings
from docflow.container import build_pipeline


def main() -> None:
    shutil.rmtree(".local_data/demo", ignore_errors=True)
    pipe = build_pipeline(Settings.from_env())
    files = sorted(Path("samples/generated").glob("*.txt"))
    if not files:
        sys.exit("Run scripts/generate_samples.py first")
    items = [{"filename": f.name, "content_type": "text/plain",
              "content_base64": base64.b64encode(f.read_bytes()).decode()} for f in files]
    job = pipe.submit(items)  # mock queue processes inline
    st = pipe.job_status(job.job_id)
    print(f"Job {job.job_id}: {st['status']}  {st['counts']}")

    waiting = pipe.docs.list_docs(status="needs_review")
    print(f"\n{len(waiting)} documents waiting for review, e.g.:")
    for d in waiting[:3]:
        print(f"  {d.filename}: {'; '.join(d.review_reasons)}")
    if waiting:
        d = waiting[0]
        out = pipe.review(d.doc_id, "approve", reviewer="demo", comment="checked manually")
        print(f"\nReviewer approved {d.filename} -> {out.status.value}")

    print("\nSearch: 'invoices above 20000'")
    for r in pipe.search("invoices above 20000", top=3)["results"]:
        print(f"  {r['party_name']}  {r['reference']}  {r['amount']}")


if __name__ == "__main__":
    main()
