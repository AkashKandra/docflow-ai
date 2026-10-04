"""Backfill a shared mailbox through the deployed API using Graph delta queries.

    python scripts/backfill_mailbox.py --api https://<func>.azurewebsites.net --key <function key>

The delta link is saved to .delta_link so re-runs only fetch changes (this is what keeps Graph API
calls low compared with re-listing the folder every time).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.graph_client import GraphMailClient

STATE = Path(".delta_link")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--folder", default="inbox")
    a = ap.parse_args()

    graph = GraphMailClient.from_env()
    link = STATE.read_text().strip() if STATE.exists() else None
    messages, new_link = graph.delta(a.folder, link)
    sent = 0
    for m in messages:
        if m.get("isRead") or not m.get("hasAttachments"):
            continue
        docs = list(graph.attachments(m["id"]))
        if not docs:
            continue
        for d in docs:
            d["source"] = {"sender": (m.get("from") or {}).get("emailAddress", {}).get("address"),
                           "subject": m.get("subject"), "message_id": m["id"]}
        r = requests.post(f"{a.api}/api/documents", params={"code": a.key}, json={"documents": docs}, timeout=60)
        r.raise_for_status()
        graph.mark_read(m["id"])
        sent += len(docs)
    STATE.write_text(new_link)
    print(f"Submitted {sent} attachments from {len(messages)} changed messages")


if __name__ == "__main__":
    main()
