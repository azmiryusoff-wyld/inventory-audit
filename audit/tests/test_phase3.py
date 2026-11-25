"""Daily use and the group view (findings C-21 to C-48 of the usability audit, Phase 3)."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.contrib.auth.models import Group, User
from django.urls import reverse

from audit import services
from audit.signals import flush_pending
from audit.models import (
    AuditPeriod, Brand, ChangeLog, Item, MenuItem, Outlet, PosAlias, Recipe, RecipeLine, ReportCache, SemiFinished,
    Substitution, UserAccess,
)

from .test_controls import XERO_OCT
from .test_importers_and_views import COUNT_CSV, POS_CSV, upload
from .test_phase2 import Phase2Base, messages


class RecipeAndMenuValidationTests(Phase2Base):
    """C-31, C-32."""

    def test_recipe_takes_its_brand_from_the_menu_item(self):
        demo = Brand.objects.create(code="DEMO", name="Demo")
        cake = MenuItem.objects.create(brand=self.brand, sku="M9", name="Cake", price=9)
        r = Recipe(brand=demo, menu_item=cake)
        r.full_clean()
        r.save()
        self.assertEqual(r.brand, self.brand)

    def test_semi_finished_of_another_brand_refused_in_recipe(self):
        other = Brand.objects.create(code="CHONKY", name="Chonky")
        dough = SemiFinished.objects.create(brand=other, sku="DOUGH", name="Dough")
        line = RecipeLine(recipe=Recipe.objects.get(menu_item__sku="M1"), semi_finished=dough, qty_per_batch=5)
        with self.assertRaisesMessage(ValidationError, "belongs to CHONKY"):
            line.full_clean()

    def test_negative_price_and_quantity_refused(self):
        m = MenuItem(brand=self.brand, sku="M8", name="Bad", price=Decimal("-3"))
        with self.assertRaisesMessage(ValidationError, "can't be negative"):
            m.full_clean()
        line = RecipeLine(recipe=Recipe.objects.get(menu_item__sku="M1"), item=Item.objects.get(sku="SUGAR"),
                          qty_per_batch=Decimal("-5"))
        with self.assertRaisesMessage(ValidationError, "above zero"):
            line.full_clean()

    def test_chained_or_self_substitution_refused(self):
        sugar, coffee, milk = (Item.objects.get(sku=s) for s in ("SUGAR", "COFFEE", "MILK"))
        Substitution.objects.create(original=sugar, substitute=coffee)
        with self.assertRaisesMessage(ValidationError, "chain"):
            Substitution(original=coffee, substitute=milk).full_clean()
        with self.assertRaisesMessage(ValidationError, "chain"):
            Substitution(original=milk, substitute=sugar).full_clean()
        with self.assertRaisesMessage(ValidationError, "itself"):
            Substitution(original=milk, substitute=milk).full_clean()
        with self.assertRaisesMessage(ValidationError, "already has"):
            Substitution(original=sugar, substitute=milk).full_clean()


class MenuEngineeringNoRecipeTests(Phase2Base):
    """C-33."""

    def test_item_with_no_recipe_is_not_a_star_and_not_in_the_average(self):
        MenuItem.objects.create(brand=self.brand, sku="M7", name="Mystery Cake - Regular Price", price=50,
                                category="Beverage")
        self.post("pos", "pos.csv", POS_CSV)
        result = services.run_audit(self.audit)
        cake = next(r for r in result.menu if r.sku == "M7")
        self.assertEqual(cake.menu_class, "NO RECIPE")
        latte = next(r for r in result.menu if r.sku == "M1")
        with_recipe = [r for r in result.menu if r.has_recipe]
        units = sum(r.units for r in with_recipe)
        self.assertAlmostEqual(latte.cat_avg_margin, sum(r.total_contribution for r in with_recipe) / units)
        self.assertContains(self.client.get(reverse("audit_menu", args=[self.audit.pk])), "NO RECIPE")


class LineEditTests(Phase2Base):
    """C-25, C-43."""

    def test_fix_one_purchase_line_and_it_is_logged(self):
        self.post("purchases", "x.csv", XERO_OCT)
        line = self.audit.purchases.get(sku="COFFEE")
        url = reverse("audit_line_edit", args=[self.audit.pk, "purchase", line.pk])
        data = {"source": "supplier", "invoice_date": "2026-10-01", "invoice_number": line.invoice_number,
                "supplier": line.supplier, "sku": "coffee", "description": line.description, "qty": "3", "value": "300"}
        resp = self.client.post(url, data, follow=True)
        self.assertIn("Purchase line saved", messages(resp))
        line.refresh_from_db()
        self.assertEqual((line.qty, line.value, line.sku), (3, 300, "COFFEE"))
        entry = ChangeLog.objects.get(action="change", what="Purchase line")
        self.assertEqual(entry.changes["value"][1], "300")
        self.assertEqual(entry.user, self.user)
        self.assertContains(self.client.get(reverse("audit_history", args=[self.audit.pk])), "value")

    def test_unknown_sku_refused_with_suggestion(self):
        resp = self.client.post(reverse("audit_line_new", args=[self.audit.pk, "count"]),
                                {"kind": "closing", "sku": "COFEE", "description": "", "value": "10"})
        self.assertContains(resp, "Did you mean COFFEE?")
        self.assertFalse(self.audit.stock_counts.exists())

    def test_add_wastage_and_delete_a_line(self):
        resp = self.client.post(reverse("audit_line_new", args=[self.audit.pk, "production"]),
                                {"kind": "wastage", "component": "m1", "qty": "4", "customer": "", "note": "dropped"},
                                follow=True)
        self.assertIn("added", messages(resp))
        line = self.audit.production.get()
        self.assertEqual((line.menu_item.sku, line.kind, line.qty), ("M1", "wastage", 4))
        self.client.post(reverse("audit_line_edit", args=[self.audit.pk, "production", line.pk]), {"action": "delete"})
        self.assertFalse(self.audit.production.exists())
        self.assertTrue(ChangeLog.objects.filter(action="delete", what__startswith="Production").exists())

    def test_procurement_cannot_edit_sales_lines_and_locked_month_is_read_only(self):
        self.post("pos", "pos.csv", POS_CSV)
        sale = self.audit.sales.first()
        self.client.force_login(self.proc)
        page = self.client.get(reverse("audit_inputs", args=[self.audit.pk]))
        self.assertContains(page, reverse("audit_line_new", args=[self.audit.pk, "purchase"]))
        self.client.force_login(self.user)
        services.sign_off(self.audit, self.user)
        resp = self.client.post(reverse("audit_line_edit", args=[self.audit.pk, "sale", sale.pk]),
                                {"pos_name": "x", "qty": "1"}, follow=True)
        self.assertIn("signed off", messages(resp))
        sale.refresh_from_db()
        self.assertNotEqual(sale.pos_name, "x")

    def test_upload_is_logged(self):
        self.post("closing", "closing-oct.csv", COUNT_CSV)
        entry = ChangeLog.objects.get(action="upload")
        self.assertEqual((entry.what, entry.object_repr, entry.audit), ("Closing stock count", "closing-oct.csv", self.audit))


class MasterDataScreenTests(Phase2Base):
    """C-24, C-44."""

    def test_recipe_screen_saves_lines_and_logs(self):
        cake = MenuItem.objects.create(brand=self.brand, sku="M7", name="Cake", price=12)
        url = reverse("data_recipe_new", args=["EOC"])
        data = {"parent": f"menu:{cake.pk}", "batch_yield": "1", "note": "",
                "lines-TOTAL_FORMS": "3", "lines-INITIAL_FORMS": "0",
                "lines-0-component": "sugar", "lines-0-qty": "30", "lines-1-component": "SF-1", "lines-1-qty": "10",
                "lines-2-component": "", "lines-2-qty": ""}
        resp = self.client.post(url, data, follow=True)
        self.assertIn("saved", messages(resp))
        recipe = Recipe.objects.get(menu_item=cake)
        self.assertEqual(sorted(line.component_sku for line in recipe.lines.all()), ["SF-1", "SUGAR"])
        self.assertTrue(ChangeLog.objects.filter(what="Recipe", action="add").exists())
        self.assertContains(resp, "Cost per unit")

    def test_recipe_screen_refuses_other_brand_sf_and_zero_qty(self):
        other = Brand.objects.create(code="CHONKY", name="Chonky")
        SemiFinished.objects.create(brand=other, sku="DOUGH", name="Dough")
        cake = MenuItem.objects.create(brand=self.brand, sku="M7", name="Cake", price=12)
        data = {"parent": f"menu:{cake.pk}", "batch_yield": "1", "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
                "lines-0-component": "DOUGH", "lines-0-qty": "30", "lines-1-component": "SUGAR", "lines-1-qty": "0"}
        resp = self.client.post(reverse("data_recipe_new", args=["EOC"]), data)
        self.assertContains(resp, "semi-finished item of CHONKY")
        self.assertContains(resp, "above zero")
        self.assertFalse(Recipe.objects.filter(menu_item=cake).exists())

    def test_where_used_includes_semi_finished_chain(self):
        resp = self.client.get(reverse("data_item", args=["SUGAR"]))
        self.assertContains(resp, "semi-finished SF-1")  # Latte uses SF-1, which uses sugar
        self.assertContains(resp, "Latte - Hot")
        resp = self.client.get(reverse("data_recipes", args=["EOC"]), {"q": "MILK"})
        self.assertContains(resp, "Latte - Hot")
        self.assertNotContains(resp, "Espresso")

    def test_procurement_can_view_but_not_change_or_link_pos_names(self):
        self.client.force_login(self.proc)
        latte = MenuItem.objects.get(sku="M1")
        self.assertEqual(self.client.get(reverse("data_menu", args=["EOC"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("data_menu_edit", args=["EOC", latte.pk])).status_code, 403)
        self.client.force_login(self.chef)
        resp = self.client.post(reverse("data_menu_edit", args=["EOC", latte.pk]),
                                {"sku": "M1", "name": "Latte - Hot", "price": "16", "category": "Beverage", "note": "",
                                 "active": "on", "aliases": "Latte Hot (old)"}, follow=True)
        self.assertIn("saved", messages(resp))
        self.assertTrue(PosAlias.objects.filter(menu_item=latte, pos_name="Latte Hot (old)").exists())
        entry = ChangeLog.objects.get(what="Menu item")
        self.assertEqual(entry.changes["price"], ["15", "16"])

    def test_item_master_shows_last_sync(self):
        from django.utils import timezone

        Item.objects.filter(sku="COFFEE").update(synced_at=timezone.now())
        self.assertContains(self.client.get(reverse("data_items")), "last synced")


class AccessTests(Phase2Base):
    """C-41, C-42."""

    def setUp(self):
        super().setUp()
        self.chonky = Brand.objects.create(code="CHONKY", name="Chonky")
        self.trx = Outlet.objects.create(brand=self.chonky, code="TRX", name="TRX")
        self.chonky_audit = AuditPeriod.objects.create(outlet=self.trx, period="2026-10")
        self.manager = User.objects.create_user("mgr", password="pw", is_staff=True)
        self.manager.groups.add(Group.objects.get(name="Outlet manager"))
        UserAccess.objects.create(user=self.manager).brands.add(self.chonky)

    def test_outlet_manager_sees_only_their_brand(self):
        self.client.force_login(self.manager)
        home = self.client.get(reverse("home"))
        self.assertContains(home, reverse("audit_detail", args=[self.chonky_audit.pk]))
        self.assertNotContains(home, reverse("audit_detail", args=[self.audit.pk]))
        self.assertEqual(self.client.get(reverse("audit_detail", args=[self.audit.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("audit_export", args=[self.audit.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("menu_costing", args=["EOC"])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("trends"), {"from": "2000-01"}), "KLCC")

    def test_user_with_no_access_sees_nothing(self):
        nobody = User.objects.create_user("nobody", password="pw", is_staff=True)
        self.client.force_login(nobody)
        self.assertContains(self.client.get(reverse("home")), "don't have access to any outlet")
        self.assertEqual(self.client.get(reverse("audit_detail", args=[self.audit.pk])).status_code, 404)

    def test_chef_and_procurement_see_every_outlet(self):
        for user in (self.chef, self.proc):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("audit_detail", args=[self.chonky_audit.pk])).status_code, 200)

    def test_login_locks_after_five_wrong_passwords(self):
        self.client.logout()
        url = reverse("admin:login")
        for _ in range(5):
            self.client.post(url, {"username": "chef", "password": "wrong"})
        resp = self.client.post(url, {"username": "chef", "password": "pw"})
        self.assertContains(resp, "Too many wrong passwords")
        self.assertNotIn("_auth_user_id", self.client.session)
        # Another user isn't affected.
        resp = self.client.post(url, {"username": "proc", "password": "pw"})
        self.assertEqual(resp.status_code, 302)


class ReportsAndTrendsTests(Phase2Base):
    """C-21, C-37, C-46."""

    def test_trends_uses_stored_dashboards_and_refreshes_after_a_change(self):
        self.post("pos", "pos.csv", POS_CSV)
        self.client.get(reverse("trends"), {"from": "2000-01"})
        self.assertTrue(ReportCache.objects.filter(audit=self.audit).exists())
        sales = ReportCache.objects.get(audit=self.audit).dashboard["total_sales"]
        with CaptureQueriesContext(connection) as q:
            self.client.get(reverse("trends"), {"from": "2000-01"})
        self.assertLess(len(q), 15)
        # A price change (master data) clears it, and Trends shows the new figure.
        latte = MenuItem.objects.get(sku="M1")
        latte.price = 20
        latte.save()
        flush_pending()  # done at commit; tests run inside one transaction
        self.assertFalse(ReportCache.objects.filter(audit=self.audit).exists())
        self.client.get(reverse("trends"), {"from": "2000-01"})
        self.assertGreater(ReportCache.objects.get(audit=self.audit).dashboard["total_sales"], sales)
        # A new upload clears it too (and opening the audit afterwards stores the new figures).
        before = ReportCache.objects.get(audit=self.audit).dashboard["total_sales"]
        self.client.post(reverse("audit_upload", args=[self.audit.pk, "pos"]),
                         {"file": upload("pos.csv", POS_CSV.replace(",100,", ",10,"))})
        flush_pending()
        self.assertFalse(ReportCache.objects.filter(audit=self.audit).exists())
        self.client.get(reverse("trends"), {"from": "2000-01"})
        self.assertLess(ReportCache.objects.get(audit=self.audit).dashboard["total_sales"], before)

    def test_trends_filters(self):
        other = Brand.objects.create(code="CHONKY", name="Chonky")
        trx = Outlet.objects.create(brand=other, code="TRX", name="TRX")
        AuditPeriod.objects.create(outlet=trx, period="2026-09")
        resp = self.client.get(reverse("trends"), {"brand": "CHONKY"})
        self.assertEqual([r["audit"].outlet.code for r in resp.context["rows"]], ["TRX"])
        resp = self.client.get(reverse("trends"), {"from": "2026-10", "to": "2026-10"})
        self.assertEqual([r["audit"].period for r in resp.context["rows"]], ["2026-10"])

    def test_variance_has_totals_units_and_sorting(self):
        self.post("pos", "pos.csv", POS_CSV)
        self.post("purchases", "x.csv", XERO_OCT)
        resp = self.client.get(reverse("audit_variance", args=[self.audit.pk]))
        self.assertContains(resp, 'class="sortable"')
        self.assertContains(resp, "<tfoot>")
        coffee = next(r for r in resp.context["result"].variance if r.sku == "COFFEE")
        self.assertAlmostEqual(coffee.variance_qty, coffee.variance / 0.1)

    def test_home_filters_and_status(self):
        AuditPeriod.objects.create(outlet=self.outlet, period="2026-09")
        resp = self.client.get(reverse("home"), {"month": "2026-09"})
        self.assertEqual([r["audit"].period for r in resp.context["rows"]], ["2026-09"])
        self.assertContains(resp, "Incomplete")

    def test_uploaded_data_tab_query_count_does_not_grow_with_lines(self):
        self.post("purchases", "x.csv", XERO_OCT)
        self.post("closing", "c.csv", COUNT_CSV)
        url = reverse("audit_inputs", args=[self.audit.pk])
        self.client.get(url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        many = "\n".join(f"INV-{i},1/10/2026,Supplier,COFFEE,Beans,1,10" for i in range(200))
        self.post("purchases", "x.csv", "InvoiceNumber,InvoiceDate,ContactName,InventoryItemCode,Description,Quantity,LineAmount\n" + many)
        self.client.get(url)
        with CaptureQueriesContext(connection) as big:
            self.client.get(url)
        self.assertLessEqual(len(big), len(small) + 2)


class MonthCloseTests(Phase2Base):
    """Month-close grid (C-37 group view)."""

    def test_grid_shows_files_and_starts_missing_audits(self):
        Outlet.objects.create(brand=self.brand, code="TRX", name="TRX")
        self.post("closing", "c.csv", COUNT_CSV)
        resp = self.client.get(reverse("month_close"), {"month": "2026-10"})
        self.assertContains(resp, "✓ 2")
        self.assertContains(resp, "Not started")
        resp = self.client.post(reverse("month_close"), {"month": "2026-11"}, follow=True)
        self.assertIn("Started 2 audits for 2026-11", messages(resp))
        klcc_nov = AuditPeriod.objects.get(outlet=self.outlet, period="2026-11")
        self.assertEqual(klcc_nov.stock_counts.filter(kind="opening").count(), 2)

    def test_procurement_cannot_start_audits_without_permission(self):
        nobody = User.objects.create_user("viewer", password="pw", is_staff=True)
        self.client.force_login(nobody)
        self.assertEqual(self.client.post(reverse("month_close"), {"month": "2026-11"}).status_code, 403)


class UploadEdgeCaseTests(Phase2Base):
    """C-45."""

    def test_huge_field_and_latin1_file_give_friendly_errors(self):
        resp = self.post("closing", "c.csv", "SKU,Item Name,Total RM Value\nCOFFEE," + "x" * 200_000 + ",5\n")
        self.assertNotIn("Traceback", messages(resp))
        self.assertEqual(resp.status_code, 200)
        from django.core.files.uploadedfile import SimpleUploadedFile

        latin = "SKU,Item Name,Total RM Value\nCOFFEE,Caf\xe9 beans,5\n".encode("latin-1")
        resp = self.client.post(reverse("audit_upload", args=[self.audit.pk, "closing"]),
                                {"file": SimpleUploadedFile("c.csv", latin)}, follow=True)
        self.assertIn("1 line loaded", messages(resp))
        self.assertEqual(self.audit.stock_counts.get().description, "Café beans")


class OnboardingTests(Phase2Base):
    """C-47, C-48."""

    def test_home_explains_the_steps_and_help_has_glossary(self):
        self.assertContains(self.client.get(reverse("home")), "How it works")
        help_page = self.client.get(reverse("help"))
        for word in ("Theoretical COGS", "Yield", "PLOWHORSE", "SST"):
            self.assertContains(help_page, word)
        self.assertContains(self.client.get(reverse("audit_detail", args=[self.audit.pk])), "<abbr title=")
