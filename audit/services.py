"""Glue between the database and the pure engine."""
from collections import Counter, defaultdict
from dataclasses import asdict

from django.db import transaction
from django.utils import timezone

from . import engine
from .models import (
    AuditPeriod, Brand, Item, MenuItem, Recipe, SemiFinished, Substitution,
)

SNAPSHOT_VERSION = 1

# The files every audit needs before its numbers mean anything. Inter-company purchases are optional.
REQUIRED_INPUTS = {
    "pos": "POS item sales report",
    "purchases": "Supplier purchases",
    "opening": "Opening stock count",
    "closing": "Closing stock count",
}


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


def engine_input_to_json(inp: engine.EngineInput) -> dict:
    return {
        "items": [asdict(i) for i in inp.items.values()],
        "recipes": [
            {"parent_sku": r.parent_sku, "batch_yield": r.batch_yield,
             "lines": [[line.component_sku, line.qty_per_batch] for line in r.lines]}
            for r in inp.recipes.values()
        ],
        "menu": [asdict(m) for m in inp.menu.values()],
        "semi_finished": sorted(inp.semi_finished),
        "substitutions": [asdict(s) for s in inp.substitutions],
        "demand": inp.demand,
        "sold": inp.sold,
        "opening_value": inp.opening_value,
        "purchases_value": inp.purchases_value,
        "closing_value": inp.closing_value,
        "categories": inp.categories,
    }


def engine_input_from_json(data: dict) -> engine.EngineInput:
    return engine.EngineInput(
        items={i["sku"]: engine.Item(**i) for i in data["items"]},
        recipes={
            r["parent_sku"]: engine.Recipe(
                r["parent_sku"], r["batch_yield"], tuple(engine.RecipeLine(c, q) for c, q in r["lines"])
            )
            for r in data["recipes"]
        },
        menu={m["sku"]: engine.MenuItem(**m) for m in data["menu"]},
        semi_finished=set(data["semi_finished"]),
        substitutions=[engine.Substitution(**x) for x in data["substitutions"]],
        demand=data["demand"],
        sold=data["sold"],
        opening_value=data["opening_value"],
        purchases_value=data["purchases_value"],
        closing_value=data["closing_value"],
        categories=data["categories"],
    )


def run_audit(audit: AuditPeriod) -> engine.EngineResult:
    """A signed-off audit is worked out from its sign-off snapshot, an open one from today's data."""
    if audit.locked and audit.snapshot:
        return engine.run(engine_input_from_json(audit.snapshot["engine_input"]))
    return engine.run(build_engine_input(audit))


def input_counts(audit: AuditPeriod) -> dict[str, int]:
    return {
        "pos": audit.sales.count(),
        "purchases": audit.purchases.filter(source="supplier").count(),
        "interco": audit.purchases.filter(source="interco").count(),
        "opening": audit.stock_counts.filter(kind="opening").count(),
        "closing": audit.stock_counts.filter(kind="closing").count(),
    }


def audit_status(audit: AuditPeriod, result: engine.EngineResult, issues: list[engine.Check],
                 counts: dict[str, int] | None = None) -> dict:
    """The one status shown on the dashboard, in Trends, in the audit list and in the Excel export.

    INCOMPLETE  a required file is missing
    REVIEW      a data check failed (fix the data first; the variance can't be trusted yet)
    OUTSIDE     F&B variance is outside the audit's tolerance, either direction
    PASS        complete, every check passes, variance within tolerance
    """
    if audit.locked and audit.snapshot and "status" in audit.snapshot:
        return audit.snapshot["status"]
    counts = counts if counts is not None else input_counts(audit)
    dash = result.dashboard()
    missing = [label for key, label in REQUIRED_INPUTS.items() if not counts.get(key)]
    failed = [c.label for c in result.checks + issues if not c.passed]
    tolerance = float(audit.variance_tolerance)
    var_pct = dash["variance_fb_pct"]
    outside = abs(var_pct) > tolerance
    if missing:
        code, reasons = "INCOMPLETE", ["Missing: " + ", ".join(missing)]
    elif failed:
        code, reasons = "REVIEW", ["Data checks to fix: " + ", ".join(failed)]
    elif outside:
        code, reasons = "OUTSIDE", [f"F&B variance {var_pct:.1%} is outside the {tolerance:.1%} tolerance"]
    else:
        code, reasons = "PASS", [f"Complete, all checks pass, variance {var_pct:.1%} within {tolerance:.1%}"]
    labels = {"INCOMPLETE": "Incomplete", "REVIEW": "Review", "OUTSIDE": "Outside tolerance", "PASS": "Pass"}
    return {"code": code, "label": labels[code], "reasons": reasons, "within_tolerance": not outside}


