"""Costing engine: a pure-Python port of the COGS & Inventory Audit sheet.

Nothing in this module touches Django, so it can be tested and reused on its
own. ``services.build_engine_input`` turns database rows into an
``EngineInput``; ``run`` returns an ``EngineResult`` with everything the
reports need.

Sheet tab -> code
-----------------
10/11 POS Sales, Production    -> EngineInput.demand
20 Item Master                 -> EngineInput.items
21 SF Master + 22 Recipe Master-> EngineInput.recipes (keyed by parent SKU)
15 Substitutions               -> EngineInput.substitutions
30 Demand / 31 Explode Helper  -> explode() and unit_cost()
40 Consumption Summary         -> EngineResult.consumption
50 Control                     -> EngineResult.checks
60 / 61 / 62 reports           -> dashboard(), variance(), menu_engineering()
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

FOOD_AND_BEVERAGE = "Food & Beverage"

# Default order of the "COGS by category" table (01 Settings, A10:A14).
DEFAULT_CATEGORIES = [
    "Food & Beverage",
    "Packaging",
    "LPG",
    "Cleaning & Chemical",
    "Other",
]

MENU_CLASS_ORDER = {"STAR": 1, "PLOWHORSE": 2, "PUZZLE": 3, "DOG": 4}


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """A purchased / stocked item (20 Item Master)."""

    sku: str
    name: str
    category: str
    cost_per_base: float  # purchase price / pack size
    yield_pct: float = 1.0  # 1.0 == 100 %
    base_uom: str = ""


@dataclass(frozen=True)
class RecipeLine:
    component_sku: str
    qty_per_batch: float


@dataclass(frozen=True)
class Recipe:
    """A menu item or semi-finished item and what goes into one batch of it."""

    parent_sku: str
    batch_yield: float
    lines: tuple[RecipeLine, ...]

    def net_qty_per_unit(self, line: RecipeLine) -> float:
        # 22 Recipe Master H: IF(F=0, 0, G/F)
        if not self.batch_yield:
            return 0.0
        return line.qty_per_batch / self.batch_yield


@dataclass(frozen=True)
class Substitution:
    original_sku: str
    substitute_sku: str
    factor: float  # substitute units per 1 original unit


@dataclass(frozen=True)
class MenuItem:
    sku: str
    name: str
    price: float
    category: str  # menu category: Beverage / Food / Retail ...


@dataclass
class EngineInput:
    items: dict[str, Item]
    recipes: dict[str, Recipe]
    menu: dict[str, MenuItem] = field(default_factory=dict)
    semi_finished: set[str] = field(default_factory=set)
    substitutions: list[Substitution] = field(default_factory=list)
    # External demand (D0): POS qty sold + production/transfer/wastage qty.
    demand: dict[str, float] = field(default_factory=dict)
    # Qty sold through POS only (menu engineering, sales).
    sold: dict[str, float] = field(default_factory=dict)
    # Stock and purchase values per item SKU, in money.
    opening_value: dict[str, float] = field(default_factory=dict)
    purchases_value: dict[str, float] = field(default_factory=dict)
    closing_value: dict[str, float] = field(default_factory=dict)
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    # Safety net against runaway recipes. The sheet stops at 5 levels;
    # the app goes deeper but still flags anything past this.
    max_depth: int = 20


# --------------------------------------------------------------------------
# Substitutions (15 Substitutions, column G)
# --------------------------------------------------------------------------


def validate_substitutions(
    subs: list[Substitution], item_skus: set[str]
) -> list[tuple[Substitution, str]]:
    """Return (substitution, status) using the sheet's rules, in the same order."""
    originals = [s.original_sku for s in subs]
    out = []
    for s in subs:
        if originals.count(s.original_sku) > 1:
            status = "ERROR: DUPLICATE ORIGINAL"
        elif s.original_sku not in item_skus:
            status = "ERROR: ORIGINAL SKU NOT FOUND"
        elif s.substitute_sku not in item_skus:
            status = "ERROR: SUBSTITUTE SKU NOT FOUND"
        elif not s.factor or s.factor <= 0:
            status = "ERROR: CHECK CONVERSION FACTOR"
        elif s.substitute_sku in originals:
            status = "ERROR: CHAIN / REVERSE MAPPING"
        else:
            status = "ACTIVE"
        out.append((s, status))
    return out


