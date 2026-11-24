from django.core.management.base import BaseCommand

from audit.importers import load_item_master, read_rows
from audit.sync import fetch_item_master_rows


class Command(BaseCommand):
    help = "Sync items from the central item master (Google Sheets, or a downloaded .xlsx/.csv)."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Downloaded item master (.xlsx or .csv) instead of Google Sheets")
        parser.add_argument("--tab", default="20 Item Master", help="Tab name inside an .xlsx")

    def handle(self, *args, file=None, tab=None, **opts):
        if file:
            rows = read_rows(file, sheet=tab if file.lower().endswith(".xlsx") else None)
        else:
            rows = fetch_item_master_rows()
        report = load_item_master(rows)
        self.stdout.write(self.style.SUCCESS(f"Item master: {report}"))
        for w in report.warnings:
            self.stdout.write(f"  ! {w}")
