import csv

from django.core.management.base import BaseCommand
from django.db import transaction

from audit.models import Brand, Outlet


class Command(BaseCommand):
    help = (
        "Create or update brands and outlets from a CSV (brand_code, brand_name, outlet_code, "
        "outlet_name, pos_location). Safe to re-run; outlets missing from the file are left alone."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", default="outlets.csv")

    @transaction.atomic
    def handle(self, file, **opts):
        with open(file, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                row = {k: (v or "").strip() for k, v in row.items()}
                if not row.get("brand_code") or not row.get("outlet_code"):
                    continue
                brand, _ = Brand.objects.update_or_create(
                    code=row["brand_code"], defaults={"name": row.get("brand_name") or row["brand_code"]}
                )
                defaults = {"name": row.get("outlet_name") or row["outlet_code"]}
                if row.get("pos_location"):
                    defaults["pos_location"] = row["pos_location"]
                outlet, created = Outlet.objects.update_or_create(
                    brand=brand, code=row["outlet_code"], defaults=defaults
                )
                self.stdout.write(f"{'Created' if created else 'Updated'} {outlet}")
