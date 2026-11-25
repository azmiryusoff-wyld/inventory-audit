"""Sign-off, upload safety, status and permissions (findings C-01 to C-14 of the usability audit)."""
from decimal import Decimal
from io import BytesIO, StringIO

import openpyxl
from django.contrib.auth.models import Group, Permission, User
from django.core.management import call_command
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from audit import importers, services
from audit.models import AuditPeriod, Item, MenuItem, RecipeLine

from .test_importers_and_views import COUNT_CSV, POS_CSV, XERO_CSV, FlowTests, upload

XERO_OCT = XERO_CSV.replace("1/9/2026", "1/10/2026")


class Base(TestCase):
    """The same brand, recipes and October audit as FlowTests, without re-running its tests."""

    setUp = FlowTests.setUp
    post_file = FlowTests.post_file


class ParseNumberTests(SimpleTestCase):
    def test_accepted(self):
        cases = {"12": "12", "1,234.50": "1234.50", "RM 1,234.50": "1234.50", "(2.00)": "-2.00",
                 "-3": "-3", "95%": "0.95", 7: "7", 1.5: "1.5", "RM-5": "-5", ".5": "0.5"}
        for raw, want in cases.items():
            self.assertEqual(importers.parse_number(raw), Decimal(want), raw)
        for blank in ("", "  ", "-", None):
            self.assertIsNone(importers.parse_number(blank))

    def test_rejected(self):
        for raw in ("abc", "two", "12,5", "1.234,50", "1e3", "5.00-", "1,23", "12abc"):
            with self.assertRaises(ValueError, msg=raw):
                importers.parse_number(raw)