def active_substitutions(inp: EngineInput) -> dict[str, Substitution]:
    return {
        s.original_sku: s
        for s, status in validate_substitutions(inp.substitutions, set(inp.items))
        if status == "ACTIVE"
    }


def resolve(line: RecipeLine, net_qty: float, subs: dict[str, Substitution]):
    """31 Explode Helper B/C: redirect a component through an ACTIVE substitution."""
    sub = subs.get(line.component_sku)
    if sub is None:
        return line.component_sku, net_qty, False
    return sub.substitute_sku, net_qty * sub.factor, True


# --------------------------------------------------------------------------
# Explosion and unit cost (30 Demand / 31 Explode Helper)
# --------------------------------------------------------------------------


@dataclass
class Explosion:
    net: dict[str, float]  # total net consumption per SKU, all levels
    max_depth_reached: int
    cycles: set[tuple[str, ...]]
    too_deep: set[str]  # demand SKUs whose tree went past max_depth


def explode(inp: EngineInput, subs: dict[str, Substitution] | None = None) -> Explosion:
    """Push external demand down through recipes to every component.

    Same maths as the sheet's D0..D5 columns, but recursive, so any depth works.
    Each SKU's own demand is counted too (the sheet's 'Total Net Consumption'
    includes D0), which is harmless because menu and SF SKUs carry no cost.
    """
    subs = active_substitutions(inp) if subs is None else subs
    net: dict[str, float] = defaultdict(float)
    cycles: set[tuple[str, ...]] = set()
    too_deep: set[str] = set()
    deepest = 0

    def walk(sku: str, qty: float, path: tuple[str, ...], root: str):
        nonlocal deepest
        net[sku] += qty
        depth = len(path) - 1
        deepest = max(deepest, depth)
        recipe = inp.recipes.get(sku)
        if recipe is None or qty == 0:
            return
        if depth >= inp.max_depth:
            too_deep.add(root)
            return
        for line in recipe.lines:
            comp, per_unit, _ = resolve(line, recipe.net_qty_per_unit(line), subs)
            if comp in path:
                cycles.add(path + (comp,))
                continue
            walk(comp, qty * per_unit, path + (comp,), root)

    for sku, qty in inp.demand.items():
        if qty:
            walk(sku, qty, (sku,), sku)
    return Explosion(dict(net), deepest, cycles, too_deep)


def unit_costs(inp: EngineInput, subs: dict[str, Substitution] | None = None) -> dict[str, float]:
    """Cost of one unit of every SKU (30 Demand S, 'Unit Cost (per portion)').

    Raw item: cost per base unit / yield (gross cost of one net unit).
    Parent: sum of (net qty per unit x substitution factor x component unit cost).
    Cycles contribute zero rather than recursing forever.
    """
    subs = active_substitutions(inp) if subs is None else subs
    memo: dict[str, float] = {}

    def cost(sku: str, stack: frozenset[str]) -> float:
        if sku in memo:
            return memo[sku]
        recipe = inp.recipes.get(sku)
        if recipe is None:
            item = inp.items.get(sku)
            if item is None:
                value = 0.0
            else:
                y = item.yield_pct
                value = item.cost_per_base / y if y and y > 0 else 0.0
        else:
            if sku in stack:
                return 0.0
            value = 0.0
            for line in recipe.lines:
                comp, per_unit, _ = resolve(line, recipe.net_qty_per_unit(line), subs)
                value += per_unit * cost(comp, stack | {sku})
        memo[sku] = value
        return value

    skus = set(inp.items) | set(inp.recipes) | set(inp.menu)
    return {sku: cost(sku, frozenset()) for sku in skus}


def recipe_cost_no_yield(inp: EngineInput, sku: str, subs=None, _stack=frozenset()) -> float:
    """Cost of one unit as 22 Recipe Master / 23 Menu Master show it.

    That view values components at cost per base unit *without* yield loss
    and ignores substitutions. Kept so the menu master's 'Cost Price' and
    'COGS %' match the sheet.
    """
    recipe = inp.recipes.get(sku)
    if recipe is None:
        item = inp.items.get(sku)
        return item.cost_per_base if item else 0.0
    if sku in _stack:
        return 0.0
    total = 0.0
    for line in recipe.lines:
        total += recipe.net_qty_per_unit(line) * recipe_cost_no_yield(
            inp, line.component_sku, subs, _stack | {sku}
        )
    return total


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class ConsumptionRow:
    """One row of 40 Consumption Summary."""

    sku: str
    name: str
    category: str
    net: float
    yield_pct: float
    gross: float
    cost_per_base: float
    theoretical_cost: float
    status: str  # OK / NO USAGE / USED - NO COST / CHECK YIELD
    base_uom: str = ""


