"""Swaps for one month (an item ran out and another was bought instead), on top of the standing swaps."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from audit import services
from audit.models import (
    AuditPeriod, AuditSubstitution, Brand, Item, MenuItem, Outlet, Recipe, RecipeLine, SaleLine, Substitution,
)


class MonthSwapTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("chef", "chef@example.com", "pw")
        self.client.force_login(self.user)
        self.brand = Brand.objects.create(code="TAP", name="Tapestry")
        outlet = Outlet.objects.create(brand=self.brand, code="TCK", name="Kamunting")
        mk = lambda sku, price: Item.objects.create(sku=sku, name=sku, pack_size=1000, purchase_price=price,
                                                    base_uom="ML")
        self.farm, self.magnolia, self.oat = mk("FARM", 9), mk("MAGNOLIA", 7), mk("OAT", 12)
        latte = MenuItem.objects.create(brand=self.brand, sku="LATTE", name="Latte", price=14, category="Beverage")
        recipe = Recipe.objects.create(brand=self.brand, menu_item=latte, batch_yield=1)
        RecipeLine.objects.create(recipe=recipe, item=self.farm, qty_per_batch=200)
        # The central item master's group-wide swap: recipes say Farm House, the group buys Magnolia.
        Substitution.objects.create(brand=None, original=self.farm, substitute=self.magnolia)
        self.audit = AuditPeriod.objects.create(outlet=outlet, period="2026-10")
        SaleLine.objects.create(audit=self.audit, pos_name="Latte", menu_item=latte, qty=100)

    def theoretical(self):
        result = services.run_audit(self.audit)
        return {v.sku: round(v.theoretical_cost, 2) for v in result.variance if v.theoretical_cost}

    def test_standing_swap_applies_by_default(self):
        self.assertEqual(self.theoretical(), {"MAGNOLIA": 140.0})  # 100 x 200 ml at RM 7 / L

    def test_turning_the_standing_swap_off_for_one_month(self):
        AuditSubstitution.objects.create(audit=self.audit, original=self.farm, substitute=None, note="Magnolia out")
        self.assertEqual(self.theoretical(), {"FARM": 180.0})
        # Other months still use the standing swap.
        other = AuditPeriod.objects.create(outlet=self.audit.outlet, period="2026-11")
        self.assertEqual([s.substitute_sku for s in services.build_engine_input(other).substitutions], ["MAGNOLIA"])

    def test_temporary_swap_replaces_the_standing_one(self):
        AuditSubstitution.objects.create(audit=self.audit, original=self.farm, substitute=self.oat, factor=1)
        self.assertEqual(self.theoretical(), {"OAT": 240.0})

    def test_add_from_the_uploaded_data_page_and_locked_month(self):
        url = reverse("audit_line_new", args=[self.audit.pk, "swap"])
        self.client.post(url, {"original_sku": "farm", "substitute_sku": "", "factor": "1", "note": "Magnolia out"})
        swap = self.audit.swaps.get()
        self.assertEqual((swap.original.sku, swap.substitute), ("FARM", None))
        page = self.client.get(reverse("audit_inputs", args=[self.audit.pk]))
        self.assertContains(page, "standing swap off")
        self.assertContains(page, "FARM &rarr; MAGNOLIA (group-wide)")
        # A second swap for the same item is refused.
        resp = self.client.post(url, {"original_sku": "FARM", "substitute_sku": "OAT", "factor": "1"})
        self.assertContains(resp, "already has a swap this month")
        # Signed off: no changes.
        self.audit.locked = True
        self.audit.save()
        self.client.post(url, {"original_sku": "OAT", "substitute_sku": "FARM", "factor": "1"})
        self.assertEqual(self.audit.swaps.count(), 1)

    def test_sign_off_snapshot_keeps_the_month_swap(self):
        AuditSubstitution.objects.create(audit=self.audit, original=self.farm, substitute=None)
        data = services.engine_input_to_json(services.build_engine_input(self.audit))
        self.assertEqual(data["substitutions"], [])
