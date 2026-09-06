"""
sheets_store.py
Stores/retrieves per-page snapshots in a Google Sheet so we can diff
today's scrape against the last one. One row per (university, page_type, url).

Writes are BATCHED (one API call for all new rows, one for all updated rows)
instead of one call per page, because Google Sheets' write-quota is only
60 requests/minute/user -- with 30 universities x multiple pages each,
one-write-per-page blew past that limit. Batching keeps total writes to ~2
calls per run regardless of how many pages are being tracked.

Setup required (see README.md):
  1. Create a Google Cloud service account with Sheets API enabled.
  2. Share your target Google Sheet with the service account's email as Editor.
  3. Put the service account JSON key into the GOOGLE_SERVICE_ACCOUNT_JSON secret.
  4. Put the Sheet's ID (from its URL) into the GOOGLE_SHEET_ID secret.
"""

import os
import json
import time
import hashlib
import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME = "Snapshots"
HEADERS = ["University", "PageType", "URL", "ContentHash", "LastUpdated", "Content"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _client():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_worksheet():
    gc = _client()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)
    return ws


def with_backoff(func, *args, max_retries=6, **kwargs):
    """Retries a Google API call with exponential backoff if we hit a
    rate-limit (429) or transient server error."""
    delay = 5
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (429, 500, 503) and attempt < max_retries - 1:
                print(f"  Google API busy (status {status}), retrying in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise


def load_previous_snapshot():
    """Returns (dict keyed by (university, page_type, url) -> {hash, content, row}, worksheet)."""
    ws = with_backoff(_get_worksheet)
    records = with_backoff(ws.get_all_values)
    snapshot = {}
    for idx, row in enumerate(records[1:], start=2):  # skip header, 1-indexed rows
        if len(row) < 6:
            continue
        university, page_type, url, content_hash, last_updated, content = row[:6]
        snapshot[(university, page_type, url)] = {
            "hash": content_hash,
            "content": content,
            "row": idx,
        }
    return snapshot, ws


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def save_all_snapshots(ws, existing, records):
    """
    records: list of dicts, each {university, page_type, url, content, timestamp}
    Writes everything in at most 2 API calls total: one batched append for
    brand-new rows, one batched update for existing rows that changed.
    """
    new_rows = []
    update_data = []  # list of {'range': 'A5:F5', 'values': [[...]]}

    for rec in records:
        key = (rec["university"], rec["page_type"], rec["url"])
        new_hash = content_hash(rec["content"])
        safe_content = rec["content"][:45000]  # Google Sheets per-cell limit safety margin
        row_values = [rec["university"], rec["page_type"], rec["url"],
                      new_hash, rec["timestamp"], safe_content]

        if key in existing:
            row_idx = existing[key]["row"]
            update_data.append({"range": f"A{row_idx}:F{row_idx}", "values": [row_values]})
        else:
            new_rows.append(row_values)

    if new_rows:
        print(f"Appending {len(new_rows)} new row(s) to the sheet (1 batched call)...")
        with_backoff(ws.append_rows, new_rows)

    if update_data:
        print(f"Updating {len(update_data)} existing row(s) in the sheet (1 batched call)...")
        with_backoff(ws.batch_update, update_data)