class UploadSafetyTests(Base):
    """C-05 to C-09: bad files change nothing, bad rows are named, odd lines are reported."""

    def messages(self, resp):
        return [str(m) for m in resp.context["messages"]] if resp.context else []

    def post(self, kind, name, text):
        return self.client.post(reverse("audit_upload", args=[self.audit.pk, kind]),
                                {"file": upload(name, text)}, follow=True)

    def test_empty_or_unreadable_file_keeps_existing_lines(self):
        self.post("closing", "c.csv", COUNT_CSV)
        self.assertEqual(self.audit.stock_counts.filter(kind="closing").count(), 2)
        resp = self.post("closing", "c.csv", "SKU,Item Name,Total RM Value\n")
        self.assertIn("nothing was changed", " ".join(self.messages(resp)))
        self.assertEqual(self.audit.stock_counts.filter(kind="closing").count(), 2)

        self.post("pos", "pos.csv", POS_CSV)
        resp = self.post("pos", "notes.txt", "Remember to order milk on Monday.\nCall the supplier.\n")
        self.assertEqual(self.audit.sales.count(), 3)
        self.assertTrue(any("Could not find the sales columns" in m for m in self.messages(resp)))

    def test_file_in_the_wrong_slot_is_refused(self):
        resp = self.post("pos", "count.csv", COUNT_CSV)
        self.assertEqual(self.audit.sales.count(), 0)
        self.assertTrue(any("looks like a stock count" in m for m in self.messages(resp)))
        resp = self.post("pos", "x.csv", XERO_CSV)
        self.assertTrue(any("looks like a Xero invoice export" in m for m in self.messages(resp)))

    def test_corrupt_xlsx_and_pdf_show_a_message_not_a_crash(self):
        resp = self.post("closing", "fake.xlsx", "this is not a spreadsheet")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any("can't be opened as an Excel workbook" in m for m in self.messages(resp)))
        resp = self.post("closing", "count.pdf", "%PDF-1.4")
        self.assertTrue(any("isn't an Excel (.xlsx) or CSV file" in m for m in self.messages(resp)))

    def test_bad_values_stop_the_upload_with_row_numbers(self):
        bad = XERO_OCT.replace(",2,100,200,", ",two,100,200,").replace("1/10/2026,500,MILK", "31/11/2026,500,MILK")
        resp = self.post("purchases", "x.csv", bad)
        text = " ".join(self.messages(resp))
        self.assertIn("Nothing was loaded", text)
        self.assertIn("Row 2: Quantity 'two' is not a number", text)
        self.assertIn("Row 3: InvoiceDate", text)
        self.assertEqual(self.audit.purchases.count(), 0)

        resp = self.post("closing", "c.csv", "SKU,Item Name,Total RM Value\nCOFFEE,Beans,abc\nMILK,Milk,\nSUGAR,Sugar,-14\n")
        text = " ".join(self.messages(resp))
        self.assertIn("Row 2: Total RM Value 'abc' is not a number", text)
        self.assertIn("Row 3: Total RM Value is blank", text)
        self.assertIn("Row 4: Total RM Value can't be negative", text)
        self.assertEqual(self.audit.stock_counts.count(), 0)

        resp = self.post("pos", "pos.csv", "Full Name,Quantity\nLatte - Hot,ten\n")
        self.assertIn("Row 2: quantity 'ten' is not a number", " ".join(self.messages(resp)))

    def test_lines_without_a_code_are_reported_with_their_value(self):
        text = XERO_OCT + "SUPPLIER A,IV-2,R1,2/10/2026,390,,Full cream milk 1L,60,6.5,390,70-004,X\n"
        resp = self.post("purchases", "x.csv", text)
        warning = " ".join(self.messages(resp))
        self.assertIn("1 line with no InventoryItemCode not loaded (390.00 in total)", warning)
        self.assertIn("Full cream milk 1L 390.00", warning)
        self.assertIn("1 handling-charge line skipped", warning)

    def test_duplicates_and_out_of_period_invoices_are_flagged(self):
        dup = XERO_CSV.splitlines()[1]
        text = XERO_OCT + dup.replace("1/9/2026", "1/10/2026") + "\n" + dup + "\n"
        resp = self.post("purchases", "x.csv", text)
        warning = " ".join(self.messages(resp))
        self.assertIn("Possible duplicate: IV-1 COFFEE 200.00 appears 2 times", warning)
        self.assertIn("dated outside 2026-10", warning)
        issues = {c.key: c for c in services.input_issues(self.audit)}
        self.assertEqual(issues["purchase_duplicate"].count, 1)
        self.assertEqual(issues["purchase_period"].count, 1)
        self.assertIn("01/09/2026", issues["purchase_period"].details[0])

    def test_pos_total_rows_and_location_mismatch(self):
        self.post("pos", "pos.csv", "Full Name,Quantity\nLatte - Hot,5\nTotal,5\n")
        self.assertEqual(list(self.audit.sales.values_list("pos_name", flat=True)), ["Latte - Hot"])

        self.outlet.pos_location = "EOC_GARDENS-O1"
        self.outlet.save()
        text = "Item,EOC_KLCC-O2 (Count),EOC_KLCC-O2 (Price)\nLatte - Hot,500,7500\n"
        resp = self.post("pos", "pos.csv", text)
        self.assertTrue(any("no column for this outlet's POS location" in m for m in self.messages(resp)))
        self.assertEqual(self.audit.sales.get().qty, 5)  # unchanged

        # A multi-column file without "(Count)" headings is not guessed at.
        text = "Item,EOC_KLCC-O2,EOC_GARDENS-O1,Total\nLatte - Hot,500,120,620\n"
        resp = self.post("pos", "pos.csv", text)
        self.assertTrue(any("Could not find the sales columns" in m for m in self.messages(resp)))

    def test_semicolon_csv(self):
        self.post("pos", "pos.csv", "Full Name;Quantity\nLatte - Hot;4\n")
        self.assertEqual(self.audit.sales.get().qty, 4)

    def test_duplicate_stock_rows_are_summed_and_mentioned(self):
        resp = self.post("closing", "c.csv", "SKU,Item Name,Total RM Value\nCOFFEE,Beans store,100\nCOFFEE,Beans bar,150\n")
        self.assertEqual(self.audit.stock_counts.count(), 2)
        self.assertIn("COFFEE is on 2 rows", " ".join(self.messages(resp)))


