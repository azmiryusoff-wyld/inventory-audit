"""A small made-up café so the app can be tried without real data."""
from decimal import Decimal

from django.core.management.base import BaseCommand

from audit.models import (
    AuditPeriod, Brand, Item, MenuItem, Outlet, PurchaseLine, Recipe, RecipeLine, SaleLine,
    SemiFinished, StockCount,
)


class Command(BaseCommand):
    help = "Create a DEMO brand with one outlet and one month of made-up data."

    def handle(self, *args, **opts):
        brand, _ = Brand.objects.get_or_create(code="DEMO", defaults={"name": "Demo Café"})
        outlet, _ = Outlet.objects.get_or_create(brand=brand, code="HQ", defaults={"name": "Demo HQ"})

        def item(sku, name, cat, pack, uom, price, y="1"):
            return Item.objects.update_or_create(sku=sku, defaults=dict(
                name=name, category=cat, pack_size=pack, base_uom=uom, purchase_price=price, yield_pct=Decimal(y)))[0]

        beans = item("DEMO-BEAN", "HOUSE BLEND BEANS 1KG", "Food & Beverage", 1000, "G", 90)
        milk = item("DEMO-MILK", "FRESH MILK 1L", "Food & Beverage", 1000, "ML", 7, "0.95")
        sugar = item("DEMO-SUGAR", "CASTER SUGAR 1KG", "Food & Beverage", 1000, "G", 4)
        butter = item("DEMO-BUTTER", "BUTTER 250G", "Food & Beverage", 250, "G", 12)
        flour = item("DEMO-FLOUR", "FLOUR 1KG", "Food & Beverage", 1000, "G", 3)
        cup = item("DEMO-CUP", "8OZ CUP 50PCS", "Packaging", 50, "PCS", 10)

        syrup, _ = SemiFinished.objects.get_or_create(brand=brand, sku="DEMO-SF-1", defaults={"name": "VANILLA SYRUP", "output_uom": "G"})

        def recipe(parent, lines, batch=1):
            kw = {"menu_item": parent} if isinstance(parent, MenuItem) else {"semi_finished": parent}
            r, _ = Recipe.objects.update_or_create(brand=brand, **kw, defaults={"batch_yield": batch})
            r.lines.all().delete()
            for comp, q in lines:
                RecipeLine.objects.create(recipe=r, qty_per_batch=q,
                                          **({"item": comp} if isinstance(comp, Item) else {"semi_finished": comp}))

        recipe(syrup, [(sugar, 600)], batch=1000)
        menu = {}
        for sku, name, price, cat in [("D-01", "Latte - Hot", 14, "Beverage"), ("D-02", "Vanilla Latte - Hot", 16, "Beverage"),
                                      ("D-03", "Long Black - Hot", 11, "Beverage"), ("D-04", "Butter Cookie - Regular Price", 6, "Food")]:
            menu[sku], _ = MenuItem.objects.update_or_create(brand=brand, sku=sku, defaults={"name": name, "price": price, "category": cat})
        recipe(menu["D-01"], [(beans, 18), (milk, 200), (cup, 1)])
        recipe(menu["D-02"], [(beans, 18), (milk, 200), (syrup, 20), (cup, 1)])
        recipe(menu["D-03"], [(beans, 18), (cup, 1)])
        recipe(menu["D-04"], [(butter, 15), (flour, 25), (sugar, 10)])

        audit, _ = AuditPeriod.objects.get_or_create(outlet=outlet, period="2026-10")
        audit.sales.all().delete(); audit.purchases.all().delete(); audit.stock_counts.all().delete()
        for sku, q in [("D-01", 900), ("D-02", 300), ("D-03", 650), ("D-04", 220)]:
            SaleLine.objects.create(audit=audit, pos_name=menu[sku].name, menu_item=menu[sku], qty=q)
        SaleLine.objects.create(audit=audit, pos_name="Seasonal Special - Regular Price", qty=12)
        for it, val in [(beans, 3400), (milk, 1750), (sugar, 60), (butter, 120), (flour, 30), (cup, 400)]:
            PurchaseLine.objects.create(audit=audit, sku=it.sku, item=it, description=it.name, value=val,
                                        qty=Decimal(val) / it.purchase_price, supplier="Demo Supplier")
        for kind, vals in [("opening", [(beans, 450), (milk, 140), (sugar, 20), (butter, 36), (flour, 9), (cup, 120)]),
                           ("closing", [(beans, 380), (milk, 98), (sugar, 18), (butter, 30), (flour, 12), (cup, 90)])]:
            for it, val in vals:
                StockCount.objects.create(audit=audit, kind=kind, sku=it.sku, item=it, description=it.name, value=val)
        self.stdout.write(self.style.SUCCESS(f"Demo data ready: audit #{audit.pk} ({audit})"))