def audit_report(audit: AuditPeriod) -> tuple[engine.EngineResult, list[engine.Check], dict]:
    """Result, upload checks and status for one audit, frozen if it is signed off."""
    result = run_audit(audit)
    if audit.locked and audit.snapshot:
        issues = [engine.Check(**c) for c in audit.snapshot["input_issues"]]
    else:
        issues = input_issues(audit)
    return result, issues, audit_status(audit, result, issues)


@transaction.atomic
def sign_off(audit: AuditPeriod, user) -> AuditPeriod:
    audit = AuditPeriod.objects.select_for_update().get(pk=audit.pk)
    if audit.locked:
        return audit
    inp = build_engine_input(audit)
    result = engine.run(inp)
    issues = input_issues(audit)
    status = audit_status(audit, result, issues)
    audit.snapshot = {
        "version": SNAPSHOT_VERSION,
        "engine_input": engine_input_to_json(inp),
        "input_issues": [asdict(c) for c in issues],
        "status": status,
        "dashboard": _jsonable(result.dashboard()),
    }
    audit.locked, audit.locked_at, audit.locked_by = True, timezone.now(), user
    audit.save(update_fields=["snapshot", "locked", "locked_at", "locked_by"])
    return audit


@transaction.atomic
def unlock(audit: AuditPeriod) -> AuditPeriod:
    audit = AuditPeriod.objects.select_for_update().get(pk=audit.pk)
    audit.locked, audit.locked_at, audit.locked_by, audit.snapshot = False, None, None, None
    audit.save(update_fields=["snapshot", "locked", "locked_at", "locked_by"])
    return audit


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def input_issues(audit: AuditPeriod) -> list[engine.Check]:
    """Checks on the uploaded files that the sheet never made visible.

    Menu items with no price are not flagged: they are treated as free
    (staff and calibration coffee) at RM0.
    """
    unmatched_sales = sorted({s.pos_name for s in audit.sales.filter(menu_item__isnull=True)})
    unknown_purchases = sorted({p.sku for p in audit.purchases.filter(item__isnull=True)})
    unknown_counts = sorted({c.sku for c in audit.stock_counts.filter(item__isnull=True)})
    purchases = list(audit.purchases.all())
    year, month = (int(x) for x in audit.period.split("-"))
    out_of_period = sorted(
        f"{p.invoice_number or 'no invoice no.'} {p.invoice_date:%d/%m/%Y} {p.sku} ({p.value:,.2f})"
        for p in purchases
        if p.invoice_date and (p.invoice_date.year, p.invoice_date.month) != (year, month)
    )
    dupes = Counter(
        (p.source, p.invoice_number, p.invoice_date, p.sku, p.qty, p.value) for p in purchases
    )
    duplicate_lines = sorted(
        f"{inv or 'no invoice no.'} {sku} qty {qty:,.2f} value {value:,.2f} x{n}"
        for (_, inv, _, sku, qty, value), n in dupes.items() if n > 1
    )
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
        engine.Check("purchase_period", f"Invoices dated outside {audit.period}", len(out_of_period),
                     "These invoices are counted in this month's actual COGS. Remove them from the file and "
                     "upload again if they belong to another month.", out_of_period),
        engine.Check("purchase_duplicate", "Possible duplicate invoice lines", len(duplicate_lines),
                     "The same invoice line appears more than once and is counted each time. Remove the "
                     "extra copies from the file and upload again, unless they really are separate lines.",
                     duplicate_lines),
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