class StatusTests(Base):
    """C-10, C-11: one status, with tolerance and completeness, the same everywhere."""

    def status(self):
        return services.audit_report(AuditPeriod.objects.get(pk=self.audit.pk))[2]

    def load_month(self, closing_coffee="RM50.00"):
        self.post_file("pos", "pos.csv", "Full Name,Quantity\nLatte - Hot,120\nEspresso - Hot,30\n")
        self.post_file("purchases", "x.csv", "\n".join(XERO_OCT.splitlines()[:3]) + "\n")
        self.post_file("opening", "o.csv", COUNT_CSV)
        self.post_file("closing", "c.csv", COUNT_CSV.replace("RM100.00", closing_coffee))

    def test_empty_audit_is_incomplete(self):
        st = self.status()
        self.assertEqual(st["code"], "INCOMPLETE")
        self.assertIn("POS item sales report", st["reasons"][0])

    def test_outside_tolerance_is_not_pass(self):
        self.load_month()
        st = self.status()
        self.assertEqual(st["code"], "OUTSIDE")
        self.assertFalse(st["within_tolerance"])

    def test_pass_when_complete_clean_and_within_tolerance(self):
        # Coffee theoretical 150 x 18 g x 0.10 = 270; milk 120 x 200 ml / 0.9 x 0.004 = 106.67;
        # sugar 2.4 g x 0.005 = 0.012 (no stock). Choose closing values so actual matches theoretical.
        self.load_month(closing_coffee="RM30.00")
        self.audit.variance_tolerance = Decimal("0.5")
        self.audit.save()
        self.assertEqual(self.status()["code"], "PASS")

    def test_failed_check_is_review_everywhere(self):
        self.load_month()
        self.post_file("pos", "pos.csv", POS_CSV)  # Mystery Cake is not on the menu
        self.assertEqual(self.status()["code"], "REVIEW")
        self.assertContains(self.client.get(reverse("audit_detail", args=[self.audit.pk])), ">Review</span>")
        self.assertContains(self.client.get(reverse("trends")), ">Review</span>")
        wb = openpyxl.load_workbook(BytesIO(self.client.get(reverse("audit_export", args=[self.audit.pk])).content))
        status_row = next(r for r in wb["Dashboard"].iter_rows(values_only=True) if r[0] == "Status")
        self.assertEqual(status_row[1], "Review")

    def test_tolerance_must_be_a_sensible_fraction(self):
        resp = self.client.post(reverse("home"), {"outlet": self.outlet.pk, "period": "2026-11",
                                                  "variance_tolerance": "2"})
        self.assertFalse(AuditPeriod.objects.filter(period="2026-11").exists())
        self.assertContains(resp, "less than or equal to 0.5")


