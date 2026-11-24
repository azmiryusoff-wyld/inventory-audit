"""Engine tests on a small hand-checked example.

Latte = 18 g coffee + 200 ml milk + 20 g syrup.
Syrup is made in 1,000 g batches from 500 g sugar.
Milk yields 90%.
"""
from django.test import SimpleTestCase

from audit import engine as e

ITEMS = {
    "COFFEE": e.Item("COFFEE", "Coffee beans", "Food & Beverage", 0.10, 1.0, "G"),
    "MILK": e.Item("MILK", "Fresh milk", "Food & Beverage", 0.01, 0.9, "ML"),
    "OAT": e.Item("OAT", "Oat milk", "Food & Beverage", 0.02, 1.0, "ML"),
    "SUGAR": e.Item("SUGAR", "Sugar", "Food & Beverage", 0.005, 1.0, "G"),
    "CUP": e.Item("CUP", "Paper cup", "Packaging", 0.30, 1.0, "PCS"),
}
RECIPES = {
    "LATTE": e.Recipe("LATTE", 1, (e.RecipeLine("COFFEE", 18), e.RecipeLine("MILK", 200), e.RecipeLine("SYRUP", 20))),
    "SYRUP": e.Recipe("SYRUP", 1000, (e.RecipeLine("SUGAR", 500),)),
    "ESPRESSO": e.Recipe("ESPRESSO", 1, (e.RecipeLine("COFFEE", 18),)),
}
MENU = {
    "LATTE": e.MenuItem("LATTE", "Latte - Hot", 15.0, "Beverage"),
    "ESPRESSO": e.MenuItem("ESPRESSO", "Espresso - Hot", 10.0, "Beverage"),
    "COOKIE": e.MenuItem("COOKIE", "Cookie", 9.0, "Food"),
}


def make(**kw):
    base = dict(items=dict(ITEMS), recipes=dict(RECIPES), menu=dict(MENU), semi_finished={"SYRUP"},
                demand={"LATTE": 10}, sold={"LATTE": 10})
    base.update(kw)
    return e.EngineInput(**base)


class ExplodeTests(SimpleTestCase):
    def test_net_consumption_through_semi_finished(self):
        net = e.explode(make()).net
        self.assertAlmostEqual(net["COFFEE"], 180)
        self.assertAlmostEqual(net["MILK"], 2000)
        self.assertAlmostEqual(net["SYRUP"], 200)
        self.assertAlmostEqual(net["SUGAR"], 100)

    def test_theoretical_cost_applies_yield(self):
        result = e.run(make())
        rows = {r.sku: r for r in result.consumption}
        self.assertAlmostEqual(rows["MILK"].gross, 2000 / 0.9)
        self.assertAlmostEqual(rows["MILK"].theoretical_cost, 2000 / 0.9 * 0.01)
        self.assertAlmostEqual(result.theoretical(), 18 + 2000 / 0.9 * 0.01 + 0.5)

    def test_unit_cost_matches_theoretical_per_portion(self):
        inp = make()
        cost = e.unit_costs(inp)["LATTE"]
        self.assertAlmostEqual(cost, 1.8 + 200 * 0.01 / 0.9 + 20 * 0.5 * 0.005)
        self.assertAlmostEqual(cost * 10, e.run(inp).theoretical())

    def test_recipe_cost_without_yield_matches_menu_master(self):
        self.assertAlmostEqual(e.recipe_cost_no_yield(make(), "LATTE"), 1.8 + 2.0 + 0.05)

    def test_substitution_redirects_and_scales(self):
        inp = make(substitutions=[e.Substitution("MILK", "OAT", 1.1)])
        result = e.run(inp)
        self.assertAlmostEqual(result.net["OAT"], 2200)
        self.assertNotIn("MILK", result.net)
        self.assertAlmostEqual(result.theoretical(), 18 + 44 + 0.5)
        self.assertAlmostEqual(result.unit_cost["LATTE"], 1.8 + 200 * 1.1 * 0.02 + 0.05)

    def test_invalid_substitutions_are_ignored_and_flagged(self):
        subs = [e.Substitution("MILK", "OAT", 1), e.Substitution("OAT", "MILK", 1),
                e.Substitution("SUGAR", "NOPE", 1), e.Substitution("COFFEE", "SUGAR", 0)]
        status = [st for _, st in e.validate_substitutions(subs, set(ITEMS))]
        self.assertEqual(status, ["ERROR: CHAIN / REVERSE MAPPING", "ERROR: CHAIN / REVERSE MAPPING",
                                  "ERROR: SUBSTITUTE SKU NOT FOUND", "ERROR: CHECK CONVERSION FACTOR"])
        result = e.run(make(substitutions=subs))
        self.assertAlmostEqual(result.net["MILK"], 2000)  # nothing applied
        self.assertEqual(next(c for c in result.checks if c.key == "subs").count, 4)

    def test_duplicate_original_is_an_error(self):
        subs = [e.Substitution("MILK", "OAT", 1), e.Substitution("MILK", "SUGAR", 1)]
        self.assertTrue(all(st == "ERROR: DUPLICATE ORIGINAL" for _, st in e.validate_substitutions(subs, set(ITEMS))))

    def test_deeper_than_five_levels(self):
        recipes = {"L0": e.Recipe("L0", 1, (e.RecipeLine("L1", 2),))}
        for i in range(1, 7):
            recipes[f"L{i}"] = e.Recipe(f"L{i}", 1, (e.RecipeLine(f"L{i + 1}", 2),))
        recipes["L7"] = e.Recipe("L7", 1, (e.RecipeLine("SUGAR", 1),))
        result = e.run(e.EngineInput(items=dict(ITEMS), recipes=recipes, demand={"L0": 1}))
        self.assertAlmostEqual(result.net["SUGAR"], 2 ** 7)
        self.assertEqual(next(c for c in result.checks if c.key == "depth").count, 0)

    def test_recipe_loop_is_caught(self):
        recipes = {"A": e.Recipe("A", 1, (e.RecipeLine("B", 1),)), "B": e.Recipe("B", 1, (e.RecipeLine("A", 1),))}
        result = e.run(e.EngineInput(items=dict(ITEMS), recipes=recipes, demand={"A": 1}))
        self.assertEqual(next(c for c in result.checks if c.key == "depth").count, 1)
        self.assertEqual(result.unit_cost["A"], 0)


