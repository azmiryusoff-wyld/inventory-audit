"""Faster, clearer monthly close (findings C-15 to C-39 of the usability audit, Phase 2)."""
from io import BytesIO, StringIO

import openpyxl
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from audit import services
from audit.models import AuditPeriod, Brand, MenuItem, Outlet, PosAlias, StockCount

from .test_controls import XERO_OCT, Base
from .test_importers_and_views import COUNT_CSV, POS_CSV, upload


def messages(resp):
    return " ".join(str(m) for m in resp.context["messages"]) if resp.context else ""


class Phase2Base(Base):
    def setUp(self):
        super().setUp()
        call_command("setup_roles", stdout=StringIO())
        self.chef = User.objects.create_user("chef", password="pw", is_staff=True)
        self.chef.groups.add(Group.objects.get(name="Chef"))
        self.proc = User.objects.create_user("proc", password="pw", is_staff=True)
        self.proc.groups.add(Group.objects.get(name="Procurement"))

    def post(self, kind, name, text, audit=None):
        audit = audit or self.audit
        return self.client.post(reverse("audit_upload", args=[audit.pk, kind]), {"file": upload(name, text)},
                                follow=True)


class OpeningCarryOverTests(Phase2Base):
    """C-15."""

    def test_new_month_starts_with_last_months_closing(self):
        self.post("closing", "c.csv", COUNT_CSV)
        resp = self.client.post(reverse("home"), {"outlet": self.outlet.pk, "period": "2026-11", "tolerance_pct": "2"},
                                follow=True)
        nov = AuditPeriod.objects.get(period="2026-11")
        self.assertIn("filled in from 2026-10's closing count (2 lines)", messages(resp))
        self.assertEqual(
            sorted(nov.stock_counts.filter(kind="opening").values_list("sku", "value")),
            sorted(self.audit.stock_counts.filter(kind="closing").values_list("sku", "value")),
        )
        self.assertEqual(nov.variance_tolerance * 100, 2)

    def test_copy_button_and_mismatch_check(self):
        nov = AuditPeriod.objects.create(outlet=self.outlet, period="2026-11")
        self.post("opening", "o.csv", COUNT_CSV, audit=nov)  # coffee 100
        self.post("closing", "c.csv", COUNT_CSV.replace("RM100.00", "RM50.00"))  # October closing coffee 50
        check = next(c for c in services.input_issues(nov) if c.key == "opening_mismatch")
        self.assertEqual(check.details, ["COFFEE: opening 100.00, last closing 50.00"])
        self.assertContains(self.client.get(reverse("audit_detail", args=[nov.pk])), "Copy 2026-10's closing count")
        resp = self.client.post(reverse("audit_copy_opening", args=[nov.pk]), follow=True)
        self.assertIn("2 lines copied from 2026-10's closing count", messages(resp))
        self.assertEqual(nov.stock_counts.get(kind="opening", sku="COFFEE").value, 50)
        self.assertEqual(next(c for c in services.input_issues(nov) if c.key == "opening_mismatch").count, 0)

    def test_january_looks_at_december(self):
        self.assertEqual(services.previous_period("2027-01"), "2026-12")


class PosMatchingTests(Phase2Base):
    """C-16, C-17: fixes take effect without a re-upload; only the chef links POS names."""

    def flag(self):
        _, issues, _ = services.audit_report(AuditPeriod.objects.get(pk=self.audit.pk))
        return next(c for c in issues if c.key == "pos_unmatched")

    def test_new_alias_clears_the_flag_without_reupload(self):
        self.post("pos", "pos.csv", POS_CSV)
        self.assertEqual(self.flag().count, 1)
        self.assertEqual(self.flag().impact, "2 units not counted in sales or COGS")
        cake = MenuItem.objects.create(brand=self.brand, sku="M7", name="Cake", price=12)
        PosAlias.objects.create(menu_item=cake, pos_name="Mystery Cake - Regular Price")
        self.assertEqual(self.flag().count, 0)
        self.assertEqual(self.audit.sales.get(pos_name__startswith="Mystery").menu_item, cake)

    def test_chef_links_a_pos_name_procurement_cannot(self):
        self.post("pos", "pos.csv", POS_CSV)
        latte = MenuItem.objects.get(sku="M1")
        url = reverse("audit_map_pos_name", args=[self.audit.pk])
        data = {"pos_name": "Mystery Cake - Regular Price", "menu_item": latte.pk}

        self.client.force_login(self.proc)
        page = self.client.get(reverse("audit_detail", args=[self.audit.pk]))
        self.assertNotContains(page, url)
        self.assertContains(page, "Chef")  # who fixes it
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(self.flag().count, 1)

        self.client.force_login(self.chef)
        self.assertContains(self.client.get(reverse("audit_detail", args=[self.audit.pk])), url)
        resp = self.client.post(url, data, follow=True)
        self.assertIn("now counts as Latte - Hot (1 line matched)", messages(resp))
        self.assertTrue(PosAlias.objects.filter(menu_item=latte, pos_name="Mystery Cake - Regular Price").exists())
        self.assertEqual(self.flag().count, 0)

    def test_menu_item_from_another_brand_is_refused(self):
        other = Brand.objects.create(code="CHONKY", name="Chonky")
        cookie = MenuItem.objects.create(brand=other, sku="C1", name="Cookie", price=5)
        self.post("pos", "pos.csv", POS_CSV)
        self.client.post(reverse("audit_map_pos_name", args=[self.audit.pk]),
                         {"pos_name": "Mystery Cake - Regular Price", "menu_item": cookie.pk})
        self.assertFalse(PosAlias.objects.filter(pos_name="Mystery Cake - Regular Price").exists())

    def test_pos_column_used_is_named(self):
        self.outlet.pos_location = "EOC_KLCC-O2"
        self.outlet.save()
        text = "Item,EOC_GARDENS-O1 (Count),EOC_KLCC-O2 (Count)\nLatte - Hot,5,7\n"
        resp = self.post("pos", "pos.csv", text)
        self.assertIn("Quantities read from the column 'EOC_KLCC-O2 (Count)'", messages(resp))