@dataclass
class VarianceRow:
    """One row of 61 Variance & Inventory."""

    sku: str
    name: str
    category: str
    has_recipe: bool
    theoretical_cost: float
    opening: float
    purchases: float
    closing: float

    @property
    def actual_cost(self) -> float:
        return self.opening + self.purchases - self.closing

    @property
    def variance(self) -> float:
        return self.actual_cost - self.theoretical_cost

    @property
    def variance_pct(self) -> float | None:
        if not self.theoretical_cost:
            return None
        return self.variance / self.theoretical_cost


@dataclass
class MenuRow:
    """One row of 62 Menu Engineering."""

    sku: str
    name: str
    category: str
    price: float
    cost: float
    units: float
    cat_avg_margin: float = 0.0
    mix_share: float = 0.0  # share of units within its category, 0..1
    popularity_threshold: float = 0.0
    menu_class: str = ""

    @property
    def margin(self) -> float:
        return self.price - self.cost

    @property
    def total_contribution(self) -> float:
        return self.margin * self.units

    @property
    def total_cost(self) -> float:
        return self.cost * self.units

    @property
    def total_sales(self) -> float:
        return self.price * self.units

    @property
    def margin_pct(self) -> float:
        return self.margin / self.price if self.price else 0.0


@dataclass
class Check:
    key: str
    label: str
    count: int
    action: str
    details: list[str] = field(default_factory=list)
    owner: str = ""  # who fixes it: Chef, Procurement or HQ (central item master)
    impact: str = ""  # what it leaves out of the numbers, e.g. "RM 390.00 not counted"

    @property
    def passed(self) -> bool:
        return self.count == 0


@dataclass
class EngineResult:
    consumption: list[ConsumptionRow]
    variance: list[VarianceRow]
    menu: list[MenuRow]
    checks: list[Check]
    unit_cost: dict[str, float]
    net: dict[str, float]
    substitution_status: list[tuple[Substitution, str]]
    categories: list[str]
    total_sales: float

    @property
    def status(self) -> str:
        return "PASS" if all(c.passed for c in self.checks) else "REVIEW"

    # 60 Exec COGS Dashboard ------------------------------------------------

    def theoretical(self, category: str | None = None) -> float:
        return sum(
            r.theoretical_cost
            for r in self.consumption
            if category is None or r.category == category
        )

    def actual(self, category: str | None = None) -> float:
        return sum(
            r.actual_cost for r in self.variance if category is None or r.category == category
        )

    def by_category(self) -> list[dict]:
        rows = []
        for cat in self.categories:
            theo, act = self.theoretical(cat), self.actual(cat)
            rows.append(
                {
                    "category": cat,
                    "theoretical": theo,
                    "actual": act,
                    "variance": act - theo,
                    "pct_of_sales": act / self.total_sales if self.total_sales else 0.0,
                }
            )
        return rows

    def dashboard(self) -> dict:
        theo_fb = self.theoretical(FOOD_AND_BEVERAGE)
        act_fb = self.actual(FOOD_AND_BEVERAGE)
        sales = self.total_sales
        fb_rows = [r for r in self.variance if r.category == FOOD_AND_BEVERAGE]
        # Leakage (used more than the recipes say) and gains (used less) are listed apart:
        # mixing them makes the shares meaningless.
        top = sorted((r for r in fb_rows if r.variance > 0.005), key=lambda r: r.variance, reverse=True)[:10]
        gains = sorted((r for r in fb_rows if r.variance < -0.005), key=lambda r: r.variance)[:5]
        total_fb_var = act_fb - theo_fb
        # Share of all positive (leakage) variance; dividing by the net variance, as the sheet does,
        # gives meaningless signs when other items are under-used.
        leakage = sum(r.variance for r in fb_rows if r.variance > 0)
        return {
            "total_sales": sales,
            "theoretical_fb": theo_fb,
            "theoretical_all": self.theoretical(),
            "actual_fb": act_fb,
            "theoretical_fb_pct": theo_fb / sales if sales else 0.0,
            "actual_fb_pct": act_fb / sales if sales else 0.0,
            "variance_fb": total_fb_var,
            "variance_fb_pct": total_fb_var / theo_fb if theo_fb else 0.0,
            "by_category": self.by_category(),
            "top_leakage": [
                {
                    "sku": r.sku,
                    "name": r.name,
                    "category": r.category,
                    "variance": r.variance,
                    "share": r.variance / leakage if leakage else None,
                }
                for r in top
            ],
            "leakage_total": leakage,
            "top_gains": [
                {"sku": r.sku, "name": r.name, "category": r.category, "variance": r.variance} for r in gains
            ],
        }

    def menu_summary(self) -> list[dict]:
        total = sum(r.total_sales for r in self.menu)
        out = []
        for cls in MENU_CLASS_ORDER:
            rows = [r for r in self.menu if r.menu_class == cls]
            sales = sum(r.total_sales for r in rows)
            out.append(
                {
                    "menu_class": cls,
                    "items": len(rows),
                    "sales": sales,
                    "share": sales / total if total else 0.0,
                }
            )
        return out