class VarianceAndDashboardTests(SimpleTestCase):
    def test_actual_and_variance(self):
        inp = make(opening_value={"COFFEE": 50, "CUP": 10}, purchases_value={"COFFEE": 100, "CUP": 30},
                   closing_value={"COFFEE": 120, "CUP": 15})
        result = e.run(inp)
        rows = {r.sku: r for r in result.variance}
        self.assertAlmostEqual(rows["COFFEE"].actual_cost, 30)
        self.assertAlmostEqual(rows["COFFEE"].variance, 12)
        self.assertAlmostEqual(rows["COFFEE"].variance_pct, 12 / 18)
        self.assertTrue(rows["COFFEE"].has_recipe)
        self.assertFalse(rows["CUP"].has_recipe)
        self.assertNotIn("OAT", rows)  # no activity, not listed

        dash = result.dashboard()
        self.assertAlmostEqual(dash["total_sales"], 150)
        self.assertAlmostEqual(dash["actual_fb"], 30)
        cats = {c["category"]: c for c in dash["by_category"]}
        self.assertAlmostEqual(cats["Packaging"]["actual"], 25)
        self.assertAlmostEqual(cats["Packaging"]["pct_of_sales"], 25 / 150)
        self.assertEqual(dash["top_leakage"][0]["sku"], "COFFEE")

    def test_production_adds_demand_but_not_sales(self):
        result = e.run(make(demand={"LATTE": 10, "SYRUP": 1000}))
        self.assertAlmostEqual(result.net["SUGAR"], 100 + 500)
        self.assertAlmostEqual(result.total_sales, 150)


class MenuEngineeringTests(SimpleTestCase):
    def test_classification_within_category(self):
        menu = {
            "A": e.MenuItem("A", "A", 20, "Beverage"),  # high margin, popular -> STAR
            "B": e.MenuItem("B", "B", 8, "Beverage"),   # low margin, popular -> PLOWHORSE
            "C": e.MenuItem("C", "C", 25, "Beverage"),  # high margin, rare -> PUZZLE
            "D": e.MenuItem("D", "D", 6, "Beverage"),   # low margin, rare -> DOG
            "F": e.MenuItem("F", "F", 10, "Food"),      # alone: margin equals the average, 100% mix -> PLOWHORSE
        }
        recipes = {k: e.Recipe(k, 1, (e.RecipeLine("COFFEE", 20),)) for k in menu}  # cost 2.00 each
        sold = {"A": 100, "B": 100, "C": 5, "D": 5, "F": 3}
        result = e.run(e.EngineInput(items=dict(ITEMS), recipes=recipes, menu=menu, demand=sold, sold=sold))
        classes = {r.sku: r.menu_class for r in result.menu}
        self.assertEqual(classes, {"A": "STAR", "B": "PLOWHORSE", "C": "PUZZLE", "D": "DOG", "F": "PLOWHORSE"})
        self.assertEqual([r.sku for r in result.menu][:2], ["A", "B"])
        self.assertAlmostEqual(result.menu[0].popularity_threshold, 0.7 / 4)


