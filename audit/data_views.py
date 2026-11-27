"""Menus, recipes, semi-finished items, substitutions and the item master, in the app's own screens
(findings C-24, C-31, C-32, C-44). The admin stays for brands, outlets and users."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from . import access, engine, history, services
from .models import Item, MenuItem, PosAlias, Recipe, RecipeLine, SemiFinished, Substitution


def _need(user, perm):
    if not user.has_perm(f"audit.{perm}"):
        raise PermissionDenied


def _brand_ctx(request, brand, section):
    return {"brand": brand, "section": section, "brands": access.brands_for(request.user)}


@login_required
def data_home(request):
    brands = access.brands_for(request.user).annotate(
        n_menu=Count("menu_items", distinct=True), n_recipes=Count("recipes", distinct=True))
    last_sync = Item.objects.aggregate(t=Max("synced_at"))["t"]
    return render(request, "audit/data/home.html", {
        "brand_rows": brands, "last_sync": last_sync, "n_items": Item.objects.count(), "section": "home",
        "n_subs": Substitution.objects.count(),
    })


# Item master (read-only: it comes from the central workbook) -------------------------------------------

@login_required
def item_list(request):
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    items = Item.objects.all()
    if q:
        items = items.filter(Q(sku__icontains=q) | Q(name__icontains=q) | Q(supplier__icontains=q))
    if category:
        items = items.filter(category=category)
    page = Paginator(items, 100).get_page(request.GET.get("page"))
    return render(request, "audit/data/items.html", {
        "page": page, "q": q, "category": category, "section": "items",
        "categories": Item.objects.values_list("category", flat=True).distinct().order_by("category"),
        "last_sync": Item.objects.aggregate(t=Max("synced_at"))["t"],
        "query": "&".join(f"{k}={v}" for k, v in (("q", q), ("category", category)) if v),
    })


def where_used(item: Item, brands) -> list[dict]:
    """Every recipe that uses the item, directly or through a semi-finished item, and substitutions."""
    rows = []
    direct = RecipeLine.objects.filter(item=item, recipe__brand__in=brands).select_related(
        "recipe__menu_item", "recipe__semi_finished", "recipe__brand")
    seen_sf = set()
    for line in direct:
        rows.append({"recipe": line.recipe, "qty": line.qty_per_batch, "via": ""})
        if line.recipe.semi_finished_id:
            seen_sf.add(line.recipe.semi_finished_id)
    frontier = set(seen_sf)
    depth = 0
    while frontier and depth < 6:  # menu items that use a semi-finished item that uses this item
        lines = list(RecipeLine.objects.filter(semi_finished__in=frontier, recipe__brand__in=brands).select_related(
            "recipe__menu_item", "recipe__semi_finished", "recipe__brand", "semi_finished"))
        frontier = set()
        for line in lines:
            rows.append({"recipe": line.recipe, "qty": line.qty_per_batch, "via": line.semi_finished.sku})
            if line.recipe.semi_finished_id and line.recipe.semi_finished_id not in seen_sf:
                seen_sf.add(line.recipe.semi_finished_id)
                frontier.add(line.recipe.semi_finished_id)
        depth += 1
    return rows


@login_required
def item_detail(request, sku):
    item = Item.objects.filter(sku=sku).first()
    if item is None:
        raise Http404("No such item.")
    brands = access.brands_for(request.user)
    subs = Substitution.objects.filter(Q(original=item) | Q(substitute=item)).select_related(
        "original", "substitute", "brand")
    return render(request, "audit/data/item_detail.html", {
        "item": item, "used": where_used(item, brands), "subs": subs, "section": "items",
    })


# Menu items -----------------------------------------------------------------------------------------

class MenuItemForm(forms.ModelForm):
    aliases = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Other POS names",
        help_text="One per line: older names or variants on the POS report that count as this item.")

    class Meta:
        model = MenuItem
        fields = ["sku", "name", "price", "category", "note", "active"]
        labels = {"sku": "SKU", "name": "POS name", "price": "Price (SST included)"}

    def __init__(self, *a, brand, can_alias, **kw):
        super().__init__(*a, **kw)
        self.instance.brand = brand
        if self.instance.pk:
            self.fields["aliases"].initial = "\n".join(self.instance.aliases.values_list("pos_name", flat=True))
        if not can_alias:
            self.fields["aliases"].disabled = True
            self.fields["aliases"].help_text = "Only the chef can link POS names to menu items."
        self.fields["category"].widget.attrs["list"] = "menu-categories"

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        clash = MenuItem.objects.filter(brand=self.instance.brand, sku__iexact=sku).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"{self.instance.brand} already has a menu item {sku}.")
        return sku


MENU_FIELDS = ["sku", "name", "price", "category", "note", "active"]


@login_required
def menu_list(request, brand_code):
    brand = access.get_brand(request.user, brand_code)
    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "")
    items = MenuItem.objects.filter(brand=brand).select_related("recipe").annotate(n_alias=Count("aliases"))
    if q:
        items = items.filter(Q(sku__icontains=q) | Q(name__icontains=q) | Q(aliases__pos_name__icontains=q)).distinct()
    if show == "no-recipe":
        items = items.filter(recipe__isnull=True, active=True)
    elif show == "inactive":
        items = items.filter(active=False)
    return render(request, "audit/data/menu.html", {
        **_brand_ctx(request, brand, "menu"), "items": items, "q": q, "show": show,
        "can_edit": request.user.has_perm("audit.change_menuitem"),
    })


@login_required
def menu_edit(request, brand_code, pk=None):
    brand = access.get_brand(request.user, brand_code)
    item = MenuItem.objects.filter(brand=brand, pk=pk).first() if pk else MenuItem(brand=brand)
    if item is None:
        raise Http404("No such menu item.")
    _need(request.user, "change_menuitem" if pk else "add_menuitem")
    can_alias = request.user.has_perm("audit.add_posalias")
    before = history.snapshot(item, MENU_FIELDS) if pk else {}
    old_aliases = sorted(item.aliases.values_list("pos_name", flat=True)) if pk else []
    form = MenuItemForm(request.POST or None, instance=item, brand=brand, can_alias=can_alias)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            item = form.save()
            changes = history.diff(before, history.snapshot(item, MENU_FIELDS))
            if can_alias:
                new = sorted({a.strip() for a in form.cleaned_data["aliases"].splitlines() if a.strip()})
                if new != old_aliases:
                    item.aliases.exclude(pos_name__in=new).delete()
                    for name in set(new) - set(old_aliases):
                        PosAlias.objects.create(menu_item=item, pos_name=name)
                    changes["other POS names"] = [", ".join(old_aliases), ", ".join(new)]
            history.log(request.user, "change" if pk else "add", "Menu item", item, changes=changes)
        messages.success(request, f"Menu item {item} saved.")
        return redirect("data_menu", brand.code)
    return render(request, "audit/data/form.html", {
        **_brand_ctx(request, brand, "menu"), "form": form, "title": f"Menu item {item}" if pk else "New menu item",
        "back": reverse("data_menu", args=[brand.code]),
        "datalists": {"menu-categories": [(c, "") for c in MenuItem.objects.filter(brand=brand).exclude(category="")
                                          .values_list("category", flat=True).distinct()]},
        "recipe_link": (reverse("data_recipe_edit", args=[brand.code, item.recipe.pk]) if pk and hasattr(item, "recipe")
                        else reverse("data_recipe_new", args=[brand.code]) + f"?menu={item.pk}" if pk else ""),
    })


# Semi-finished items ---------------------------------------------------------------------------------

class SemiFinishedForm(forms.ModelForm):
    class Meta:
        model = SemiFinished
        fields = ["sku", "name", "output_uom", "active"]
        labels = {"sku": "SKU", "output_uom": "Unit it is made in (g, ml, portion)"}

    def __init__(self, *a, brand, **kw):
        super().__init__(*a, **kw)
        self.instance.brand = brand

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        if SemiFinished.objects.filter(brand=self.instance.brand, sku__iexact=sku).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"{self.instance.brand} already has a semi-finished item {sku}.")
        return sku


@login_required
def sf_list(request, brand_code):
    brand = access.get_brand(request.user, brand_code)
    q = request.GET.get("q", "").strip()
    items = SemiFinished.objects.filter(brand=brand).select_related("recipe").annotate(n_used=Count("used_in"))
    if q:
        items = items.filter(Q(sku__icontains=q) | Q(name__icontains=q))
    return render(request, "audit/data/semi_finished.html", {
        **_brand_ctx(request, brand, "sf"), "items": items, "q": q,
        "can_edit": request.user.has_perm("audit.change_semifinished"),
    })


class SemiFinishedRecipeForm(forms.Form):
    """The recipe a new semi-finished item is created with: a semi-finished item can't exist without one."""

    batch_yield = forms.DecimalField(
        label="Batch makes", min_value=0.000001, max_digits=18, decimal_places=6,
        help_text="How much one batch makes, in the unit above: 1000 for a 1 kg batch in grams. Required.")
    note = forms.CharField(required=False, max_length=300, label="Recipe note")