def _consumption(inp: EngineInput, net: dict[str, float]) -> list[ConsumptionRow]:
    rows = []
    for sku in sorted(set(net) | set(inp.items)):
        item = inp.items.get(sku)
        if item is None or sku in inp.recipes:
            # Menu and SF SKUs carry no cost of their own. Some central item master rows
            # are menu or SF items with a typed price; when a recipe exists, the recipe's
            # components are what is consumed, so costing the row too would double count.
            continue
        n = net.get(sku, 0.0)
        y = item.yield_pct
        gross = n / y if y and y > 0 else n
        theo = gross * item.cost_per_base
        if n > 0:
            if not item.cost_per_base:
                status = "USED - NO COST"
            elif y is None or y <= 0 or y > 1:
                status = "CHECK YIELD"
            else:
                status = "OK"
        else:
            status = "NO USAGE"
        rows.append(
            ConsumptionRow(sku, item.name, item.category, n, y, gross, item.cost_per_base, theo, status,
                           item.base_uom)
        )
    return rows


def _variance(inp: EngineInput, consumption: list[ConsumptionRow]) -> list[VarianceRow]:
    theo = {r.sku: r.theoretical_cost for r in consumption}
    components = {line.component_sku for r in inp.recipes.values() for line in r.lines}
    rows = []
    for sku, item in sorted(inp.items.items()):
        row = VarianceRow(
            sku=sku,
            name=item.name,
            category=item.category,
            has_recipe=sku in components,
            theoretical_cost=theo.get(sku, 0.0),
            opening=inp.opening_value.get(sku, 0.0),
            purchases=inp.purchases_value.get(sku, 0.0),
            closing=inp.closing_value.get(sku, 0.0),
        )
        if row.theoretical_cost or row.opening or row.purchases or row.closing:
            rows.append(row)
    return rows


def _menu_engineering(inp: EngineInput, unit_cost: dict[str, float]) -> list[MenuRow]:
    """Kasavana-Smith classification, per menu category (62 Menu Engineering X:AF)."""
    rows = [
        MenuRow(
            sku=sku,
            name=m.name,
            category=m.category or "Other",
            price=m.price,
            cost=unit_cost.get(sku, 0.0),
            units=inp.sold.get(sku, 0.0),
        )
        for sku, m in inp.menu.items()
        if inp.sold.get(sku, 0.0) > 0
    ]
    by_cat: dict[str, list[MenuRow]] = defaultdict(list)
    for r in rows:
        by_cat[r.category].append(r)
    for cat_rows in by_cat.values():
        units = sum(r.units for r in cat_rows)
        contribution = sum(r.total_contribution for r in cat_rows)
        avg_margin = contribution / units if units else 0.0
        threshold = 0.7 / len(cat_rows)
        for r in cat_rows:
            r.cat_avg_margin = avg_margin
            r.mix_share = r.units / units if units else 0.0
            r.popularity_threshold = threshold
            high_margin = r.margin > avg_margin
            high_pop = r.mix_share > threshold
            r.menu_class = (
                "STAR" if high_margin and high_pop
                else "PLOWHORSE" if high_pop
                else "PUZZLE" if high_margin
                else "DOG"
            )
    rows.sort(key=lambda r: (MENU_CLASS_ORDER[r.menu_class], -r.total_contribution))
    return rows


