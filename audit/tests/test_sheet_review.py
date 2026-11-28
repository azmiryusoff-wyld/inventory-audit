"""Changes adopted from the chef's improved Tapestry TCK workbook (Nov 2026): B2B sales invoices,
the raw Dinlr export, variance tested as a % of sales, and the top 10 items used less than the recipes say."""
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from audit import engine, importers, services
from audit.models import (
    AuditPeriod, Brand, Item, MenuItem, Outlet, ProductionLine, Recipe, RecipeLine, SaleLine, SemiFinished,
)

B2B = """InvoiceNumber,InvoiceDate,ContactName,InventoryItemCode,Description,Quantity,LineAmount
INV-1,05/10/2026,Eight Ounce Coffee,KFF0156,SPINACH CHICKEN SANDWICH (1 SET X 5 PORTIONS),2,59.60
INV-1,05/10/2026,Eight Ounce Coffee,KFF0156,SPINACH CHICKEN SANDWICH (1 SET X 5 PORTIONS),1,29.80
INV-1,05/10/2026,Eight Ounce Coffee,SUGAR,SUGAR 1KG,3,15.00
INV-1,05/10/2026,Eight Ounce Coffee,,Delivery handling charge,1,20.00
INV-2,06/10/2026,Eight Ounce Coffee,NOPE-1,Something unknown,1,9.00
"""


def dinlr(rows, location="TAP_CHOW_KIT-O47"):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in [["Business Name", "WYLD Group"], ["Location Name", location], ["Generated Date", "6/11/2026"], [],
              ["Item", "1/10/2026 - 31/10/2026 (Count)", "1/10/2026 - 31/10/2026 (Price)"], *rows]:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return SimpleUploadedFile("Item Sales.xlsx", buf.getvalue())


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("chef", "chef@example.com", "pw")
        self.client.force_login(self.user)
        self.brand = Brand.objects.create(code="TAP", name="Tapestry")
        self.outlet = Outlet.objects.create(brand=self.brand, code="TCK", name="Kamunting",
                                            pos_location="TAP_CHOW_KIT-O47")
        self.sugar = Item.objects.create(sku="SUGAR", name="SUGAR 1KG", pack_size=1000, purchase_price=5, base_uom="G")
        # The same SKU is also in the item master as a bought item: the semi-finished one must win.
        Item.objects.create(sku="KFF0156", name="SPINACH CHICKEN SANDWICH (1 SET X 5 PORTIONS)", pack_size=5,
                            purchase_price=30, base_uom="PORTION")
        self.sandwich = SemiFinished.objects.create(brand=self.brand, sku="KFF0156", name="Spinach sandwich",
                                                    output_uom="PORTION")
        r = Recipe.objects.create(brand=self.brand, semi_finished=self.sandwich, batch_yield=5)
        RecipeLine.objects.create(recipe=r, item=self.sugar, qty_per_batch=100)  # 20 g per portion
        self.audit = AuditPeriod.objects.create(outlet=self.outlet, period="2026-10")

    def upload(self, kind, f):
        return self.client.post(reverse("audit_upload", args=[self.audit.pk, kind]), {"file": f})


