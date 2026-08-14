import json
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "contactos")

_worksheet = None


def _get_worksheet():
    global _worksheet
    if _worksheet is None:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        client = gspread.authorize(creds)
        _worksheet = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
    return _worksheet


def list_contacts_by_status(status):
    ws = _get_worksheet()
    rows = ws.get_all_records()
    return [
        {**row, "row_number": i + 2}
        for i, row in enumerate(rows)
        if row["status"] == status
    ]


def get_contact_by_phone(phone):
    ws = _get_worksheet()
    rows = ws.get_all_records()
    for i, row in enumerate(rows):
        if str(row["telefono"]) == phone:
            return {**row, "row_number": i + 2}
    return None


def update_status(row_number, status):
    ws = _get_worksheet()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ws.update(range_name=f"C{row_number}:D{row_number}", values=[[status, now]])


def count_by_status():
    ws = _get_worksheet()
    rows = ws.get_all_records()
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts
