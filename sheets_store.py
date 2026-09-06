"""
sheets_store.py
Stores/retrieves per-page snapshots in a Google Sheet so we can diff
today's scrape against the last one. One row per (university, page_type, url).

Setup required (see README.md):
  1. Create a Google Cloud service account with Sheets API enabled.
  2. Share your target Google Sheet with the service account's email as Editor.
  3. Put the service account JSON key into the GOOGLE_SERVICE_ACCOUNT_JSON secret.
  4. Put the Sheet's ID (from its URL) into the GOOGLE_SHEET_ID secret.
"""

import os
import json
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


def load_previous_snapshot():
    """Returns dict keyed by (university, page_type, url) -> {hash, content, row_index}"""
    ws = _get_worksheet()
    records = ws.get_all_values()
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


def save_snapshot(ws, existing, university, page_type, url, content, timestamp):
    """Insert or update a row for this (university, page_type, url)."""
    key = (university, page_type, url)
    new_hash = content_hash(content)
    # Google Sheets cell limit is ~50,000 chars -- truncate defensively
    safe_content = content[:45000]

    if key in existing:
        row_idx = existing[key]["row"]
        ws.update(f"A{row_idx}:F{row_idx}",
                  [[university, page_type, url, new_hash, timestamp, safe_content]])
    else:
        ws.append_row([university, page_type, url, new_hash, timestamp, safe_content])