@login_required
def sf_edit(request, brand_code, pk=None):
    brand = access.get_brand(request.user, brand_code)
    sf = SemiFinished.objects.filter(brand=brand, pk=pk).first() if pk else SemiFinished(brand=brand)
    if sf is None:
        raise Http404("No such semi-finished item.")
    _need(request.user, "change_semifinished" if pk else "add_semifinished")
    if not pk:
        return _sf_new(request, brand, sf)
    fields = ["sku", "name", "output_uom", "active"]
    before = history.snapshot(sf, fields) if pk else {}
    form = SemiFinishedForm(request.POST or None, instance=sf, brand=brand)
    if request.method == "POST" and form.is_valid():
        sf = form.save()
        history.log(request.user, "change" if pk else "add", "Semi-finished item", sf,
                    changes=history.diff(before, history.snapshot(sf, fields)))
        messages.success(request, f"Semi-finished item {sf} saved.")
        return redirect("data_sf", brand.code)
    return render(request, "audit/data/form.html", {
        **_brand_ctx(request, brand, "sf"), "form": form,
        "title": f"Semi-finished item {sf}" if pk else "New semi-finished item",
        "back": reverse("data_sf", args=[brand.code]),
        "recipe_link": (reverse("data_recipe_edit", args=[brand.code, sf.recipe.pk]) if pk and hasattr(sf, "recipe")
                        else reverse("data_recipe_new", args=[brand.code]) + f"?sf={sf.pk}" if pk else ""),
    })