class B2BSalesTests(Base):
    def test_sets_become_portions_and_amounts_become_revenue(self):
        self.upload("b2b", SimpleUploadedFile("b2b.csv", B2B.encode()))
        lines = list(self.audit.production.filter(kind="b2b").order_by("pk"))
        self.assertEqual(len(lines), 3)  # handling charge skipped, unknown SKU left out
        self.assertEqual((lines[0].semi_finished, lines[0].item, lines[0].invoice_qty, lines[0].qty),
                         (self.sandwich, None, 2, 10))
        self.assertEqual((lines[2].item, lines[2].qty), (self.sugar, 3000))  # 3 packs x 1,000 g
        result = services.run_audit(self.audit)
        self.assertAlmostEqual(result.b2b_sales, 104.40)
        self.assertAlmostEqual(result.dashboard()["total_sales"], 104.40)
        # 15 portions x 20 g sugar + 3,000 g sugar sold as is = 3,300 g at RM 0.005
        self.assertAlmostEqual(result.theoretical(), 16.50)

    def test_semi_finished_code_with_a_space_still_wins(self):
        self.sandwich.sku = "KFF 0156"
        self.sandwich.save()
        self.upload("b2b", SimpleUploadedFile("b2b.csv", B2B.encode()))
        line = self.audit.production.filter(kind="b2b").order_by("pk").first()
        self.assertEqual((line.semi_finished, line.item, line.qty), (self.sandwich, None, 10))

    def test_upload_replaces_only_b2b_lines(self):
        ProductionLine.objects.create(audit=self.audit, kind="wastage", item=self.sugar, qty=50)
        self.upload("b2b", SimpleUploadedFile("b2b.csv", B2B.encode()))
        self.upload("b2b", SimpleUploadedFile("b2b.csv", B2B.encode()))
        self.assertEqual(self.audit.production.filter(kind="b2b").count(), 3)
        self.assertEqual(self.audit.production.filter(kind="wastage").count(), 1)
        self.assertEqual(services.input_counts(self.audit)["b2b"], 3)

    def test_warns_about_unknown_sku(self):
        report = importers.load_b2b_sales(self.audit, SimpleUploadedFile("b2b.csv", B2B.encode()))
        self.assertEqual(report.skipped, 1)
        self.assertTrue(any("NOPE-1" in w for w in report.warnings))
        self.assertTrue(any("9.00 of B2B sales" in w for w in report.warnings))

    def test_snapshot_keeps_b2b_revenue(self):
        self.upload("b2b", SimpleUploadedFile("b2b.csv", B2B.encode()))
        services.sign_off(self.audit, self.user)
        self.audit.refresh_from_db()
        self.audit.production.all().delete()
        self.assertAlmostEqual(services.run_audit(self.audit).b2b_sales, 104.40)

    def test_template_and_manual_form(self):
        self.assertIn("InventoryItemCode", self.client.get(reverse("upload_template", args=["b2b"])).content.decode())
        page = self.client.get(reverse("audit_line_new", args=[self.audit.pk, "production"])).content.decode()
        self.assertNotIn('value="b2b"', page)


class DinlrTests(Base):
    def test_variants_are_named_parent_dash_variant(self):
        rows = importers.read_pos_sales(dinlr([
            ["Americano", 30, 300], ["Cold", 20, 200], ["Hot", 10, 100], [" "],
            ["Angus Beef Ragu", 5, 210], [" "],
            ["Total", 35, 510],
        ]), location="TAP_CHOW_KIT-O47")
        self.assertEqual({r["pos_name"]: r["qty"] for r in rows},
                         {"Americano - Cold": 20, "Americano - Hot": 10, "Angus Beef Ragu": 5})

    def test_other_location_is_refused(self):
        with self.assertRaises(importers.PosLocationError):
            importers.read_pos_sales(dinlr([["Latte", 1, 10]], location="TAP_GARDENS-O44"),
                                     location="TAP_CHOW_KIT-O47")

    def test_two_locations_are_refused(self):
        with self.assertRaises(importers.PosLocationError):
            importers.read_pos_sales(dinlr([["Latte", 1, 10]], location="TAP_CHOW_KIT-O47, TAP_GARDENS-O44"))

    def test_upload_matches_menu(self):
        MenuItem.objects.create(brand=self.brand, sku="TDB 050", name="Americano - Hot", price=12)
        self.upload("pos", dinlr([["Americano", 4, 48], ["Hot", 4, 48], [" "]]))
        line = SaleLine.objects.get(audit=self.audit)
        self.assertEqual((line.pos_name, line.qty, line.menu_item.sku), ("Americano - Hot", 4, "TDB 050"))


class ToleranceOfSalesTests(TestCase):
    def dash(self, variance, sales, theo=1000.0):
        return {"variance_fb": variance, "total_sales": sales, "theoretical_fb": theo}

    def test_tested_on_sales_not_theoretical(self):
        # RM 50 on RM 1,000 theoretical is 5% of theoretical, but only 1% of RM 5,000 sales.
        self.assertAlmostEqual(services.variance_pct_of_sales(self.dash(50, 5000)), 0.01)
        self.assertFalse(services.variance_outside(self.dash(50, 5000), 0.02))
        self.assertTrue(services.variance_outside(self.dash(-150, 5000), 0.02))

    def test_no_sales_any_variance_is_outside(self):
        self.assertTrue(services.variance_outside(self.dash(5, 0), 0.02))
        self.assertFalse(services.variance_outside(self.dash(0, 0), 0.02))


