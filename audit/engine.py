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

# "NO RECIPE": sold but has no recipe, so its cost is unknown (shown as 0). It is kept out of the
# category averages and thresholds, which it would otherwise distort, and never classed STAR.
# "FREE": sold at no price (staff or calibration coffee, cakeage). Nothing to earn, so it is kept out
# of the classification too, instead of being counted as a DOG.
# Smallest RM amount per item for the "used but in no recipe" and "never bought" checks.
MATERIAL = 50.0

MENU_CLASS_ORDER = {"STAR": 1, "PLOWHORSE": 2, "PUZZLE": 3, "DOG": 4, "NO RECIPE": 5, "FREE": 6}


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
    # Semi-finished items counted in stock, in the unit their recipe makes (g, ml, pcs), per SF SKU.
    # They are broken down through their recipes into the supplier items they were made from, and those
    # items' stock values go up by that much (see stock_from_semi_finished).
    opening_sf_qty: dict[str, float] = field(default_factory=dict)
    closing_sf_qty: dict[str, float] = field(default_factory=dict)
    # Revenue from B2B sales to sister brands (invoice line amounts). Their portions are in demand.
    b2b_sales: float = 0.0
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


def explode(inp: EngineInput, subs: dict[str, Substitution] | None = None,
            demand: dict[str, float] | None = None) -> Explosion:
    """Push external demand down through recipes to every component.

    ``demand`` defaults to the audit's sales and production; a semi-finished stock count is
    pushed through the same way (see stock_from_semi_finished).

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

    for sku, qty in (inp.demand if demand is None else demand).items():
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


def stock_from_semi_finished(
    inp: EngineInput, counted: dict[str, float], subs: dict[str, Substitution] | None = None
) -> dict[str, float]:
    """Value of the supplier items held inside counted semi-finished stock, per item SKU.

    1.2 kg of caramel sauce in the fridge is the sugar, cream and butter it was made from. The count
    is pushed down through the recipes exactly like a sale (batch yield, nested semi-finished items,
    substitutions), and each item is valued the way consumption is: net / yield x cost per base unit.
    """
    subs = active_substitutions(inp) if subs is None else subs
    counted = {sku: qty for sku, qty in counted.items() if qty and sku in inp.recipes}
    if not counted:
        return {}
    net = explode(inp, subs, demand=counted).net
    out: dict[str, float] = {}
    for sku, n in net.items():
        item = inp.items.get(sku)
        if item is None or sku in inp.recipes or not n:
            continue
        y = item.yield_pct
        out[sku] = (n / y if y and y > 0 else n) * item.cost_per_base
    return out


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
    cost_per_base: float = 0.0
    base_uom: str = ""
    # The part of opening / closing that is this item held inside counted semi-finished stock.
    # Already included in opening and closing.
    opening_sf: float = 0.0
    closing_sf: float = 0.0

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

    @property
    def variance_qty(self) -> float | None:
        """The variance in the item's base unit (g, ml, pcs), at today's cost per unit."""
        return self.variance / self.cost_per_base if self.cost_per_base else None


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
    has_recipe: bool = True

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
    def profit_category(self) -> str:
        """H if the margin is above its category's average (62 col N), else L. Blank with no recipe."""
        if self.menu_class in ("NO RECIPE", "FREE"):
            return ""
        return "H" if self.margin > self.cat_avg_margin else "L"

    @property
    def popularity_category(self) -> str:
        """H if its share of the category's units is above the popularity threshold (62 col P), else L."""
        if self.menu_class in ("NO RECIPE", "FREE"):
            return ""
        return "H" if self.mix_share > self.popularity_threshold else "L"

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
    total_sales: float  # POS sales + B2B sales
    pos_sales: float = 0.0
    b2b_sales: float = 0.0
    # Semi-finished stock counts with their value at today's recipe cost:
    # [{"kind": "opening"/"closing", "sku", "qty", "value"}]
    sf_stock: list[dict] = field(default_factory=list)

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
        gains = sorted((r for r in fb_rows if r.variance < -0.005), key=lambda r: r.variance)[:10]
        total_fb_var = act_fb - theo_fb
        # Share of all positive (leakage) variance; dividing by the net variance, as the sheet does,
        # gives meaningless signs when other items are under-used.
        leakage = sum(r.variance for r in fb_rows if r.variance > 0)
        # Gross underuse (negative, the sheet's "negative offsets"): leakage + offsets = net variance.
        offsets = sum(r.variance for r in fb_rows if r.variance < 0)

        def line(r, total):
            return {"sku": r.sku, "name": r.name, "category": r.category, "variance": r.variance,
                    "opening": r.opening, "purchases": r.purchases, "closing": r.closing,
                    "actual": r.actual_cost, "theoretical": r.theoretical_cost,
                    "share": r.variance / total if total else None}
        return {
            "total_sales": sales,
            "pos_sales": self.pos_sales,
            "b2b_sales": self.b2b_sales,
            "theoretical_fb": theo_fb,
            "theoretical_all": self.theoretical(),
            "actual_fb": act_fb,
            "theoretical_fb_pct": theo_fb / sales if sales else 0.0,
            "actual_fb_pct": act_fb / sales if sales else 0.0,
            "variance_fb": total_fb_var,
            "variance_fb_pct": total_fb_var / theo_fb if theo_fb else 0.0,
            # The tolerance is tested on this one (the chef's rule, Nov 2026): variance as a share of sales.
            "variance_fb_pct_sales": total_fb_var / sales if sales else 0.0,
            "by_category": self.by_category(),
            "top_leakage": [line(r, leakage) for r in top],
            "leakage_total": leakage,
            "top_gains": [line(r, offsets) for r in gains],
            "offsets_total": offsets,
            # Categories no recipe drives (packaging, LPG, cleaning...): their actual cost is the number to watch.
            "non_recipe": [c for c in self.by_category() if c["category"] != FOOD_AND_BEVERAGE],
        }

    def menu_summary(self) -> list[dict]:
        total = sum(r.total_sales for r in self.menu)
        out = []
        for cls in MENU_CLASS_ORDER:
            rows = [r for r in self.menu if r.menu_class == cls]
            if cls in ("NO RECIPE", "FREE") and not rows:
                continue
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


