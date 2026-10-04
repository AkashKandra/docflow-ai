# DocFlow AI: Invoice & KYC Document Processing Automation

Automated pipeline that captures invoice and KYC documents from a shared mailbox, extracts and validates the key fields, auto-approves the confident ones and routes the rest to a human reviewer with the reason attached. Approved documents are searchable in natural language.

> Personal project built on a synthetic dataset. All sample data is fictional.

## Architecture

```
 Shared mailbox
      │  (new email with attachment)
      ▼
 Azure Logic App ──POST /api/documents──▶ Azure Function (HTTP)
  filter + shape attachments                │ store blob, create job, enqueue 1 msg/doc
  mark email read (Graph)                   ▼
                                     Azure Storage Queue
                                            │
                                            ▼
                               Azure Function (queue trigger)
                                            │
                    ┌───────────────────────┴────────────────────────┐
                    │              LangGraph workflow                │
                    │ classify → extract → validate → decide         │
                    │   (keywords  (Document    (rules +   │         │
                    │    then LLM)  Intelligence) Azure     │         │
                    │                              OpenAI) │         │
                    │            auto_approve ◀────────────┤         │
                    │            human_review ◀────────────┘         │
                    └───────────────┬───────────────┬────────────────┘
                                    │ approved      │ needs review
                                    ▼               ▼
                           Azure AI Search     GET /api/review
                      (vector + keyword hybrid)  POST /api/review/{id}
                                    ▲               │ approve / reject / correct
                                    └───────────────┘ (workflow resumes, then indexes)
```

| Concern | How it is handled |
|---|---|
| Capture | Logic App trigger on a shared mailbox, filters supported attachment types, one API call per email. `scripts/backfill_mailbox.py` uses Microsoft Graph **delta queries** for bulk or incremental syncs. |
| Classification | Keyword scoring first (free), LLM only when ambiguous. Document text is treated as untrusted input. |
| Extraction | Document Intelligence `prebuilt-invoice` and `prebuilt-idDocument`, with per-field confidence. |
| Validation | Deterministic rules (totals, dates, duplicates, expiry, age) plus an LLM sanity check. The LLM may only normalise formatting of a fixed set of text fields. It never changes numbers, dates or IDs. If the LLM call fails, the document goes to review (fail closed). |
| Routing | Auto-approve only if there are no rule errors, the minimum required-field confidence is at least the threshold, the LLM risk is low or medium, and the invoice is below the high-value limit. Everything else goes to a person with the reasons listed. |
| Human in the loop | The LangGraph run ends at `human_review` and the state is persisted in the document record. When the reviewer responds, the graph is re-entered at `apply_review`. No in-memory checkpointer is needed, so it survives restarts and scale-out. Reviewers can only correct a fixed allow-list of fields. |
| Async jobs | `POST /api/documents` returns `202` with a job id. A queue trigger processes each document and `GET /api/jobs/{id}` is polled for progress. Processing is idempotent, retried up to 5 times, then marked `failed`. |
| PII | KYC document numbers are masked (`****4567`) before they are persisted or indexed. Raw values only exist in memory during processing. The original file stays in your private blob container. |
| Search | Hybrid vector + keyword search with an LLM that converts questions like *"invoices from vendor Acme above 50,000 last quarter"* into OData filters. Model output is allow-listed and escaped. |

## Quick start (no Azure account needed)

```bash
python -m venv .venv && source .venv/bin/activate
make install
make test
make demo        # generates 40 synthetic docs, processes them, reviews one, runs a search
make eval        # accuracy / routing / auto-approval report -> eval_report.json
```

`DOCFLOW_MODE=mock` swaps Document Intelligence, Azure OpenAI, Azure AI Search and the queue for offline stand-ins, so the full workflow, rules and API run locally. Mock mode proves the plumbing and the logic. It does **not** measure real extraction accuracy.

## Run against Azure

