"""Glue between the database and the pure engine."""
from collections import defaultdict

from . import engine
from .models import (
    AuditPeriod, Brand, Item, MenuItem, Recipe, SemiFinished, Substitution,
)


def _items() -> dict[str, engine.Item]:
    return {
        i.sku: engine.Item(
            sku=i.sku,
            name=i.name,
            category=i.category,
            cost_per_base=float(i.cost_per_base),
            yield_pct=float(i.yield_pct),
            base_uom=i.base_uom,
        )
        for i in Item.objects.all()
    }


def _recipes(brand: Brand) -> dict[str, engine.Recipe]:
    out = {}
    qs = Recipe.objects.filter(brand=brand).select_related("menu_item", "semi_finished").prefetch_related(
        "lines__item", "lines__semi_finished"
    )
    for r in qs:
        if not r.parent:
            continue
        out[r.parent_sku] = engine.Recipe(
            parent_sku=r.parent_sku,
            batch_yield=float(r.batch_yield),
            lines=tuple(
                engine.RecipeLine(line.component_sku, float(line.qty_per_batch)) for line in r.lines.all()
            ),
        )
    return out


def _menu(brand: Brand) -> dict[str, engine.MenuItem]:
    return {
        m.sku: engine.MenuItem(m.sku, m.name, float(m.price or 0), m.category or "Other")
        for m in MenuItem.objects.filter(brand=brand)
    }


def _substitutions(brand: Brand) -> list[engine.Substitution]:
    """Brand mappings first; group-wide ones only for items the brand hasn't mapped itself."""
    qs = Substitution.objects.filter(active=True).select_related("original", "substitute")
    own = list(qs.filter(brand=brand))
    mapped = {s.original_id for s in own}
    group = [s for s in qs.filter(brand__isnull=True) if s.original_id not in mapped]
    return [engine.Substitution(s.original.sku, s.substitute.sku, float(s.factor)) for s in own + group]


def brand_input(brand: Brand) -> engine.EngineInput:
    """Master data only, no period data. Used for recipe and menu costing."""
    return engine.EngineInput(
        items=_items(),
        recipes=_recipes(brand),
        menu=_menu(brand),
        semi_finished=set(SemiFinished.objects.filter(brand=brand).values_list("sku", flat=True)),
        substitutions=_substitutions(brand),
    )


def build_engine_input(audit: AuditPeriod) -> engine.EngineInput:
    inp = brand_input(audit.brand)
    demand: dict[str, float] = defaultdict(float)
    sold: dict[str, float] = defaultdict(float)
    for s in audit.sales.select_related("menu_item"):
        if s.menu_item:
            demand[s.menu_item.sku] += float(s.qty)
            sold[s.menu_item.sku] += float(s.qty)
    for p in audit.production.select_related("menu_item", "semi_finished", "item"):
        if p.sku:
            demand[p.sku] += float(p.qty)
    values = {"opening": defaultdict(float), "closing": defaultdict(float)}
    for c in audit.stock_counts.all():
        values[c.kind][c.sku] += float(c.value)
    purchases: dict[str, float] = defaultdict(float)
    for p in audit.purchases.all():
        purchases[p.sku] += float(p.value)
    inp.demand = dict(demand)
    inp.sold = dict(sold)
    inp.opening_value = dict(values["opening"])
    inp.closing_value = dict(values["closing"])
    inp.purchases_value = dict(purchases)
    return inp


def run_audit(audit: AuditPeriod) -> engine.EngineResult:
    return engine.run(build_engine_input(audit))


def input_issues(audit: AuditPeriod) -> list[engine.Check]:
    """Checks on the uploaded files that the sheet never made visible.

    Menu items with no price are not flagged: they are treated as free
    (staff and calibration coffee) at RM0.
    """
    unmatched_sales = sorted({s.pos_name for s in audit.sales.filter(menu_item__isnull=True)})
    unknown_purchases = sorted({p.sku for p in audit.purchases.filter(item__isnull=True)})
    unknown_counts = sorted({c.sku for c in audit.stock_counts.filter(item__isnull=True)})
    return [
        engine.Check("pos_unmatched", "POS items not found in the menu", len(unmatched_sales),
                     "Add the item to the menu, or add its POS name as an alias of an existing item.",
                     unmatched_sales),
        engine.Check("purchase_unknown", "Purchase SKUs not in the item master", len(unknown_purchases),
                     "Add the SKU to the central item master and sync, or fix the invoice code.",
                     unknown_purchases),
        engine.Check("count_unknown", "Stock count SKUs not in the item master", len(unknown_counts),
                     "Add the SKU to the central item master and sync, or fix the count sheet.",
                     unknown_counts),
    ]


def menu_costing(brand: Brand) -> list[dict]:
    """23 Menu Master 'Cost Price' and 'COGS %' (no yield, price net of tax)."""
    inp = brand_input(brand)
    tax = float(brand.sales_tax_rate)
    yield_costs = engine.unit_costs(inp)
    rows = []
    for m in MenuItem.objects.filter(brand=brand):
        has_recipe = m.sku in inp.recipes
        cost = engine.recipe_cost_no_yield(inp, m.sku) if has_recipe else None
        net_price = float(m.price) / (1 + tax) if m.price else None
        rows.append({
            "menu": m,
            "has_recipe": has_recipe,
            "cost": cost,
            "cost_with_yield": yield_costs.get(m.sku) if has_recipe else None,
            "cogs_pct": (cost / net_price) if (cost is not None and net_price) else None,
        })
    return rows
