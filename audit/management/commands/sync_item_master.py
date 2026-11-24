from django.core.management.base import BaseCommand

from audit.importers import load_group_substitutions, load_item_master, read_rows
from audit.sync import fetch_tab


class Command(BaseCommand):
    help = (
        "Sync the central item master workbook: '20 Item Master' and the group-wide "
        "'15 Substitutions'. From Google Sheets, or from a downloaded .xlsx."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Downloaded central workbook (.xlsx) instead of Google Sheets")

    def handle(self, *args, file=None, **opts):
        if file:
            items = read_rows(file, sheet="20 Item Master")
            try:
                subs = read_rows(file, sheet="15 Substitutions")
            except KeyError:
                subs = []
        else:
            items = fetch_tab("20 Item Master")
            subs = fetch_tab("15 Substitutions", missing_ok=True)
        for label, report in [("Item master", load_item_master(items)),
                              ("Group substitutions", load_group_substitutions(subs))]:
            self.stdout.write(self.style.SUCCESS(f"{label}: {report}"))
            for w in report.warnings:
                self.stdout.write(f"  ! {w}")