def _sf_new(request, brand, sf):
    """A new semi-finished item is saved together with its recipe (batch yield and at least one line)."""
    _need(request.user, "add_recipe")
    form = SemiFinishedForm(request.POST or None, instance=sf, brand=brand)
    form.fields["output_uom"].required = True
    recipe_form = SemiFinishedRecipeForm(request.POST or None)
    lines = RecipeLineFormSet(request.POST or None, form_kwargs={"brand": brand}, prefix="lines")
    if request.method == "POST" and form.is_valid() and recipe_form.is_valid() and lines.is_valid():
        kept = [f.cleaned_data for f in lines if f.cleaned_data and not f.cleaned_data.get("empty")]
        if not kept:
            messages.error(request, "Add at least one ingredient line: a semi-finished item must have a recipe.")
        elif any(d["sf"] is not None and d["sf"].sku.lower() == form.cleaned_data["sku"].lower() for d in kept):
            messages.error(request, f"{form.cleaned_data['sku']} can't be an ingredient of itself.")
        else:
            with transaction.atomic():
                sf = form.save()
                recipe = Recipe.objects.create(brand=brand, semi_finished=sf,
                                               batch_yield=recipe_form.cleaned_data["batch_yield"],
                                               note=recipe_form.cleaned_data["note"])
                RecipeLine.objects.bulk_create(
                    RecipeLine(recipe=recipe, item=d["item"], semi_finished=d["sf"], qty_per_batch=d["qty"])
                    for d in kept)
                from .signals import invalidate

                invalidate()
                history.log(request.user, "add", "Semi-finished item", sf,
                            changes=history.diff({}, history.snapshot(sf, ["sku", "name", "output_uom", "active"])))
                history.log(request.user, "add", "Recipe", recipe, changes={
                    "batch makes": [None, history._plain(recipe.batch_yield)], "lines": [None, _recipe_lines_text(recipe)]})
            messages.success(request, f"Semi-finished item {sf} saved with its recipe.")
            return redirect("data_sf", brand.code)
    components = list(SemiFinished.objects.filter(brand=brand).values_list("sku", "name")) + list(
        Item.objects.filter(active=True).values_list("sku", "name"))
    return render(request, "audit/data/sf_new.html", {
        **_brand_ctx(request, brand, "sf"), "form": form, "recipe_form": recipe_form, "lines": lines,
        "components": components, "back": reverse("data_sf", args=[brand.code]),
    })


