"""Semi-finished items counted in stock are broken down into the items they were made from,
and a semi-finished item can't exist without a recipe and batch yield."""
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from audit import importers, services
from audit.models import AuditPeriod, Brand, Item, MenuItem, Outlet, Recipe, RecipeLine, SemiFinished, StockCount

COUNT = """SKU,Item Name,Counted Qty (packs),Total RM Value,Semi-finished Qty
SUGAR,SUGAR 1KG,,2.00,
SF-1,Caramel syrup,,,2000
"""


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("chef", "chef@example.com", "pw")
        self.client.force_login(self.user)
        self.brand = Brand.objects.create(code="EOC", name="Eight Ounce")
        self.outlet = Outlet.objects.create(brand=self.brand, code="KLCC", name="KLCC")
        self.sugar = Item.objects.create(sku="SUGAR", name="SUGAR 1KG", pack_size=1000, purchase_price=5, base_uom="G")
        self.milk = Item.objects.create(sku="MILK", name="FRESH MILK 1L", pack_size=1000, purchase_price=4,
                                        base_uom="ML", yield_pct=Decimal("0.9"))
        self.syrup = SemiFinished.objects.create(brand=self.brand, sku="SF-1", name="Caramel syrup", output_uom="G")
        r = Recipe.objects.create(brand=self.brand, semi_finished=self.syrup, batch_yield=1000)
        RecipeLine.objects.create(recipe=r, item=self.sugar, qty_per_batch=500)
        self.audit = AuditPeriod.objects.create(outlet=self.outlet, period="2026-10")

    def post_count(self, kind, text, audit=None):
        audit = audit or self.audit
        return self.client.post(reverse("audit_upload", args=[audit.pk, kind]),
                                {"file": SimpleUploadedFile("c.csv", text.encode())})


class StockCountTests(Base):
    def test_upload_breaks_sf_into_ingredients(self):
        self.post_count("closing", COUNT)
        line = self.audit.stock_counts.get(sku="SF-1")
        self.assertEqual((line.semi_finished, line.qty, line.value, line.item), (self.syrup, 2000, 0, None))
        sugar = next(r for r in services.run_audit(self.audit).variance if r.sku == "SUGAR")
        # 2,000 g syrup = 1,000 g sugar at RM 0.005 = RM 5.00, on top of the RM 2.00 counted as sugar.
        self.assertAlmostEqual(sugar.closing_sf, 5.0)
        self.assertAlmostEqual(sugar.closing, 7.0)
        self.assertEqual({c.key: c.count for c in services.input_issues(self.audit)}["count_unknown"], 0)

    def test_sf_counted_by_value_is_refused(self):
        resp = self.post_count("closing", COUNT.replace(",,,2000", ",,10.00,"))
        self.assertFalse(self.audit.stock_counts.exists())
        self.assertContains(self.client.get(reverse("audit_detail", args=[self.audit.pk])),
                            "semi-finished item made in-house")
        self.assertEqual(resp.status_code, 302)

    def test_sf_qty_on_a_supplier_item_is_refused(self):
        self.post_count("closing", COUNT.replace("SUGAR,SUGAR 1KG,,2.00,", "SUGAR,SUGAR 1KG,,,5"))
        self.assertFalse(self.audit.stock_counts.exists())

    def test_both_value_and_sf_qty_is_refused(self):
        with self.assertRaises(importers.RowErrors):
            importers.read_stock_count(SimpleUploadedFile("c.csv", COUNT.replace(",,,2000", ",,1.00,2000").encode()))

    def test_template_has_sf_column(self):
        resp = self.client.get(reverse("upload_template", args=["closing"]))
        self.assertIn("Semi-finished Qty", resp.content.decode())

    def test_opening_copied_from_last_closing_keeps_sf(self):
        self.post_count("closing", COUNT)
        nov = AuditPeriod.objects.create(outlet=self.outlet, period="2026-11")
        self.assertEqual(services.copy_previous_closing(nov), 2)
        line = nov.stock_counts.get(sku="SF-1")
        self.assertEqual((line.kind, line.semi_finished, line.qty), ("opening", self.syrup, 2000))
        sugar = next(r for r in services.run_audit(nov).variance if r.sku == "SUGAR")
        self.assertAlmostEqual(sugar.opening_sf, 5.0)
        self.assertEqual(services.opening_vs_previous_closing(nov), [])
        line.qty = 1500
        line.save()
        self.assertEqual(len(services.opening_vs_previous_closing(nov)), 1)

    def test_sign_off_snapshot_keeps_sf_counts(self):
        self.post_count("closing", COUNT)
        services.sign_off(self.audit, self.user)
        StockCount.objects.filter(sku="SF-1").update(qty=0)  # later edits don't restate a signed-off month
        self.audit.refresh_from_db()
        sugar = next(r for r in services.run_audit(self.audit).variance if r.sku == "SUGAR")
        self.assertAlmostEqual(sugar.closing_sf, 5.0)

    def test_old_snapshot_without_sf_still_loads(self):
        services.sign_off(self.audit, self.user)
        self.audit.refresh_from_db()
        snap = self.audit.snapshot
        del snap["engine_input"]["opening_sf_qty"], snap["engine_input"]["closing_sf_qty"]
        self.audit.snapshot = snap
        self.audit.save()
        services.run_audit(self.audit)

    def test_line_form_adds_sf_count(self):
        url = reverse("audit_line_new", args=[self.audit.pk, "count"])
        resp = self.client.post(url, {"kind": "closing", "sku": "sf-1", "description": "", "value": "", "qty": "750"})
        self.assertEqual(resp.status_code, 302)
        line = self.audit.stock_counts.get()
        self.assertEqual((line.semi_finished, line.qty, line.description), (self.syrup, 750, "Caramel syrup"))
        resp = self.client.post(url, {"kind": "closing", "sku": "SF-1", "description": "", "value": "3", "qty": ""})
        self.assertEqual(resp.status_code, 200)  # a semi-finished item needs a weight, not RM

    def test_screens_show_sf_part(self):
        self.post_count("closing", COUNT)
        self.assertContains(self.client.get(reverse("audit_variance", args=[self.audit.pk])), "in SF")
        self.assertContains(self.client.get(reverse("audit_inputs", args=[self.audit.pk])), "semi-finished")
        self.assertEqual(self.client.get(reverse("audit_export", args=[self.audit.pk])).status_code, 200)


