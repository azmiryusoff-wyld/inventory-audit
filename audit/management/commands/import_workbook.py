from django.core.management.base import BaseCommand, CommandError

from audit.importers import load_workbook_masters
from audit.models import Brand


class Command(BaseCommand):
    help = (
        "One-off migration of a brand's existing workbook (File > Download > .xlsx): "
        "semi-finished items, menu, recipes and substitutions. Run sync_item_master first."
    )

    def add_arguments(self, parser):
        parser.add_argument("brand", help="Brand code, e.g. EOC (created if missing)")
        parser.add_argument("file", help="Path to the downloaded .xlsx")
        parser.add_argument("--name", help="Brand name when creating it")

    def handle(self, brand, file, name=None, **opts):
        if not file.lower().endswith(".xlsx"):
            raise CommandError("Download the workbook as .xlsx first")
        b, created = Brand.objects.get_or_create(code=brand, defaults={"name": name or brand})
        if created:
            self.stdout.write(f"Created brand {b.code}")
        for part, report in load_workbook_masters(b, file).items():
            self.stdout.write(self.style.SUCCESS(f"{part}: {report}"))
            for w in report.warnings[:50]:
                self.stdout.write(f"  ! {w}")
            if len(report.warnings) > 50:
                self.stdout.write(f"  ... and {len(report.warnings) - 50} more")