# Recipes ---------------------------------------------------------------------------------------------

class RecipeForm(forms.Form):
    parent = forms.ChoiceField(label="Recipe for")
    batch_yield = forms.DecimalField(
        label="Batch makes", min_value=0.000001, max_digits=18, decimal_places=6, initial=1,
        help_text="Units of the parent one batch makes: 1 for a single portion, 1000 for a 1 kg batch in grams.")
    note = forms.CharField(required=False, max_length=300)

    def __init__(self, *a, brand, recipe, **kw):
        super().__init__(*a, **kw)
        taken_menu = Recipe.objects.filter(brand=brand, menu_item__isnull=False).exclude(pk=recipe.pk)
        taken_sf = Recipe.objects.filter(brand=brand, semi_finished__isnull=False).exclude(pk=recipe.pk)
        menu = MenuItem.objects.filter(brand=brand).exclude(pk__in=taken_menu.values("menu_item")).order_by("sku")
        sf = SemiFinished.objects.filter(brand=brand).exclude(pk__in=taken_sf.values("semi_finished")).order_by("sku")
        self.fields["parent"].choices = [("", "Choose a menu item or semi-finished item")] + [
            ("Menu items", [(f"menu:{m.pk}", f"{m.sku} {m.name}") for m in menu]),
            ("Semi-finished items", [(f"sf:{s.pk}", f"{s.sku} {s.name}") for s in sf]),
        ]
        self.brand = brand

    def clean_parent(self):
        kind, _, pk = self.cleaned_data["parent"].partition(":")
        model = MenuItem if kind == "menu" else SemiFinished
        obj = model.objects.filter(brand=self.brand, pk=pk or 0).first()
        if obj is None:
            raise forms.ValidationError("Pick a menu item or semi-finished item of this brand.")
        return obj


class RecipeLineForm(forms.Form):
    component = forms.CharField(label="SKU", required=False)
    qty = forms.DecimalField(label="Quantity per batch", required=False, max_digits=18, decimal_places=6)

    def __init__(self, *a, brand=None, **kw):
        super().__init__(*a, **kw)
        self.brand = brand
        self.fields["component"].widget.attrs.update({"list": "component-skus", "autocomplete": "off"})
        self.fields["qty"].widget.attrs.update({"step": "any", "style": "width:9em"})

    def clean(self):
        data = super().clean()
        sku, qty = (data.get("component") or "").strip(), data.get("qty")
        if not sku and qty is None:
            data["empty"] = True
            return data
        if not sku:
            raise forms.ValidationError("Enter the SKU for this quantity.")
        if qty is None or qty <= 0:
            self.add_error("qty", "Enter a quantity above zero.")
        sf = SemiFinished.objects.filter(brand=self.brand, sku__iexact=sku).first()
        item = None if sf else Item.objects.filter(sku__iexact=sku).first()
        if sf is None and item is None:
            other = SemiFinished.objects.filter(sku__iexact=sku).exclude(brand=self.brand).select_related("brand").first()
            if other:
                self.add_error("component", f"{sku} is a semi-finished item of {other.brand}, not {self.brand}.")
            else:
                self.add_error("component", f"{sku} isn't an item master or {self.brand} semi-finished SKU.")
        data["sf"], data["item"] = sf, item
        return data