1. **Create resources** (free or low tiers are enough): Storage account, Document Intelligence (F0), Azure OpenAI with a chat deployment (e.g. `gpt-4o-mini`) and `text-embedding-3-small`, AI Search (Free or Basic), Function App (Python 3.11, Linux), Logic App (Consumption).
2. **Configure**: copy `local.settings.sample.json` to `local.settings.json`, set `DOCFLOW_MODE` to `azure` and fill in the endpoints. Leave the keys empty to use `az login` / managed identity (grant the identity *Cognitive Services User*, *Search Index Data Contributor*, *Storage Blob/Queue/Table Data Contributor*).
3. **Run locally**: start Azurite, then `func start`.
4. **Deploy**: `func azure functionapp publish <app-name>` and set the same values as app settings.
5. **Logic App**: create the Office 365 connection, then paste `logic_app/workflow.json` into the code view and set `functionBaseUrl`, `functionKey` and `mailbox`. Give the Logic App's managed identity `Mail.ReadWrite` on the mailbox to allow "mark as read". The connection ids and the connector paths depend on your tenant, so treat the file as a template and check it in the designer.
6. **Generate PDFs and measure**: `python scripts/generate_samples.py` then `DOCFLOW_MODE=azure python scripts/evaluate.py --ext pdf`.

## API

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/documents` | `{"documents":[{"filename","content_base64","content_type","source":{...}}]}` → `202 {job_id}` |
| GET | `/api/jobs/{job_id}` | status counts and per-document status |
| GET | `/api/documents/{doc_id}` | fields, confidences, issues, review reasons |
| GET | `/api/review` | documents waiting for a human |
| POST | `/api/review/{doc_id}` | `{"decision":"approve"\|"reject","corrections":{...},"reviewer","comment"}` |
| GET | `/api/search?q=...&top=5` | natural-language search over approved documents |
| GET | `/api/health` | liveness |

## Project layout

```
function_app.py          Azure Functions v2 entry points (HTTP + queue trigger)
docflow/
  workflow.py            LangGraph graph (classify → extract → validate → decide → review)
  validation.py          business rules + LLM sanity check
  routing.py             auto-approve vs. review decision
  extraction.py          Document Intelligence wrapper (+ offline mock)
  classifier.py          keyword-first document classification
  search_index.py        AI Search index, hybrid query, NL → filter parsing
  storage.py             Blob / Table / Queue backends + local file backend
  pipeline.py            submit, process, review, job status
  masking.py             PII masking
  graph_client.py        Microsoft Graph delta-query client
logic_app/workflow.json  Logic App definition (template)
scripts/                 sample generator, evaluation, demo, mailbox backfill
tests/                   unit + pipeline + HTTP tests (all offline)
```

## Design decisions and trade-offs

- **Persisted-state pause instead of a LangGraph checkpointer.** Azure Functions instances are ephemeral and `MemorySaver` would lose paused runs. Persisting the state in the document record keeps the pause durable without extra infrastructure. For richer branching, swap in a Postgres or Cosmos checkpointer.
- **Fail closed.** Unknown type, low confidence, LLM failure and high value all route to a human. Cost of a false review is a minute of someone's time. Cost of a false approval is a wrong payment or a bad KYC decision.
- **Job progress is derived, not counted.** Status is computed from the document records instead of incrementing counters, which avoids races between concurrent queue workers.
- **Table Storage for records.** Cheap and enough for this scale. Lookups by status or job scan one partition. Move to Cosmos DB or SQL if you need richer queries.
- **Prompt-injection hygiene.** The only raw document text sent to a model is for classification, with an enum-constrained output. Validation sees structured fields only. Search parsing output is allow-listed and OData-escaped.

## Known limitations

- Prebuilt models cover standard invoices and common ID formats. Unusual layouts will need a custom model or more review.
- Duplicate detection matches on normalised vendor + invoice number among approved documents only.
- Table `list_docs` queries are single-partition scans. Fine for thousands of documents, not millions.
- Authentication on the API is the Functions key only. Put API Management or Entra ID auth in front for anything beyond a demo.
- Indexing failures are recorded as an `index_failed` warning on the document but not retried automatically.

## Measuring results honestly

Run `scripts/evaluate.py` on your own labelled sample and report what it prints (classification accuracy, routing accuracy, auto-approval rate, field accuracy, seconds per document). The "minutes saved" figure uses an assumption you set with `--manual-minutes`. State that assumption wherever you quote the number.

## License

MIT