class CheckTests(SimpleTestCase):
    def test_all_checks(self):
        items = dict(ITEMS)
        items["FREE"] = e.Item("FREE", "No price", "Food & Beverage", 0, 1)
        items["BADY"] = e.Item("BADY", "Bad yield", "Food & Beverage", 1, 1.5)
        recipes = dict(RECIPES)
        recipes["X"] = e.Recipe("X", 1, (e.RecipeLine("GHOST", 1), e.RecipeLine("FREE", 1),
                                         e.RecipeLine("BADY", 1), e.RecipeLine("SUGAR", 0)))
        menu = dict(MENU)
        menu["X"] = e.MenuItem("X", "X", 5, "Food")
        result = e.run(make(items=items, recipes=recipes, menu=menu,
                            demand={"X": 1, "COOKIE": 4}, sold={"X": 1, "COOKIE": 4}))
        counts = {c.key: c.count for c in result.checks}
        self.assertEqual(counts, {"unmapped": 1, "qty": 1, "no_cost": 1, "yield": 1,
                                  "no_recipe": 1, "depth": 0, "subs": 0})
        self.assertEqual(result.status, "REVIEW")
        self.assertEqual(e.run(make()).status, "PASS")


class MatchesEocSheetTests(SimpleTestCase):
    """Menu costs the live EOC workbook shows in 23 Menu Master (Nov 2026), rebuilt from its recipe lines."""

    def test_americano_and_black_eye(self):
        items = {sku: e.Item(sku, sku, "Food & Beverage", cost, 1.0) for sku, cost in {
            "PBE0006": 0.090, "H2O": 0.01 / 120, "ICE": 0.010, "PPP0228": 0.195, "PPP0212": 0.070,
            "PPP0232": 0.192, "PPP0168": 0.095, "SF04-INPUTS": 8.20,
        }.items()}
        recipes = {
            "EOC-001": e.Recipe("EOC-001", 1, (e.RecipeLine("PBE0006", 20), e.RecipeLine("H2O", 120),
                                               e.RecipeLine("ICE", 5), e.RecipeLine("PPP0228", 1),
                                               e.RecipeLine("PPP0212", 1))),
            "EOC-006": e.Recipe("EOC-006", 1, (e.RecipeLine("PBE0006", 25), e.RecipeLine("PPP0232", 1),
                                               e.RecipeLine("PPP0168", 1), e.RecipeLine("EOC-SF-04", 150))),
            # EOC-BATCH BREW: RM8.20 of inputs per 1,000 g batch.
            "EOC-SF-04": e.Recipe("EOC-SF-04", 1000, (e.RecipeLine("SF04-INPUTS", 1),)),
        }
        inp = e.EngineInput(items=items, recipes=recipes)
        self.assertAlmostEqual(e.recipe_cost_no_yield(inp, "EOC-001"), 2.13, delta=0.01)  # sheet: 2.13
        self.assertAlmostEqual(e.recipe_cost_no_yield(inp, "EOC-006"), 3.77, delta=0.01)  # sheet: 3.77


class ItemMasterRowWithRecipeTests(SimpleTestCase):
    """The central item master also lists menu and SF items with a typed price (category 'Menu')."""

    def test_recipe_wins_and_is_not_double_counted(self):
        items = dict(ITEMS)
        items["LATTE"] = e.Item("LATTE", "Latte - Hot", "Menu", 99.0, 1.0)
        items["SYRUP"] = e.Item("SYRUP", "Syrup", "Food & Beverage", 5.0, 1.0)
        result = e.run(make(items=items))
        self.assertAlmostEqual(result.theoretical(), 18 + 2000 / 0.9 * 0.01 + 0.5)
        self.assertNotIn("LATTE", {r.sku for r in result.consumption})
        self.assertAlmostEqual(result.unit_cost["LATTE"], e.unit_costs(make())["LATTE"])


class LeakageShareTests(SimpleTestCase):
    def test_share_is_of_positive_leakage(self):
        inp = make(purchases_value={"COFFEE": 48, "MILK": 0, "SUGAR": 1.5}, closing_value={})
        # coffee +30 over, sugar +1 over, milk -22.2 under
        dash = e.run(inp).dashboard()
        shares = {r["sku"]: r["share"] for r in dash["top_leakage"]}
        self.assertAlmostEqual(shares["COFFEE"], 30 / 31)
        self.assertTrue(shares["MILK"] < 0)
