"""Microsoft Graph helper for mailbox backfills and read/unread sync.

The Logic App handles live capture. This client is for the cases Logic Apps is awkward at:
bulk backfill of an existing mailbox and incremental sync using Graph *delta queries*, which return
only what changed since the last call instead of re-listing the folder.
"""
from __future__ import annotations

import base64
import os
from collections.abc import Iterator

import requests

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphMailClient:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, mailbox: str):
        import msal

        self._app = msal.ConfidentialClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant_id}", client_credential=client_secret
        )
        self._mailbox = mailbox

    @classmethod
    def from_env(cls) -> GraphMailClient:
        e = os.environ
        return cls(e["GRAPH_TENANT_ID"], e["GRAPH_CLIENT_ID"], e["GRAPH_CLIENT_SECRET"], e["GRAPH_MAILBOX"])

    def _headers(self) -> dict[str, str]:
        tok = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in tok:
            raise RuntimeError(f"Graph auth failed: {tok.get('error_description')}")
        return {"Authorization": f"Bearer {tok['access_token']}"}

    def _get(self, url: str, **params) -> dict:
        r = requests.get(url, headers=self._headers(), params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    def delta(self, folder: str = "inbox", delta_link: str | None = None) -> tuple[list[dict], str]:
        """Return (changed messages, new delta link). Persist the link and pass it next time."""
        url = delta_link or f"{GRAPH}/users/{self._mailbox}/mailFolders/{folder}/messages/delta"
        params = {} if delta_link else {"$select": "id,subject,from,isRead,hasAttachments,receivedDateTime"}
        changed: list[dict] = []
        while True:
            page = self._get(url, **params)
            changed += page.get("value", [])
            if "@odata.nextLink" in page:
                url, params = page["@odata.nextLink"], {}
            else:
                return changed, page["@odata.deltaLink"]

    def attachments(self, message_id: str) -> Iterator[dict]:
        url = f"{GRAPH}/users/{self._mailbox}/messages/{message_id}/attachments"
        for a in self._get(url).get("value", []):
            if a.get("@odata.type") == "#microsoft.graph.fileAttachment":
                yield {
                    "filename": a["name"],
                    "content_type": a["contentType"],
                    "content_base64": base64.b64encode(base64.b64decode(a["contentBytes"])).decode(),
                }

    def mark_read(self, message_id: str) -> None:
        r = requests.patch(
            f"{GRAPH}/users/{self._mailbox}/messages/{message_id}",
            headers=self._headers(), json={"isRead": True}, timeout=30,
        )
        r.raise_for_status()
