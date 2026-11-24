"""Read the central item master workbook straight from Google Sheets.

Uses a Google service account (no user login): create one in Google Cloud,
download its JSON key, share the central workbook with the service account's
email (Viewer is enough), then set GOOGLE_SERVICE_ACCOUNT_FILE and
ITEM_MASTER_SHEET_ID. Requires the optional ``gspread`` package.
"""
from django.conf import settings


def fetch_tab(tab: str, sheet_id: str | None = None, missing_ok: bool = False) -> list[list]:
    try:
        import gspread
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Install gspread to sync from Google Sheets: pip install gspread") from exc
    sheet_id = sheet_id or settings.ITEM_MASTER_SHEET_ID
    if not sheet_id or not settings.GOOGLE_SERVICE_ACCOUNT_FILE:
        raise RuntimeError("Set ITEM_MASTER_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE first")
    client = gspread.service_account(filename=settings.GOOGLE_SERVICE_ACCOUNT_FILE)
    try:
        ws = client.open_by_key(sheet_id).worksheet(tab)
    except gspread.WorksheetNotFound:
        if missing_ok:
            return []
        raise
    # UNFORMATTED keeps numbers as numbers (no 'RM' or thousands separators).
    return ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