def _checks(
    inp: EngineInput,
    consumption: list[ConsumptionRow],
    explosion: Explosion,
    sub_status: list[tuple[Substitution, str]],
    variance: list[VarianceRow] = (),
) -> list[Check]:
    known = set(inp.items) | set(inp.recipes) | inp.semi_finished
    unmapped, bad_qty = [], []
    for parent, recipe in inp.recipes.items():
        if not recipe.batch_yield or recipe.batch_yield <= 0:
            bad_qty.append(f"{parent}: batch yield is zero")
        for line in recipe.lines:
            if line.component_sku not in known:
                unmapped.append(f"{parent} -> {line.component_sku}")
            if not line.qty_per_batch or line.qty_per_batch <= 0:
                bad_qty.append(f"{parent} -> {line.component_sku}")
    no_recipe = [
        sku for sku, qty in inp.demand.items()
        if qty > 0 and sku in inp.menu and sku not in inp.recipes
    ]
    depth = sorted(explosion.too_deep) + [" -> ".join(c) for c in sorted(explosion.cycles)]
    # An item whose category isn't one of the report's categories would fall out of every COGS total.
    # It is shown as its own row in "COGS by category", and flagged here so it gets fixed at the source.
    odd_category = sorted(
        f"{r.sku} ({r.category or 'blank'})" for r in variance if r.category not in inp.categories
    )
    return [
        Check("unmapped", "Unmapped recipe components", len(unmapped),
              "Fix the component so it matches an item or semi-finished SKU.", unmapped, "Chef"),
        Check("qty", "Recipe lines missing / zero qty", len(bad_qty),
              "Enter a positive quantity (and batch yield) on the flagged recipe line.", bad_qty, "Chef"),
        Check("no_cost", "Items used but not costed",
              sum(r.status == "USED - NO COST" for r in consumption),
              "Enter purchase price and pack size for the item in the item master.",
              [r.sku for r in consumption if r.status == "USED - NO COST"], "HQ (central item master)"),
        Check("yield", "Items with invalid yield",
              sum(r.status == "CHECK YIELD" for r in consumption),
              "Set yield between 0% and 100% in the item master.",
              [r.sku for r in consumption if r.status == "CHECK YIELD"], "HQ (central item master)"),
        Check("no_recipe", "Menu items sold with no recipe", len(no_recipe),
              "Add a recipe for the sold menu item, otherwise its cost is zero.", sorted(no_recipe), "Chef",
              f"{sum(inp.demand[s] for s in no_recipe):,.0f} units costed at zero" if no_recipe else ""),
        Check("depth", "Recipe loops or nesting too deep", len(depth),
              "A recipe contains itself somewhere down the chain, or nests deeper than the limit.", depth, "Chef"),
        Check("subs", "Invalid substitution mappings",
              sum(st.startswith("ERROR") for _, st in sub_status),
              "Each original SKU must map to one valid substitute; no reverse or chain mappings.",
              [f"{s.original_sku} -> {s.substitute_sku}: {st}" for s, st in sub_status if st != "ACTIVE"], "Chef"),
        Check("category", "Items with an unknown category", len(odd_category),
              "Set the item's category in the central item master to one of: "
              + ", ".join(inp.categories) + ". Until then it is not counted in Food & Beverage.",
              odd_category, "HQ (central item master)"),
    ]


def run(inp: EngineInput) -> EngineResult:
    sub_status = validate_substitutions(inp.substitutions, set(inp.items))
    subs = {s.original_sku: s for s, st in sub_status if st == "ACTIVE"}
    explosion = explode(inp, subs)
    costs = unit_costs(inp, subs)
    consumption = _consumption(inp, explosion.net)
    variance = _variance(inp, consumption)
    menu = _menu_engineering(inp, costs)
    total_sales = sum(qty * inp.menu[sku].price for sku, qty in inp.sold.items() if sku in inp.menu)
    return EngineResult(
        consumption=consumption,
        variance=variance,
        menu=menu,
        checks=_checks(inp, consumption, explosion, sub_status, variance),
        unit_cost=costs,
        net=explosion.net,
        substitution_status=sub_status,
        categories=list(inp.categories) + sorted(
            {r.category for r in variance if r.category not in inp.categories}
        ),
        total_sales=total_sales,
    )