class SkuAndCountTests(Phase2Base):
    """C-26, C-27, C-39."""

    def test_sku_case_and_spacing_ignored_with_suggestion(self):
        text = XERO_OCT.replace(",COFFEE,", ",coffee ,").replace(",ZZZ999,", ",COFEE,")
        resp = self.post("purchases", "x.csv", text)
        self.assertTrue(self.audit.purchases.filter(sku="COFFEE", item__sku="COFFEE").exists())
        self.assertIn("Unknown SKU COFEE (UNKNOWN THING), did you mean COFFEE?", messages(resp))
        check = next(c for c in services.input_issues(self.audit) if c.key == "purchase_unknown")
        self.assertEqual(check.impact, "RM 5.00 not counted in actual COGS")
        self.assertEqual(check.owner, "Procurement")

    def test_counted_packs_are_valued_at_item_master_price(self):
        text = "SKU,Item Name,Counted Qty (packs),Total RM Value\nCOFFEE,Beans,3,\nMILK,Milk,,8.00\n"
        resp = self.post("closing", "c.csv", text)
        self.assertIn("1 valued from counted packs", messages(resp))
        self.assertEqual(self.audit.stock_counts.get(sku="COFFEE").value, 300)  # 3 x RM100 per pack
        self.assertEqual(self.audit.stock_counts.get(sku="MILK").value, 8)


class StartAuditTests(Phase2Base):
    """C-29, C-30, C-49."""

    def test_future_month_refused_and_duplicate_links_to_existing(self):
        resp = self.client.post(reverse("home"), {"outlet": self.outlet.pk, "period": "2999-01", "tolerance_pct": "2"})
        self.assertContains(resp, "hasn&#x27;t started yet")
        resp = self.client.post(reverse("home"), {"outlet": self.outlet.pk, "period": "2026-10", "tolerance_pct": "2"})
        self.assertContains(resp, "already exists")
        self.assertContains(resp, reverse("audit_detail", args=[self.audit.pk]))
        self.assertNotContains(resp, "__all__")

    def test_month_picker_defaults_to_last_month(self):
        from audit.views import last_month

        self.assertContains(self.client.get(reverse("home")), f'type="month" name="period" value="{last_month()}"')


class ReportTests(Phase2Base):
    """C-20, C-23, C-34, C-38."""

    def test_brand_not_set_up_note(self):
        brand = Brand.objects.create(code="GNP", name="Good News Pizza")
        outlet = Outlet.objects.create(brand=brand, code="GNP", name="Good News Pizza")
        audit = AuditPeriod.objects.create(outlet=outlet, period="2026-10")
        self.assertContains(self.client.get(reverse("audit_detail", args=[audit.pk])), "GNP isn't set up yet")
        self.assertContains(self.client.get(reverse("menu_costing", args=["GNP"])), "has no menu items yet")
        self.assertNotContains(self.client.get(reverse("audit_detail", args=[self.audit.pk])), "isn't set up yet")

    def test_sst_basis_and_menu_classes_explained(self):
        self.post("pos", "pos.csv", POS_CSV)
        self.assertContains(self.client.get(reverse("audit_detail", args=[self.audit.pk])), "SST included")
        menu = self.client.get(reverse("audit_menu", args=[self.audit.pk]))
        self.assertContains(menu, "Prices include SST")
        self.assertContains(menu, "Rework it or take it off the menu")
        self.assertContains(self.client.get(reverse("menu_costing", args=["EOC"])), "<strong>without</strong>")

    def test_excel_is_formatted_and_rounded(self):
        self.post("pos", "pos.csv", POS_CSV)
        self.post("purchases", "x.csv", XERO_OCT)
        wb = openpyxl.load_workbook(BytesIO(self.client.get(reverse("audit_export", args=[self.audit.pk])).content))
        dash = {r[0].value: r[1] for r in wb["Dashboard"].iter_rows() if r[0].value}
        self.assertEqual(dash["Total sales (POS, SST included)"].number_format, "#,##0.00;(#,##0.00);-")
        self.assertEqual(dash["Variance % of theoretical"].number_format, "0.0%")
        for row in wb["Variance"].iter_rows(min_row=2, values_only=True):
            for v in row[4:10]:
                if isinstance(v, float):
                    self.assertEqual(v, round(v, 2))
        self.assertIsNotNone(wb["Variance"].auto_filter.ref)
        self.assertEqual(wb["Purchases"].max_row, 4)  # header + 3 lines (handling charge skipped)