RecipeLineFormSet = forms.formset_factory(RecipeLineForm, extra=3)


def _recipe_lines_text(recipe) -> str:
    return "; ".join(f"{line.component_sku} x {history._plain(line.qty_per_batch)}"
                     for line in recipe.lines.select_related("item", "semi_finished").order_by("pk"))


@login_required
def recipe_list(request, brand_code):
    brand = access.get_brand(request.user, brand_code)
    q = request.GET.get("q", "").strip()
    recipes = Recipe.objects.filter(brand=brand).select_related("menu_item", "semi_finished").annotate(
        n_lines=Count("lines"))
    if q:  # matches the parent or any ingredient: "where is DEMO-MILK used?"
        recipes = recipes.filter(
            Q(menu_item__sku__icontains=q) | Q(menu_item__name__icontains=q) | Q(semi_finished__sku__icontains=q)
            | Q(semi_finished__name__icontains=q) | Q(lines__item__sku__icontains=q)
            | Q(lines__item__name__icontains=q) | Q(lines__semi_finished__sku__icontains=q)).distinct()
    costs = engine.unit_costs(services.brand_input(brand))
    rows = [{"recipe": r, "cost": costs.get(r.parent_sku)} for r in recipes]
    missing = MenuItem.objects.filter(brand=brand, active=True, recipe__isnull=True).count()
    return render(request, "audit/data/recipes.html", {
        **_brand_ctx(request, brand, "recipes"), "rows": rows, "q": q, "missing": missing,
        "can_edit": request.user.has_perm("audit.change_recipe"), "currency": brand.currency,
    })


