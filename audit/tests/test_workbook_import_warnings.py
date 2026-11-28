"""Warnings the brand-workbook import gives for problems found in the GNP workbook (Nov 2026)."""
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.test import TestCase

from audit import importers
from audit.models import Brand, Item, MenuItem


def workbook(recipe_rows, menu_rows, sf_rows=()):
    wb = openpyxl.Workbook()
    sf = wb.active
    sf.title = "21 SF Master"
    sf.append(["SF SKU", "SF Name", "Output UOM"])
    for r in sf_rows:
        sf.append(list(r))
    mm = wb.create_sheet("23 Menu Master")
    mm.append(["Menu SKU", "Menu Name", "Unit Price", "Menu Category"])
    for r in menu_rows:
        mm.append(list(r))
    rm = wb.create_sheet("22 Recipe Master")
    rm.append(["Parent SKU", "Parent Name", "Component SKU", "Component Name", "Component Type",
               "Parent Batch Yield Qty", "Component Qty (per batch)"])
    for r in recipe_rows:
        rm.append(list(r))
    buf = BytesIO()
    wb.save(buf)
    buf.name = "gnp.xlsx"
    buf.seek(0)
    return buf


class WorkbookImportWarningsTests(TestCase):
    def setUp(self):
        self.brand = Brand.objects.create(code="GNP", name="Good News Pizza")
        Item.objects.create(sku="FLOUR", name="PIZZA FLOUR 25KG", pack_size=25000, purchase_price=100, base_uom="G")
        Item.objects.create(sku="GNP-HM13", name="GNP-Aioli", category="Semi Finished",
                            pack_size=1000, purchase_price=Decimal("15.84"), base_uom="G")
        Item.objects.create(sku="GNP-HM39", name="GNP-Mixed Mushroom", category="Semi Finished", pack_size=0)

    def test_unknown_parent_is_named_once(self):
        buf = workbook(
            [["?", "Kids' Marinara - Regular Price", "FLOUR", "PIZZA FLOUR 25KG", "RAW", 1, 200],
             ["?", "Kids' Marinara - Regular Price", "FLOUR", "PIZZA FLOUR 25KG", "RAW", 1, 10]],
            [["P1", "Margherita - Regular Price", 30, "Food"]])
        rep = importers.load_workbook_masters(self.brand, buf)["recipes"]
        self.assertEqual(rep.skipped, 2)
        self.assertEqual(rep.warnings, [
            "Parent not found (not in 23 Menu Master or 21 SF Master): Kids' Marinara - Regular Price"])

    def test_semi_finished_item_without_brand_recipe_is_reported(self):
        buf = workbook(
            [["P1", "Wedges - Regular Price", "?", "GNP-Aioli", "UNMAPPED", 1, 30],
             ["P2", "Squid - Regular Price", "?", "GNP-Aioli", "UNMAPPED", 1, 30],
             ["P2", "Squid - Regular Price", "?", "GNP-Mixed Mushroom", "UNMAPPED", 1, 20]],
            [["P1", "Wedges - Regular Price", 20, "Food"], ["P2", "Squid - Regular Price", 30, "Food"]])
        rep = importers.load_workbook_masters(self.brand, buf)["recipes"]
        self.assertEqual(rep.created, 3)
        text = "\n".join(rep.warnings)
        self.assertIn("GNP-HM13 GNP-Aioli is a semi-finished item in the item master but has no recipe", text)
        self.assertIn("RM15.84 per pack", text)
        self.assertIn("Used in 2 recipe(s)", text)
        self.assertIn("GNP-HM39 GNP-Mixed Mushroom", text)
        self.assertIn("no price, so RM0", text)

    def test_large_menu_batch_yield_is_reported(self):
        buf = workbook(
            [["P1", "Salsiccia - Regular Price", "FLOUR", "PIZZA FLOUR 25KG", "RAW", 4500, 70],
             ["P2", "Cookie", "FLOUR", "PIZZA FLOUR 25KG", "RAW", 40, 2000]],
            [["P1", "Salsiccia - Regular Price", 53, "Food"], ["P2", "Cookie", 8, "Food"]])
        rep = importers.load_workbook_masters(self.brand, buf)["recipes"]
        self.assertEqual(len(rep.warnings), 1)
        self.assertIn("P1 Salsiccia - Regular Price: batch yield 4500 portions", rep.warnings[0])
        self.assertEqual(MenuItem.objects.get(sku="P1").recipe.batch_yield, 4500)