class SignOffTests(Base):
    """C-01, C-02: sign-off freezes the month, and only someone allowed to sign off can unlock."""

    def setUp(self):
        super().setUp()
        call_command("setup_roles", stdout=StringIO())
        self.chef = User.objects.create_user("chef", password="pw", is_staff=True)
        self.chef.groups.add(Group.objects.get(name="Chef"))
        self.proc = User.objects.create_user("proc", password="pw", is_staff=True)
        self.proc.groups.add(Group.objects.get(name="Procurement"))
        self.post_file("pos", "pos.csv", POS_CSV)
        self.post_file("purchases", "x.csv", XERO_OCT)
        self.post_file("opening", "o.csv", COUNT_CSV)
        self.post_file("closing", "c.csv", COUNT_CSV.replace("RM100.00", "RM50.00"))

    def theoretical(self):
        return services.run_audit(AuditPeriod.objects.get(pk=self.audit.pk)).dashboard()["theoretical_fb"]

    def sign_off(self, user=None, action="sign_off"):
        if user:
            self.client.force_login(user)
        return self.client.post(reverse("audit_sign_off", args=[self.audit.pk]), {"action": action})

    def test_signed_off_month_ignores_later_recipe_and_price_changes(self):
        before = self.theoretical()
        self.sign_off()
        audit = AuditPeriod.objects.get(pk=self.audit.pk)
        self.assertTrue(audit.locked)
        self.assertEqual(audit.locked_by, self.user)
        line = RecipeLine.objects.get(recipe__menu_item__sku="M1", item__sku="MILK")
        line.qty_per_batch = 250
        line.save()
        Item.objects.filter(sku="COFFEE").update(purchase_price=200)
        Item.objects.filter(sku="MILK").update(category="Dairy")
        self.assertAlmostEqual(self.theoretical(), before)
        self.assertNotContains(self.client.get(reverse("audit_detail", args=[self.audit.pk])), "Dairy")
        self.sign_off(action="unlock")
        self.assertNotAlmostEqual(self.theoretical(), before)

    def test_chef_and_procurement_cannot_sign_off_or_unlock(self):
        for user in (self.chef, self.proc):
            self.assertEqual(self.sign_off(user).status_code, 403)
        self.client.force_login(self.user)
        self.sign_off()
        for user in (self.chef, self.proc):
            self.assertEqual(self.sign_off(user, "unlock").status_code, 403)
        self.assertTrue(AuditPeriod.objects.get(pk=self.audit.pk).locked)

    def test_signed_off_audit_cannot_be_edited_or_deleted_in_master_data(self):
        self.sign_off()
        sale = self.audit.sales.filter(menu_item__isnull=False).first()
        change_url = reverse("admin:audit_auditperiod_change", args=[self.audit.pk])
        self.client.force_login(self.proc)
        page = self.client.get(change_url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="sales-0-qty"')
        self.client.post(change_url, {
            "sales-TOTAL_FORMS": "1", "sales-INITIAL_FORMS": "1", "sales-0-id": sale.pk,
            "sales-0-audit": self.audit.pk, "sales-0-pos_name": sale.pos_name, "sales-0-qty": "1900",
            "production-TOTAL_FORMS": "0", "production-INITIAL_FORMS": "0",
        })
        sale.refresh_from_db()
        self.assertNotEqual(sale.qty, 1900)
        resp = self.client.post(reverse("admin:audit_auditperiod_delete", args=[self.audit.pk]), {"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(AuditPeriod.objects.filter(pk=self.audit.pk).exists())
        with self.assertRaises(ValueError):
            AuditPeriod.objects.get(pk=self.audit.pk).delete()

    def test_uploads_to_a_signed_off_audit_change_nothing(self):
        self.sign_off()
        self.post_file("pos", "pos.csv", "Full Name,Quantity\nLatte - Hot,999\n")
        self.assertEqual(self.audit.sales.count(), 3)
        with self.assertRaises(ValueError):
            importers.load_pos_sales(self.audit, upload("pos.csv", "Full Name,Quantity\nLatte - Hot,1\n"))

    def test_sold_menu_items_and_purchased_items_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            MenuItem.objects.get(sku="M1").delete()
        with self.assertRaises(ProtectedError):
            Item.objects.get(sku="COFFEE").delete()

    def test_signed_off_audit_in_trends_and_list(self):
        self.sign_off()
        self.assertContains(self.client.get(reverse("trends")), "Signed off")
        self.assertContains(self.client.get(reverse("home")), "Signed off")


class PermissionAndExportTests(Base):
    """C-13, C-14."""

    def test_chef_uploads_sales_but_not_purchases_or_counts(self):
        call_command("setup_roles", stdout=StringIO())
        chef = User.objects.create_user("chef", password="pw", is_staff=True)
        chef.groups.add(Group.objects.get(name="Chef"))
        self.client.force_login(chef)
        self.assertEqual(self.post_file("pos", "pos.csv", POS_CSV).status_code, 302)
        self.assertEqual(self.audit.sales.count(), 3)
        for kind in ("purchases", "interco", "opening", "closing"):
            self.assertEqual(self.post_file(kind, "f.csv", COUNT_CSV).status_code, 403, kind)
        page = self.client.get(reverse("audit_detail", args=[self.audit.pk]))
        self.assertContains(page, "Uploaded by procurement", count=4)

    def test_staff_without_a_role_cannot_upload(self):
        user = User.objects.create_user("nogroup", password="pw", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_auditperiod"))
        self.client.force_login(user)
        self.assertEqual(self.post_file("pos", "pos.csv", POS_CSV).status_code, 403)

    def test_text_that_looks_like_a_formula_is_exported_as_text(self):
        self.post_file("pos", "pos.csv", 'Full Name,Quantity\n"=HYPERLINK(""http://evil.example"",""x"")",3\n')
        wb = openpyxl.load_workbook(BytesIO(self.client.get(reverse("audit_export", args=[self.audit.pk])).content))
        cells = [c for row in wb["Checks"].iter_rows(values_only=True) for c in row if isinstance(c, str)]
        self.assertTrue(any(c.startswith("'=HYPERLINK") or "=HYPERLINK" in c and not c.startswith("=") for c in cells))
        self.assertFalse(any(c.startswith("=") for c in cells))


class CategoryTests(Base):
    """C-03: an item in a category outside the report's list is shown and flagged, not dropped."""

    def test_unknown_category_is_shown_and_flagged(self):
        Item.objects.filter(sku="MILK").update(category="Dairy")
        self.post_file("pos", "pos.csv", POS_CSV)
        result = services.run_audit(self.audit)
        cats = {c["category"]: c for c in result.dashboard()["by_category"]}
        self.assertIn("Dairy", cats)
        self.assertGreater(cats["Dairy"]["theoretical"], 0)
        check = next(c for c in result.checks if c.key == "category")
        self.assertEqual(check.details, ["MILK (Dairy)"])

    def test_item_master_category_case_is_normalised(self):
        rows = [["SKU", "Item Name", "Category", "Pack Size (base units per pack)"],
                ["A1", "Thing", "food & beverage", "1"], ["A2", "Meat", "Meat", "1"]]
        cats = {r["sku"]: r["category"] for r in importers.parse_item_master(rows)}
        self.assertEqual(cats, {"A1": "Food & Beverage", "A2": "Meat"})
        report = importers.load_item_master(rows)
        self.assertTrue(any("Meat" in w for w in report.warnings))