def _variance(inp: EngineInput, consumption: list[ConsumptionRow],
              subs: dict[str, Substitution] | None = None) -> list[VarianceRow]:
    theo = {r.sku: r.theoretical_cost for r in consumption}
    components = {line.component_sku for r in inp.recipes.values() for line in r.lines}
    open_sf = stock_from_semi_finished(inp, inp.opening_sf_qty, subs)
    close_sf = stock_from_semi_finished(inp, inp.closing_sf_qty, subs)
    rows = []
    for sku, item in sorted(inp.items.items()):
        row = VarianceRow(
            sku=sku,
            name=item.name,
            category=item.category,
            has_recipe=sku in components,
            theoretical_cost=theo.get(sku, 0.0),
            opening=inp.opening_value.get(sku, 0.0) + open_sf.get(sku, 0.0),
            purchases=inp.purchases_value.get(sku, 0.0),
            closing=inp.closing_value.get(sku, 0.0) + close_sf.get(sku, 0.0),
            cost_per_base=item.cost_per_base,
            base_uom=item.base_uom,
            opening_sf=open_sf.get(sku, 0.0),
            closing_sf=close_sf.get(sku, 0.0),
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
            has_recipe=sku in inp.recipes,
        )
        for sku, m in inp.menu.items()
        if inp.sold.get(sku, 0.0) > 0
    ]
    by_cat: dict[str, list[MenuRow]] = defaultdict(list)
    for r in rows:
        if not r.has_recipe:
            r.menu_class = "NO RECIPE"
        elif not r.price or r.price <= 0:
            r.menu_class = "FREE"
        else:
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
    # A semi-finished item must have a recipe with a batch yield: without one it costs nothing in sales
    # and its stock can't be broken down into the items it was made from.
    sf_used = {line.component_sku for r in inp.recipes.values() for line in r.lines} | set(inp.demand) \
        | set(inp.opening_sf_qty) | set(inp.closing_sf_qty)
    sf_no_recipe = sorted(sku for sku in inp.semi_finished & sf_used if sku not in inp.recipes)
    # An item whose category isn't one of the report's categories would fall out of every COGS total.
    # It is shown as its own row in "COGS by category", and flagged here so it gets fixed at the source.
    odd_category = sorted(
        f"{r.sku} ({r.category or 'blank'})" for r in variance if r.category not in inp.categories
    )
    # Three patterns from the chef's October 2026 review that are almost always a data problem, not usage.
    # Semi-finished and menu SKUs are left out: they aren't bought stock.
    # The last two only from RM 50 an item: below that it is small change (water, a pinch of salt with no
    # stock line) and not worth holding the month in Review for.
    stock_rows = [r for r in variance if r.sku not in inp.semi_finished and r.sku not in inp.menu]
    negative = sorted((r for r in stock_rows if r.actual_cost <= -0.01), key=lambda r: r.actual_cost)
    unused = sorted((r for r in stock_rows if r.category == FOOD_AND_BEVERAGE and not r.has_recipe
                     and r.actual_cost >= MATERIAL), key=lambda r: -r.actual_cost)
    never_bought = sorted((r for r in stock_rows if r.theoretical_cost >= MATERIAL and not (r.opening or r.purchases
                           or r.closing)), key=lambda r: -r.theoretical_cost)
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
        Check("sf_no_recipe", "Semi-finished items with no recipe", len(sf_no_recipe),
              "Every semi-finished item needs a recipe and a batch yield. Add the recipe; until then it "
              "costs nothing and its stock counts as nothing.", sf_no_recipe, "Chef"),
        Check("depth", "Recipe loops or nesting too deep", len(depth),
              "A recipe contains itself somewhere down the chain, or nests deeper than the limit.", depth, "Chef"),
        Check("subs", "Invalid substitution mappings",
              sum(st.startswith("ERROR") for _, st in sub_status),
              "Each original SKU must map to one valid substitute; no reverse or chain mappings.",
              [f"{s.original_sku} -> {s.substitute_sku}: {st}" for s, st in sub_status if st != "ACTIVE"], "Chef"),
        Check("negative_actual", "Closing stock above opening + purchases", len(negative),
              "Actual cost comes out below zero: more is in the closing count than was ever there or bought. "
              "Usually a purchase or inter-company invoice that wasn't uploaded, an opening count that is too "
              "low, or a closing count in the wrong unit. Check the invoices and both counts for these items.",
              [f"{r.sku} {r.name}: opening {r.opening:,.2f} + purchases {r.purchases:,.2f} - closing "
               f"{r.closing:,.2f} = {r.actual_cost:,.2f}" for r in negative], "Procurement",
              f"RM {-sum(r.actual_cost for r in negative):,.2f} of cost that can't be real" if negative else ""),
        Check("used_no_recipe", "F&B items used but in no recipe (RM 50 or more)", len(unused),
              "These were bought and used, but no recipe uses them, so all of their cost shows as leakage. "
              "Add them to the recipes that use them, or move them to the right category in the item master "
              "if they aren't food.",
              [f"{r.sku} {r.name}: actual {r.actual_cost:,.2f}" for r in unused], "Chef",
              f"RM {sum(r.actual_cost for r in unused):,.2f} counted as leakage" if unused else ""),
        Check("never_bought", "Used by recipes but never bought or counted (RM 50 or more)", len(never_bought),
              "The recipes say these were used, but there is no opening stock, purchase or closing stock for "
              "them. Usually a missing purchase or inter-company invoice, or a recipe line pointing at the "
              "wrong SKU (the item really used has a different code).",
              [f"{r.sku} {r.name}: theoretical {r.theoretical_cost:,.2f}" for r in never_bought], "Procurement",
              f"RM {sum(r.theoretical_cost for r in never_bought):,.2f} shown as underuse" if never_bought else ""),
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
    variance = _variance(inp, consumption, subs)
    menu = _menu_engineering(inp, costs)
    pos_sales = sum(qty * inp.menu[sku].price for sku, qty in inp.sold.items() if sku in inp.menu)
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
        total_sales=pos_sales + inp.b2b_sales,
        pos_sales=pos_sales,
        b2b_sales=inp.b2b_sales,
        sf_stock=[
            {"kind": kind, "sku": sku, "qty": qty, "value": qty * costs.get(sku, 0.0)}
            for kind, counted in (("opening", inp.opening_sf_qty), ("closing", inp.closing_sf_qty))
            for sku, qty in sorted(counted.items())
        ],
    )