class RecipeMandatoryTests(Base):
    def test_new_sf_needs_recipe_lines_and_yield(self):
        url = reverse("data_sf_new", args=[self.brand.code])
        base = {"sku": "SF-2", "name": "Dough", "output_uom": "G", "active": "on", "note": "",
                "lines-TOTAL_FORMS": "3", "lines-INITIAL_FORMS": "0"}
        self.client.post(url, {**base, "batch_yield": "1000"})  # no lines
        self.client.post(url, {**base, "batch_yield": "", "lines-0-component": "SUGAR", "lines-0-qty": "100"})
        self.assertFalse(SemiFinished.objects.filter(sku="SF-2").exists())
        resp = self.client.post(url, {**base, "batch_yield": "1000", "lines-0-component": "SUGAR", "lines-0-qty": "100"})
        self.assertEqual(resp.status_code, 302)
        sf = SemiFinished.objects.get(sku="SF-2")
        self.assertEqual((sf.recipe.batch_yield, sf.recipe.lines.count()), (1000, 1))

    def test_sf_recipe_cannot_be_deleted_or_moved(self):
        recipe = self.syrup.recipe
        self.client.post(reverse("data_recipe_delete", args=[self.brand.code, recipe.pk]))
        self.assertTrue(Recipe.objects.filter(pk=recipe.pk).exists())
        menu = MenuItem.objects.create(brand=self.brand, sku="M1", name="Latte", price=15)
        self.client.post(reverse("data_recipe_edit", args=[self.brand.code, recipe.pk]), {
            "parent": f"menu:{menu.pk}", "batch_yield": "1", "note": "",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-0-component": "SUGAR", "lines-0-qty": "5"})
        recipe.refresh_from_db()
        self.assertEqual(recipe.semi_finished, self.syrup)

    def test_sf_without_recipe_cannot_be_counted(self):
        bare = SemiFinished.objects.create(brand=self.brand, sku="SF-9", name="Legacy", output_uom="G")
        self.post_count("closing", COUNT.replace("SF-1,Caramel syrup", f"{bare.sku},Legacy"))
        self.assertFalse(self.audit.stock_counts.exists())

    def test_workbook_import_refuses_sf_without_recipe_or_yield(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "21 SF Master"
        ws.append(["SF SKU", "SF Name", "Output UOM"])
        ws.append(["SF-1", "Caramel syrup", "G"])
        ws.append(["SF-3", "Ganache", "G"])
        rm = wb.create_sheet("22 Recipe Master")
        rm.append(["Parent SKU", "Parent Name", "Component SKU", "Component Name", "Component Type",
                   "Parent Batch Yield Qty", "Component Qty (per batch)"])
        rm.append(["SF-1", "Caramel syrup", "SUGAR", "SUGAR 1KG", "RAW", "", 500])  # no batch yield
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        with self.assertRaisesMessage(ValueError, "SF-1: no batch yield; SF-3: no recipe lines"):
            importers.load_workbook_masters(self.brand, buf)
        self.assertTrue(Recipe.objects.filter(semi_finished=self.syrup).exists())  # nothing changed
        self.assertFalse(SemiFinished.objects.filter(sku="SF-3").exists())
