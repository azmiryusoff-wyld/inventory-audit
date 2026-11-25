"""Database tables.

Group-wide:  Item (synced from the central item master workbook), Brand, Outlet.
Per brand:   SemiFinished, MenuItem (+ PosAlias), Recipe + RecipeLine, Substitution.
Per audit:   AuditPeriod (one outlet, one month) with its sales, production,
             purchase and stock-count lines. One AuditPeriod replaces one copy
             of the workbook.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

DEFAULT_CATEGORIES = ["Food & Beverage", "Packaging", "LPG", "Cleaning & Chemical", "Other"]


def money(**kw):
    return models.DecimalField(max_digits=16, decimal_places=4, default=Decimal("0"), **kw)


def quantity(**kw):
    return models.DecimalField(max_digits=18, decimal_places=6, default=Decimal("0"), **kw)


class Brand(models.Model):
    code = models.CharField(max_length=20, unique=True, help_text="Short code, e.g. EOC")
    name = models.CharField(max_length=200)
    currency = models.CharField(max_length=8, default="RM")
    sales_tax_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal("0.06"),
        help_text="Tax included in menu prices (0.06 = 6% SST). Only used for menu cost %.",
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class Outlet(models.Model):
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, related_name="outlets")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=200)
    pos_location = models.CharField(
        max_length=100, blank=True,
        help_text="Location name in the POS report, e.g. EOC_KLCC-O2. Needed when one report covers several outlets.",
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["brand__code", "code"]
        unique_together = [("brand", "code")]

    def __str__(self):
        return f"{self.brand.code} {self.name}"


class Item(models.Model):
    """20 Item Master. Shared by every brand; synced from the central workbook."""

    sku = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=300)
    category = models.CharField(max_length=40, default="Food & Beverage")
    pack_size = quantity(help_text="Base units per pack")
    base_uom = models.CharField(max_length=20, blank=True)
    pack_uom = models.CharField(max_length=20, blank=True)
    purchase_price = money(help_text="Price per pack")
    yield_pct = models.DecimalField(
        max_digits=7, decimal_places=4, default=Decimal("1"),
        help_text="1 = 100%. Gross consumption = net / yield.",
    )
    intermediate_pack_size = models.DecimalField(null=True, blank=True, max_digits=18, decimal_places=6)
    intermediate_uom = models.CharField(max_length=20, blank=True)
    supplier = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sku"]

    def __str__(self):
        return f"{self.sku} {self.name}"

    @property
    def cost_per_base(self) -> Decimal:
        if not self.pack_size:
            return Decimal("0")
        return self.purchase_price / self.pack_size


class SemiFinished(models.Model):
    """21 SF Master: sauces, syrups, batch brew... made in-house."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="semi_finished")
    sku = models.CharField(max_length=40)
    name = models.CharField(max_length=300)
    output_uom = models.CharField(max_length=20, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["brand__code", "sku"]
        unique_together = [("brand", "sku")]
        verbose_name = "semi-finished item"

    def __str__(self):
        return f"{self.sku} {self.name}"


class MenuItem(models.Model):
    """23 Menu Master."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="menu_items")
    sku = models.CharField(max_length=40)
    name = models.CharField(max_length=300, help_text="Exactly as the POS report's Full Name")
    price = models.DecimalField(
        null=True, blank=True, max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0"), "A price can't be negative.")],
        help_text="POS price including SST. Leave empty for a free item.",
    )
    category = models.CharField(max_length=60, blank=True, help_text="Beverage, Food, Retail...")
    note = models.CharField(max_length=300, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["brand__code", "sku"]
        unique_together = [("brand", "sku")]

    def __str__(self):
        return f"{self.sku} {self.name}"


class PosAlias(models.Model):
    """Extra POS names that count as a menu item (a renamed item, a variant)."""

    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="aliases")
    pos_name = models.CharField(max_length=300)

    class Meta:
        verbose_name = "POS alias"
        verbose_name_plural = "POS aliases"

    def __str__(self):
        return self.pos_name


class Recipe(models.Model):
    """22 Recipe Master, grouped by parent. The parent is a menu item or an SF item."""

    # Always the parent's brand (set on save): a recipe filed under another brand would cost nothing.
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="recipes", editable=False)
    menu_item = models.OneToOneField(
        MenuItem, null=True, blank=True, on_delete=models.CASCADE, related_name="recipe"
    )
    semi_finished = models.OneToOneField(
        SemiFinished, null=True, blank=True, on_delete=models.CASCADE, related_name="recipe"
    )
    batch_yield = models.DecimalField(
        max_digits=18, decimal_places=6, default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.000001"))],
        help_text="Units of the parent one batch makes (1 for a single portion).",
    )
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["brand__code", "menu_item__sku", "semi_finished__sku"]

    def clean(self):
        if bool(self.menu_item_id) == bool(self.semi_finished_id):
            raise ValidationError("A recipe belongs to exactly one menu item or one semi-finished item.")
        self.brand_id = self.parent.brand_id

    def save(self, *args, **kwargs):
        if self.parent:
            self.brand_id = self.parent.brand_id
        super().save(*args, **kwargs)

    @property
    def parent(self):
        return self.menu_item or self.semi_finished

    @property
    def parent_sku(self):
        return self.parent.sku if self.parent else ""

    def __str__(self):
        return f"Recipe: {self.parent}"


class RecipeLine(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, null=True, blank=True, on_delete=models.PROTECT)
    semi_finished = models.ForeignKey(
        SemiFinished, null=True, blank=True, on_delete=models.PROTECT, related_name="used_in"
    )
    qty_per_batch = models.DecimalField(
        max_digits=18, decimal_places=6, help_text="In the component's base unit",
        validators=[MinValueValidator(Decimal("0.000001"), "Enter a quantity above zero.")],
    )

    def clean(self):
        if bool(self.item_id) == bool(self.semi_finished_id):
            raise ValidationError("Pick either an item or a semi-finished item.")
        try:
            recipe = self.recipe
        except Recipe.DoesNotExist:
            recipe = None
        if self.semi_finished_id and recipe is not None and recipe.parent is not None:
            if self.semi_finished.brand_id != recipe.parent.brand_id:
                raise ValidationError({"semi_finished": f"{self.semi_finished.sku} belongs to {self.semi_finished.brand}, "
                                                        f"not {recipe.parent.brand}."})

    @property
    def component_sku(self):
        comp = self.item or self.semi_finished
        return comp.sku if comp else ""

    def __str__(self):
        return f"{self.component_sku} x {self.qty_per_batch}"


class Substitution(models.Model):
    """15 Substitutions: a one-way swap of an item in every recipe.

    With a brand it applies to that brand only; without one (as in the central
    item master workbook) it applies to every brand. A brand's own mapping for
    an item wins over the group-wide one.
    """

    brand = models.ForeignKey(
        Brand, null=True, blank=True, on_delete=models.CASCADE, related_name="substitutions",
        help_text="Leave empty to apply to every brand",
    )
    original = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    substitute = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    factor = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.000001"), "The factor must be above zero.")],
        help_text="Substitute units per 1 original unit",
    )
    note = models.CharField(max_length=300, blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.original.sku} -> {self.substitute.sku}"

    def clean(self):
        if not (self.original_id and self.substitute_id) or not self.active:
            return
        if self.original_id == self.substitute_id:
            raise ValidationError("An item can't be swapped for itself.")
        # A chain (sugar -> flour, flour -> butter) or a second mapping for the same item silently does
        # nothing in the calculation, so it is refused here.
        others = Substitution.objects.filter(active=True).exclude(pk=self.pk)
        others = others.filter(models.Q(brand=self.brand_id) | models.Q(brand__isnull=True)) if self.brand_id else others
        clash = others.filter(original=self.original_id, brand=self.brand_id).first()
        if clash:
            raise ValidationError(f"{self.original.sku} already has a substitution ({clash}). Edit that one instead.")
        chain = (others.filter(original=self.substitute_id).first()
                 or others.filter(substitute=self.original_id).first())
        if chain:
            raise ValidationError(f"This would chain with {chain}. A substitute can't itself be swapped, "
                                  "and a swapped-in item can't be swapped out again. Map the original straight "
                                  "to the final item instead.")


class AuditPeriod(models.Model):
    """One outlet, one month."""

    outlet = models.ForeignKey(Outlet, on_delete=models.PROTECT, related_name="audits")
    period = models.CharField(
        max_length=7, validators=[RegexValidator(r"^\d{4}-(0[1-9]|1[0-2])$", "Use YYYY-MM")]
    )
    variance_tolerance = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal("0.02"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("0.5"))],
        help_text="Allowed F&B variance as a fraction of theoretical: 0.02 = 2%. Between 0 and 0.5.",
    )
    locked = models.BooleanField(default=False, help_text="Signed off: inputs can no longer change")
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Frozen at sign-off: the recipes, prices and quantities the month was signed off with, so later
    # master data changes (a recipe edit, a price sync, a renamed category) don't restate a closed month.
    snapshot = models.JSONField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period", "outlet__brand__code", "outlet__code"]
        unique_together = [("outlet", "period")]
        permissions = [
            ("sign_off_auditperiod", "Can sign off and unlock audits"),
            ("view_all_outlets", "Can see every brand and outlet"),
        ]

    def __str__(self):
        return f"{self.outlet} {self.period}"

    def delete(self, *args, **kwargs):
        if self.locked:
            raise ValueError(f"{self} is signed off. Unlock it before deleting.")
        return super().delete(*args, **kwargs)

    @property
    def brand(self):
        return self.outlet.brand


class SaleLine(models.Model):
    """10 POS Sales. Unmatched POS names keep menu_item empty and are flagged."""

    audit = models.ForeignKey(AuditPeriod, on_delete=models.CASCADE, related_name="sales")
    pos_name = models.CharField(max_length=300)
    # PROTECT: a menu item that has been sold can't be deleted (that would rewrite past audits);
    # set it inactive instead.
    menu_item = models.ForeignKey(MenuItem, null=True, blank=True, on_delete=models.PROTECT)
    qty = quantity()


class ProductionLine(models.Model):
    """11 Production / Transfer / Wastage: extra demand on the same recipes."""

    KIND_CHOICES = [("production", "B2B production"), ("transfer", "Transfer"), ("wastage", "Wastage")]
    audit = models.ForeignKey(AuditPeriod, on_delete=models.CASCADE, related_name="production")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="production")
    menu_item = models.ForeignKey(MenuItem, null=True, blank=True, on_delete=models.PROTECT)
    semi_finished = models.ForeignKey(SemiFinished, null=True, blank=True, on_delete=models.PROTECT)
    item = models.ForeignKey(Item, null=True, blank=True, on_delete=models.PROTECT)
    qty = quantity(help_text="For an item: its base unit (g, ml, pcs, see Unit). For a menu or semi-finished item: portions.")
    customer = models.CharField(max_length=200, blank=True, help_text="Customer / brand")
    note = models.CharField(max_length=300, blank=True)

    def clean(self):
        if sum(bool(x) for x in (self.menu_item_id, self.semi_finished_id, self.item_id)) != 1:
            raise ValidationError("Pick exactly one of menu item, semi-finished item or item.")

    @property
    def sku(self):
        obj = self.menu_item or self.semi_finished or self.item
        return obj.sku if obj else ""

    @property
    def unit(self):
        if self.item:
            return self.item.base_uom or "base unit"
        if self.semi_finished:
            return self.semi_finished.output_uom or "portion"
        return "portion" if self.menu_item else ""


class PurchaseLine(models.Model):
    """12 Purchasing, plus inter-company purchases."""

    SOURCE_CHOICES = [("supplier", "Supplier"), ("interco", "Inter-company")]
    audit = models.ForeignKey(AuditPeriod, on_delete=models.CASCADE, related_name="purchases")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="supplier")
    sku = models.CharField(max_length=40)
    item = models.ForeignKey(Item, null=True, blank=True, on_delete=models.PROTECT)
    description = models.CharField(max_length=300, blank=True)
    qty = quantity()
    value = money()
    invoice_number = models.CharField(max_length=60, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    supplier = models.CharField(max_length=200, blank=True)


class StockCount(models.Model):
    """13 Opening / 14 Closing Inventory."""

    KIND_CHOICES = [("opening", "Opening"), ("closing", "Closing")]
    audit = models.ForeignKey(AuditPeriod, on_delete=models.CASCADE, related_name="stock_counts")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    sku = models.CharField(max_length=40)
    item = models.ForeignKey(Item, null=True, blank=True, on_delete=models.PROTECT)
    description = models.CharField(max_length=300, blank=True)
    value = money()

    @property
    def qty_base(self):
        """Quantity in base units, back-calculated as the sheet does: value / cost per base."""
        if not self.item or not self.item.cost_per_base:
            return Decimal("0")
        return self.value / self.item.cost_per_base


class UserAccess(models.Model):
    """Which brands and outlets a user may see. Users with "Can see every brand and outlet" (the chef,
    procurement and the CEO) don't need one; everyone else sees only what is ticked here."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="audit_access")
    brands = models.ManyToManyField(Brand, blank=True, help_text="Every outlet of these brands")
    outlets = models.ManyToManyField(Outlet, blank=True, help_text="Single outlets, on top of the brands above")

    class Meta:
        verbose_name = "brand and outlet access"
        verbose_name_plural = "brand and outlet access"

    def __str__(self):
        return f"Access for {self.user}"


class ChangeLog(models.Model):
    """Who uploaded, added, changed or deleted what, and when, with old and new values."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    audit = models.ForeignKey(AuditPeriod, null=True, blank=True, on_delete=models.SET_NULL, related_name="changes")
    action = models.CharField(max_length=20)  # upload, add, change, delete, sign off, unlock...
    what = models.CharField(max_length=60)  # e.g. "Purchase line", "Recipe", "Supplier purchases"
    object_repr = models.CharField(max_length=300, blank=True)
    changes = models.JSONField(default=dict, blank=True)  # {"field": [old, new]}
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-at", "-pk"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.user} {self.action} {self.what} {self.object_repr}"


class ReportCache(models.Model):
    """An unlocked audit's dashboard and status, so Trends doesn't recalculate every audit on every visit.
    Deleted whenever the audit's lines or any master data change (see audit/signals.py)."""

    audit = models.OneToOneField(AuditPeriod, on_delete=models.CASCADE, related_name="report_cache")
    dashboard = models.JSONField()
    status = models.JSONField()
    computed_at = models.DateTimeField(auto_now=True)


class FailedLogin(models.Model):
    username = models.CharField(max_length=150, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