@login_required
def recipe_edit(request, brand_code, pk=None):
    brand = access.get_brand(request.user, brand_code)
    recipe = Recipe.objects.filter(brand=brand, pk=pk).first() if pk else Recipe(brand=brand)
    if recipe is None:
        raise Http404("No such recipe.")
    can_edit = request.user.has_perm("audit.change_recipe" if pk else "audit.add_recipe")
    if request.method == "POST" and not can_edit:
        raise PermissionDenied
    if pk:
        initial = {"parent": f"menu:{recipe.menu_item_id}" if recipe.menu_item_id else f"sf:{recipe.semi_finished_id}",
                   "batch_yield": history._plain(recipe.batch_yield), "note": recipe.note}
        line_initial = [{"component": line.component_sku, "qty": history._plain(line.qty_per_batch)}
                        for line in recipe.lines.select_related("item", "semi_finished").order_by("pk")]
    else:
        parent = (f"menu:{request.GET['menu']}" if request.GET.get("menu")
                  else f"sf:{request.GET['sf']}" if request.GET.get("sf") else "")
        # A semi-finished item's batch yield must be typed in, never assumed to be 1.
        initial, line_initial = {"parent": parent, "batch_yield": None if parent.startswith("sf:") else 1}, []
    form = RecipeForm(request.POST or None, brand=brand, recipe=recipe, initial=initial)
    lines = RecipeLineFormSet(request.POST or None, initial=line_initial, form_kwargs={"brand": brand}, prefix="lines")
    if request.method == "POST" and form.is_valid() and lines.is_valid():
        kept = [f.cleaned_data for f in lines if f.cleaned_data and not f.cleaned_data.get("empty")]
        parent = form.cleaned_data["parent"]
        if not kept:
            messages.error(request, "Add at least one ingredient line.")
        elif pk and recipe.semi_finished_id and parent != recipe.semi_finished:
            messages.error(request, f"{recipe.semi_finished.sku} would be left with no recipe. A semi-finished "
                                    "item must always have one: create a new recipe for the other item instead.")
        elif any(d["sf"] is not None and d["sf"] == parent for d in kept):
            messages.error(request, f"{parent.sku} can't be an ingredient of itself.")
        else:
            before = {"recipe for": str(recipe.parent) if pk else None,
                      "batch makes": history._plain(recipe.batch_yield) if pk else None,
                      "note": recipe.note if pk else None, "lines": _recipe_lines_text(recipe) if pk else None}
            with transaction.atomic():
                recipe.menu_item = parent if isinstance(parent, MenuItem) else None
                recipe.semi_finished = parent if isinstance(parent, SemiFinished) else None
                recipe.batch_yield, recipe.note = form.cleaned_data["batch_yield"], form.cleaned_data["note"]
                recipe.save()
                recipe.lines.all().delete()
                RecipeLine.objects.bulk_create(
                    RecipeLine(recipe=recipe, item=d["item"], semi_finished=d["sf"], qty_per_batch=d["qty"])
                    for d in kept)
                after = {"recipe for": str(recipe.parent), "batch makes": history._plain(recipe.batch_yield),
                         "note": recipe.note, "lines": _recipe_lines_text(recipe)}
                from .signals import invalidate

                invalidate()  # bulk_create sends no signal
                history.log(request.user, "change" if pk else "add", "Recipe", recipe, changes=history.diff(before, after))
            check = [c for c in engine.run(services.brand_input(brand)).checks
                     if c.key == "depth" and any(recipe.parent_sku in d for d in c.details)]
            if check:
                messages.warning(request, "This recipe makes a loop: " + "; ".join(check[0].details))
            messages.success(request, f"{recipe} saved.")
            return redirect("data_recipe_edit", brand.code, recipe.pk)
    rows = []
    if pk:
        inp = services.brand_input(brand)
        costs = engine.unit_costs(inp)
        for line in recipe.lines.select_related("item", "semi_finished").order_by("pk"):
            comp = line.item or line.semi_finished
            unit = (line.item.base_uom if line.item else line.semi_finished.output_uom) or ""
            cost = costs.get(line.component_sku)
            if line.item:
                item = inp.items.get(line.item.sku)
                cost = item.cost_per_base / item.yield_pct if item and item.yield_pct else None
            rows.append({"line": line, "name": comp.name, "unit": unit, "cost": cost,
                         "total": float(line.qty_per_batch) * cost if cost is not None else None})
        portion = costs.get(recipe.parent_sku)
    else:
        portion = None
    components = list(SemiFinished.objects.filter(brand=brand).values_list("sku", "name")) + list(
        Item.objects.filter(active=True).values_list("sku", "name"))
    return render(request, "audit/data/recipe_form.html", {
        **_brand_ctx(request, brand, "recipes"), "form": form, "lines": lines, "recipe": recipe, "rows": rows,
        "portion": portion, "components": components, "can_edit": can_edit, "currency": brand.currency,
    })


@login_required
def recipe_delete(request, brand_code, pk):
    brand = access.get_brand(request.user, brand_code)
    _need(request.user, "delete_recipe")
    recipe = Recipe.objects.filter(brand=brand, pk=pk).first()
    if recipe is None or request.method != "POST":
        raise Http404("No such recipe.")
    if recipe.semi_finished_id:
        messages.error(request, f"{recipe.semi_finished.sku} is a semi-finished item, which must always have a "
                                "recipe. Change the recipe instead, or set the item inactive.")
        return redirect("data_recipe_edit", brand.code, recipe.pk)
    text = _recipe_lines_text(recipe)
    history.log(request.user, "delete", "Recipe", recipe, changes={"lines": [text, None]})
    recipe.delete()
    messages.success(request, f"{recipe} deleted.")
    return redirect("data_recipes", brand.code)


# Substitutions ---------------------------------------------------------------------------------------

