"""File readers and the full upload -> report flow, using files shaped like the real exports."""
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from audit import importers, services
from audit.models import AuditPeriod, Brand, Item, MenuItem, Outlet, PosAlias, Recipe, RecipeLine, SemiFinished

POS_CSV = """Business Name,WYLD Group
Location Name,EOC_KLCC-O2
Generated Date,12/11/2026
Report Period,01/10/2026

Category / Group,Item,Quantity,Full Name
Latte,Hot,100,Latte - Hot
Latte,Hot,20,Latte - Hot
Espresso,Hot,30,Espresso - Hot
Mystery Cake,Regular Price,2,Mystery Cake - Regular Price
"""

XERO_CSV = """ContactName,InvoiceNumber,Reference,InvoiceDate,Total,InventoryItemCode,Description,Quantity,UnitAmount,LineAmount,AccountCode,TrackingOption1
SUPPLIER A,IV-1,R1,1/9/2026,500,COFFEE,COFFEE BEAN 1KG,2,100,200,70-004,X
SUPPLIER A,IV-1,R1,1/9/2026,500,MILK,FRESH MILK 1L,10,4,40,70-004,X
SUPPLIER A,IV-1,R1,1/9/2026,500,ZZZ999,UNKNOWN THING,1,5,5,70-004,X
SUPPLIER A,IV-1,R1,1/9/2026,500,PPP0249,HANDLING CHARGE (PASTRY BOYS),1,10,10,70-004,X
"""

COUNT_CSV = """SKU,Item Name,Category,Qty 1,Uom 1,Price Per Uom 1,Qty 2,Uom 2,Price Per Uom 2,Qty 3,Uom 3,Price Per Uom 3,Total RM Value,,KLCC,31/10/2026
COFFEE,COFFEE BEAN 1KG,Food & Beverage,1,PKT,100,,0,0.1,,G,0.1,"RM100.00",,Grand Total RM Value,"RM21,505.17"
MILK,FRESH MILK 1L,Food & Beverage,2,L,4,,0,0,,ML,0.004,8.00
"""


def upload(name, text):
    return SimpleUploadedFile(name, text.encode())


class ParsingTests(SimpleTestCase):
    def test_to_decimal(self):
        cases = {"RM1,234.50": "1234.50", "(RM2.00)": "-2.00", "100.00%": "1", "95%": "0.95",
                 "-": "0", "": "0", "#DIV/0!": "0", 12: "12", "-3.5": "-3.5"}
        for raw, want in cases.items():
            self.assertEqual(importers.to_decimal(raw), Decimal(want), raw)

    def test_pos_sales_sums_duplicates(self):
        rows = importers.read_pos_sales(upload("pos.csv", POS_CSV))
        self.assertEqual({r["pos_name"]: r["qty"] for r in rows},
                         {"Latte - Hot": 120, "Espresso - Hot": 30, "Mystery Cake - Regular Price": 2})

    def test_pos_sales_two_column_paste(self):
        rows = importers.read_pos_sales(upload("pos.csv", "Latte - Hot,5\nEspresso - Hot,3\n"))
        self.assertEqual(len(rows), 2)

    def test_pos_sales_multi_outlet_report(self):
        text = (
            "Business Name,WYLD Group\nLocation Name,\"CHNKY_GARDENS-O43, CHNKY_TRX-O36\"\n \n"
            "Item,CHNKY_GARDENS-O43 (Count),CHNKY_GARDENS-O43 (Price),CHNKY_TRX-O36 (Count),CHNKY_TRX-O36 (Price)\n"
            " \n4pcs Cookies In A Box,334,14028,1146,48132\n \nDark Chocolate Chonk,10,100,0,0\n"
        )
        rows = importers.read_pos_sales(upload("p.csv", text), location="CHNKY_TRX-O36")
        self.assertEqual(rows, [{"pos_name": "4pcs Cookies In A Box", "qty": Decimal("1146")}])
        rows = importers.read_pos_sales(upload("p.csv", text), location="chnky_gardens-o43")
        self.assertEqual(len(rows), 2)
        with self.assertRaises(importers.PosLocationError):
            importers.read_pos_sales(upload("p.csv", text))

    def test_xero(self):
        rows = importers.read_xero_invoices(upload("x.csv", XERO_CSV))
        self.assertEqual(rows[0]["sku"], "COFFEE")
        self.assertEqual(rows[0]["value"], Decimal("200"))
        self.assertEqual(str(rows[0]["invoice_date"]), "2026-09-01")

    def test_stock_count(self):
        rows = importers.read_stock_count(upload("c.csv", COUNT_CSV))
        self.assertEqual([(r["sku"], r["value"]) for r in rows], [("COFFEE", Decimal("100.00")), ("MILK", Decimal("8.00"))])

    def test_item_master(self):
        rows = [
            ["SKU", "Item Name", "Category", "Pack Size (base units per pack)", "Base UOM", "Pack UOM",
             "Purchase Price (per pack)", "Cost per Base UOM", "Yield %", "Yield Factor", "Waste %", "Active",
             "Pack Size (base units per intermediate)", "Intermediate UOM", "SUPPLIER 1"],
            ["KFF0008", "BURNT CHEESECAKE", "Food & Beverage", "10", "SLICE", "BOX", "50.61", "5.061",
             "95.00%", "1", "0", "TRUE", "", "", "PASTRY BOYS"],
        ]
        item = importers.parse_item_master(rows)[0]
        self.assertEqual(item["yield_pct"], Decimal("0.95"))
        self.assertEqual(item["purchase_price"], Decimal("50.61"))
        self.assertIsNone(item["intermediate_pack_size"])


class FlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("ceo", "ceo@example.com", "pw")
        self.client.force_login(self.user)
        self.brand = Brand.objects.create(code="EOC", name="Eight Ounce")
        self.outlet = Outlet.objects.create(brand=self.brand, code="KLCC", name="KLCC")
        coffee = Item.objects.create(sku="COFFEE", name="COFFEE BEAN 1KG", pack_size=1000, purchase_price=100, base_uom="G")
        milk = Item.objects.create(sku="MILK", name="FRESH MILK 1L", pack_size=1000, purchase_price=4,
                                   base_uom="ML", yield_pct=Decimal("0.9"))
        sugar = Item.objects.create(sku="SUGAR", name="SUGAR 1KG", pack_size=1000, purchase_price=5, base_uom="G")
        syrup = SemiFinished.objects.create(brand=self.brand, sku="SF-1", name="SYRUP", output_uom="G")
        latte = MenuItem.objects.create(brand=self.brand, sku="M1", name="Latte - Hot", price=15, category="Beverage")
        espresso = MenuItem.objects.create(brand=self.brand, sku="M2", name="Espresso", price=10, category="Beverage")
        PosAlias.objects.create(menu_item=espresso, pos_name="Espresso - Hot")
        r = Recipe.objects.create(brand=self.brand, menu_item=latte)
        RecipeLine.objects.create(recipe=r, item=coffee, qty_per_batch=18)
        RecipeLine.objects.create(recipe=r, item=milk, qty_per_batch=200)
        RecipeLine.objects.create(recipe=r, semi_finished=syrup, qty_per_batch=20)
        r2 = Recipe.objects.create(brand=self.brand, menu_item=espresso)
        RecipeLine.objects.create(recipe=r2, item=coffee, qty_per_batch=18)
        rs = Recipe.objects.create(brand=self.brand, semi_finished=syrup, batch_yield=1000)
        RecipeLine.objects.create(recipe=rs, item=sugar, qty_per_batch=500)
        self.audit = AuditPeriod.objects.create(outlet=self.outlet, period="2026-10")

    def post_file(self, kind, name, text):
        return self.client.post(reverse("audit_upload", args=[self.audit.pk, kind]), {"file": upload(name, text)})

    def test_full_month(self):
        self.post_file("pos", "pos.csv", POS_CSV)
        self.post_file("purchases", "x.csv", XERO_CSV)
        self.post_file("opening", "o.csv", COUNT_CSV)
        self.post_file("closing", "c.csv", COUNT_CSV.replace("RM100.00", "RM50.00"))

        self.assertEqual(self.audit.sales.count(), 3)
        self.assertEqual(self.audit.sales.filter(menu_item__isnull=True).count(), 1)  # Mystery Cake

        result = services.run_audit(self.audit)
        dash = result.dashboard()
        self.assertAlmostEqual(dash["total_sales"], 120 * 15 + 30 * 10)
        # Coffee: (120 + 30) x 18 g x RM0.10
        coffee = next(r for r in result.variance if r.sku == "COFFEE")
        self.assertAlmostEqual(coffee.theoretical_cost, 150 * 18 * 0.1)
        self.assertAlmostEqual(coffee.actual_cost, 100 + 200 - 50)

        issues = {c.key: c.count for c in services.input_issues(self.audit)}
        self.assertEqual(issues["pos_unmatched"], 1)
        self.assertEqual(issues["purchase_unknown"], 1)  # ZZZ999; the handling charge is skipped
        self.assertFalse(self.audit.purchases.filter(sku="PPP0249").exists())

        # Uploading again replaces, not doubles.
        self.post_file("pos", "pos.csv", POS_CSV)
        self.assertEqual(self.audit.sales.count(), 3)

        for name in ["audit_detail", "audit_variance", "audit_menu", "audit_consumption", "audit_inputs"]:
            resp = self.client.get(reverse(name, args=[self.audit.pk]))
            self.assertEqual(resp.status_code, 200, name)
        self.assertContains(self.client.get(reverse("audit_menu", args=[self.audit.pk])), "PLOWHORSE")

        resp = self.client.get(reverse("audit_export", args=[self.audit.pk]))
        wb = openpyxl.load_workbook(BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ["Dashboard", "Variance", "Top leakage", "Menu engineering", "Consumption",
                                         "Checks", "Purchases", "Stock counts"])

        self.assertEqual(self.client.get(reverse("trends")).status_code, 200)
        self.assertEqual(self.client.get(reverse("menu_costing", args=["EOC"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_multi_outlet_report_and_regular_price_names(self):
        MenuItem.objects.create(brand=self.brand, sku="M9", name="8oz Mug - Regular Price", price=58)
        self.outlet.pos_location = "EOC_KLCC-O2"
        self.outlet.save()
        text = ("Item,EOC_GARDENS-O1 (Count),EOC_GARDENS-O1 (Price),EOC_KLCC-O2 (Count),EOC_KLCC-O2 (Price)\n"
                "8oz Mug,1,58,2,116\nLatte - Hot,5,75,7,105\n")
        self.post_file("pos", "pos.csv", text)
        got = {s.menu_item.sku: s.qty for s in self.audit.sales.all()}
        self.assertEqual(got, {"M9": 2, "M1": 7})

    def test_multi_outlet_report_without_location_is_rejected(self):
        text = "Item,A (Count),A (Price),B (Count),B (Price)\nLatte - Hot,5,75,7,105\n"
        resp = self.post_file("pos", "pos.csv", text)
        self.assertEqual(self.audit.sales.count(), 0)
        self.assertContains(self.client.get(reverse("audit_detail", args=[self.audit.pk])), "POS location")

    def test_group_substitutions(self):
        from audit.models import Substitution
        Item.objects.create(sku="OAT", name="OAT MILK", pack_size=1000, purchase_price=20, base_uom="ML")
        rows = [["Original SKU", "Original Name", "Substitute SKU", "Substitute Name",
                 "Conversion Factor (sub units per 1 original)", "Note", "Engine Status"],
                ["MILK", "", "OAT", "", 1.0, "", "ACTIVE"], ["NOPE", "", "OAT", "", 1, "", ""]]
        report = importers.load_group_substitutions(rows)
        self.assertEqual((report.created, report.skipped), (1, 1))
        net = services.run_audit(self.audit).net
        self.assertEqual(net, {})  # no sales yet
        inp = services.brand_input(self.brand)
        self.assertEqual([(s.original_sku, s.substitute_sku) for s in inp.substitutions], [("MILK", "OAT")])
        # A brand's own mapping for the same item wins.
        Substitution.objects.create(brand=self.brand, original=Item.objects.get(sku="MILK"),
                                    substitute=Item.objects.get(sku="SUGAR"), factor=1)
        inp = services.brand_input(self.brand)
        self.assertEqual([(s.original_sku, s.substitute_sku) for s in inp.substitutions], [("MILK", "SUGAR")])

    def test_menu_costing_matches_sheet_definition(self):
        rows = {r["menu"].sku: r for r in services.menu_costing(self.brand)}
        # 18 g x 0.10 + 200 ml x 0.004 + 20 g syrup x (0.5 g sugar x 0.005)
        self.assertAlmostEqual(rows["M1"]["cost"], 1.8 + 0.8 + 0.05)
        self.assertAlmostEqual(rows["M1"]["cogs_pct"], 2.65 / (15 / 1.06))

    def test_every_upload_has_a_template_that_imports(self):
        from audit.views import UPLOAD_TEMPLATES, UPLOADS

        self.assertEqual(set(UPLOAD_TEMPLATES), set(UPLOADS))
        resp = self.client.get(reverse("audit_detail", args=[self.audit.pk]))
        for kind in UPLOADS:
            self.assertContains(resp, reverse("upload_template", args=[kind]))
        sample = {
            "pos": "Latte - Hot,2",
            "purchases": "INV1,2026-10-01,Supplier,COFFEE,Beans,1,100",
            "interco": "INV2,2026-10-02,Sister Co,MILK,Milk,2,8",
            "opening": "COFFEE,Beans,,RM10.00",
            "closing": "COFFEE,Beans,,RM5.00",
        }
        for kind, row in sample.items():
            resp = self.client.get(reverse("upload_template", args=[kind]))
            self.assertEqual(resp["Content-Type"], "text/csv")
            header = resp.content.decode()
            self.assertEqual(header.strip().count("\n"), 0)  # header only, no data rows
            self.post_file(kind, f"{kind}.csv", header + row + "\n")
        self.assertEqual(self.audit.sales.get().qty, 2)
        self.assertEqual(self.audit.purchases.filter(source="supplier").get().value, 100)
        self.assertEqual(self.audit.purchases.filter(source="interco").get().value, 8)
        self.assertEqual(self.audit.stock_counts.get(kind="opening").value, 10)
        self.assertEqual(self.audit.stock_counts.get(kind="closing").value, 5)
        self.assertEqual(self.client.get("/templates/nope.csv").status_code, 404)

    def test_login_required_and_permissions(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("home")).status_code, 302)
        viewer = User.objects.create_user("viewer", password="pw")
        self.client.force_login(viewer)
        resp = self.post_file("pos", "pos.csv", POS_CSV)
        self.assertEqual(resp.status_code, 403)

    def test_locked_audit_rejects_uploads(self):
        self.audit.locked = True
        self.audit.save()
        self.post_file("pos", "pos.csv", POS_CSV)
        self.assertEqual(self.audit.sales.count(), 0)

    def test_xlsx_upload_and_workbook_import(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "23 Menu Master"
        ws.append(["Menu SKU", "Menu Name", "Unit Price", "Menu Category", "Note", "Cost Price", "Cogs %"])
        ws.append(["M3", "Flat White - Regular Price", 15, "Beverage", "", 3.4, 0.24])
        rm = wb.create_sheet("22 Recipe Master")
        rm.append(["Parent SKU", "Parent Name", "Component SKU", "Component Name", "Component Type",
                   "Parent Batch Yield Qty", "Component Qty (per batch)"])
        rm.append(["M3", "Flat White - Regular Price", "COFFEE", "COFFEE BEAN 1KG", "RAW", 1, 18])
        rm.append(["M3", "Flat White - Regular Price", "", "FRESH MILK 1L", "RAW", 1, 150])
        rm.append(["M3", "Flat White - Regular Price", "?", "NOT A THING", "UNMAPPED", 1, 5])
        rm.append(["SF-1", "SYRUP", "SUGAR", "SUGAR 1KG", "RAW", 1000, 500])
        buf = BytesIO()
        wb.save(buf)
        buf.name = "eoc.xlsx"
        buf.seek(0)
        reports = importers.load_workbook_masters(self.brand, buf)
        self.assertEqual(reports["recipes"].created, 3)
        self.assertEqual(reports["recipes"].skipped, 1)
        fw = MenuItem.objects.get(brand=self.brand, sku="M3")
        self.assertEqual(sorted(line.component_sku for line in fw.recipe.lines.all()), ["COFFEE", "MILK"])


class SetupOutletsTests(TestCase):
    def test_outlets_csv(self):
        from django.core.management import call_command
        call_command("setup_outlets", "outlets.csv", stdout=__import__("io").StringIO())
        self.assertEqual(Brand.objects.count(), 7)
        self.assertEqual(Outlet.objects.count(), 14)
        self.assertEqual(Outlet.objects.get(brand__code="EOC", code="KLCC").pos_location, "EOC_KLCC-O2")
        call_command("setup_outlets", "outlets.csv", stdout=__import__("io").StringIO())  # re-run is safe
        self.assertEqual(Outlet.objects.count(), 14)