class TopGainsTests(TestCase):
    def test_ten_gains(self):
        items = {f"I{n}": engine.Item(f"I{n}", f"Item {n}", "Food & Beverage", 1.0) for n in range(12)}
        inp = engine.EngineInput(items=items, recipes={}, opening_value={k: 0.0 for k in items},
                                 closing_value={k: float(n + 1) for n, k in enumerate(items)})
        dash = engine.run(inp).dashboard()
        self.assertEqual(len(dash["top_gains"]), 10)
        self.assertEqual(dash["top_gains"][0]["sku"], "I11")


class ReportExtrasTests(TestCase):
    """Items 5-11 of the review: gross leakage vs offsets, top-10 breakdowns, non-recipe categories,
    three new data checks and the menu engineering H/L tags."""

    def setUp(self):
        fb = "Food & Beverage"
        self.inp = engine.EngineInput(
            items={
                "BEAN": engine.Item("BEAN", "Coffee bean", fb, 0.1),
                "PEA": engine.Item("PEA", "Blue pea flower", fb, 1.0),
                "CHICKEN": engine.Item("CHICKEN", "Chicken whole", fb, 1.0),
                "WINE": engine.Item("WINE", "Sauvignon blanc", fb, 1.0),
                "BOX": engine.Item("BOX", "Takeaway box", "Packaging", 1.0),
            },
            recipes={
                "LATTE": engine.Recipe("LATTE", 1, (engine.RecipeLine("BEAN", 18), engine.RecipeLine("PEA", 1))),
                "GLASS": engine.Recipe("GLASS", 1, (engine.RecipeLine("WINE", 100),)),
            },
            menu={"LATTE": engine.MenuItem("LATTE", "Latte", 12, "Beverage"),
                  "GLASS": engine.MenuItem("GLASS", "Glass of wine", 30, "Beverage")},
            demand={"LATTE": 100, "GLASS": 2}, sold={"LATTE": 100, "GLASS": 2},
            opening_value={"BEAN": 100, "PEA": 5},
            purchases_value={"BEAN": 300, "CHICKEN": 400, "BOX": 80},
            closing_value={"BEAN": 50, "PEA": 900},
        )
        self.result = engine.run(self.inp)
        self.dash = self.result.dashboard()

    def test_gross_leakage_and_offsets_add_up_to_net(self):
        # BEAN: actual 350 - theo 180 = +170; CHICKEN +400; PEA: -895 - 100 = -995; WINE: 0 - 200 = -200
        self.assertAlmostEqual(self.dash["leakage_total"], 570)
        self.assertAlmostEqual(self.dash["offsets_total"], -1195)
        self.assertAlmostEqual(self.dash["leakage_total"] + self.dash["offsets_total"], self.dash["variance_fb"])

    def test_top_lists_carry_the_breakdown(self):
        bean = next(r for r in self.dash["top_leakage"] if r["sku"] == "BEAN")
        self.assertEqual((bean["opening"], bean["purchases"], bean["closing"], bean["actual"]), (100, 300, 50, 350))
        self.assertAlmostEqual(bean["theoretical"], 180)
        pea = self.dash["top_gains"][0]
        self.assertEqual(pea["sku"], "PEA")
        self.assertAlmostEqual(pea["share"], 995 / 1195)

    def test_non_recipe_categories(self):
        self.assertEqual([c["category"] for c in self.dash["non_recipe"]][:1], ["Packaging"])
        self.assertAlmostEqual(self.dash["non_recipe"][0]["actual"], 80)

    def test_new_checks(self):
        checks = {c.key: c for c in self.result.checks}
        self.assertEqual(checks["negative_actual"].count, 1)
        self.assertIn("PEA", checks["negative_actual"].details[0])
        self.assertEqual(checks["used_no_recipe"].details, ["CHICKEN Chicken whole: actual 400.00"])
        self.assertEqual(checks["never_bought"].details, ["WINE Sauvignon blanc: theoretical 200.00"])

    def test_menu_tags(self):
        latte = next(r for r in self.result.menu if r.sku == "LATTE")
        glass = next(r for r in self.result.menu if r.sku == "GLASS")
        self.assertEqual((latte.popularity_category, latte.profit_category), ("H", "H"))
        self.assertEqual((glass.popularity_category, glass.profit_category), ("L", "L"))
        self.assertAlmostEqual(latte.net_after_cost, latte.total_contribution - latte.total_cost)