class SubstitutionForm(forms.ModelForm):
    original_sku = forms.CharField(label="Swap this item (SKU)")
    substitute_sku = forms.CharField(label="For this item (SKU)")

    class Meta:
        model = Substitution
        fields = ["brand", "original_sku", "substitute_sku", "factor", "note", "active"]
        labels = {"brand": "Brand"}

    def __init__(self, *a, user, **kw):
        super().__init__(*a, **kw)
        self.fields["brand"].queryset = access.brands_for(user)
        self.fields["brand"].empty_label = "Every brand"
        for f in ("original_sku", "substitute_sku"):
            self.fields[f].widget.attrs["list"] = "item-skus"
        if self.instance.pk:
            self.fields["original_sku"].initial = self.instance.original.sku
            self.fields["substitute_sku"].initial = self.instance.substitute.sku

    def _item(self, field):
        sku = self.cleaned_data[field].strip()
        item = Item.objects.filter(sku__iexact=sku).first()
        if item is None:
            raise forms.ValidationError(f"{sku} isn't in the item master.")
        return item

    def clean_original_sku(self):
        self.instance.original = self._item("original_sku")
        return self.instance.original.sku

    def clean_substitute_sku(self):
        self.instance.substitute = self._item("substitute_sku")
        return self.instance.substitute.sku


SUB_FIELDS = ["brand", "original", "substitute", "factor", "note", "active"]


@login_required
def sub_list(request):
    brands = access.brands_for(request.user)
    subs = Substitution.objects.filter(Q(brand__isnull=True) | Q(brand__in=brands)).select_related(
        "original", "substitute", "brand").order_by("original__sku")
    status = {}
    for brand in brands:  # what the calculation makes of each mapping
        inp = services.brand_input(brand)
        for s, st in engine.validate_substitutions(inp.substitutions, set(inp.items)):
            status.setdefault((s.original_sku, s.substitute_sku), st)
    rows = [{"sub": s, "status": status.get((s.original.sku, s.substitute.sku), "ACTIVE" if s.active else "OFF")}
            for s in subs]
    return render(request, "audit/data/substitutions.html", {
        "rows": rows, "section": "subs", "can_edit": request.user.has_perm("audit.change_substitution"),
    })


@login_required
def sub_edit(request, pk=None):
    brands = access.brands_for(request.user)
    sub = (Substitution.objects.filter(Q(brand__isnull=True) | Q(brand__in=brands), pk=pk).first()
           if pk else Substitution())
    if sub is None:
        raise Http404("No such substitution.")
    _need(request.user, "change_substitution" if pk else "add_substitution")
    before = history.snapshot(sub, SUB_FIELDS) if pk else {}
    form = SubstitutionForm(request.POST or None, instance=sub, user=request.user)
    if request.method == "POST" and form.is_valid():
        sub = form.save()
        history.log(request.user, "change" if pk else "add", "Substitution", sub,
                    changes=history.diff(before, history.snapshot(sub, SUB_FIELDS)))
        messages.success(request, f"Substitution {sub} saved.")
        return redirect("data_subs")
    return render(request, "audit/data/form.html", {
        "form": form, "section": "subs", "title": f"Substitution {sub}" if pk else "New substitution",
        "back": reverse("data_subs"),
        "datalists": {"item-skus": Item.objects.filter(active=True).values_list("sku", "name")},
    })


@login_required
def change_log(request):
    """Every change to menus, recipes and substitutions, and every upload and line fix."""
    entries = ChangeLogQuery(request).page
    return render(request, "audit/data/changes.html", {"page": entries, "section": "changes",
                                                       "q": request.GET.get("q", "")})


class ChangeLogQuery:
    def __init__(self, request):
        from .models import ChangeLog

        qs = ChangeLog.objects.select_related("user", "audit__outlet__brand")
        if not access.sees_all(request.user):
            qs = qs.filter(audit__in=access.audits_for(request.user))
        q = request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(object_repr__icontains=q) | Q(what__icontains=q) | Q(user__username__icontains=q)
                           | Q(note__icontains=q))
        self.page = Paginator(qs, 100).get_page(request.GET.get("page"))
        for e in self.page.object_list:
            e.summary = history.describe(e)
