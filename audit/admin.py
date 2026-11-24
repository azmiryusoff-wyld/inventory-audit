"""Master data screens. Django's admin gives search, filters and editing for free."""
from django.contrib import admin

from . import models


@admin.register(models.Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "currency", "sales_tax_rate"]


@admin.register(models.Outlet)
class OutletAdmin(admin.ModelAdmin):
    list_display = ["brand", "code", "name", "active"]
    list_filter = ["brand", "active"]


@admin.register(models.Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ["sku", "name", "category", "pack_size", "base_uom", "pack_uom",
                    "purchase_price", "cost_per_base", "yield_pct", "active", "synced_at"]
    list_filter = ["category", "active", "supplier"]
    search_fields = ["sku", "name", "supplier"]
    readonly_fields = ["synced_at"]

    def has_add_permission(self, request):
        # The central item master workbook is the source; edits here would be overwritten by the next sync.
        return request.user.is_superuser

    @admin.display(description="Cost / base unit")
    def cost_per_base(self, obj):
        return round(obj.cost_per_base, 4)


@admin.register(models.SemiFinished)
class SemiFinishedAdmin(admin.ModelAdmin):
    list_display = ["sku", "name", "brand", "output_uom", "active"]
    list_filter = ["brand", "active"]
    search_fields = ["sku", "name"]


class PosAliasInline(admin.TabularInline):
    model = models.PosAlias
    extra = 0


@admin.register(models.MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ["sku", "name", "brand", "category", "price", "has_recipe", "active"]
    list_filter = ["brand", "category", "active"]
    search_fields = ["sku", "name", "aliases__pos_name"]
    inlines = [PosAliasInline]

    @admin.display(boolean=True)
    def has_recipe(self, obj):
        return hasattr(obj, "recipe")


class RecipeLineInline(admin.TabularInline):
    model = models.RecipeLine
    extra = 1
    autocomplete_fields = ["item", "semi_finished"]


@admin.register(models.Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ["__str__", "brand", "batch_yield", "line_count"]
    list_filter = ["brand"]
    search_fields = ["menu_item__name", "menu_item__sku", "semi_finished__name", "semi_finished__sku"]
    autocomplete_fields = ["menu_item", "semi_finished"]
    inlines = [RecipeLineInline]

    @admin.display(description="Lines")
    def line_count(self, obj):
        return obj.lines.count()


@admin.register(models.Substitution)
class SubstitutionAdmin(admin.ModelAdmin):
    list_display = ["original", "substitute", "factor", "brand", "active"]
    list_filter = ["brand", "active"]
    autocomplete_fields = ["original", "substitute"]


class SaleInline(admin.TabularInline):
    model = models.SaleLine
    extra = 0
    autocomplete_fields = ["menu_item"]


class ProductionInline(admin.TabularInline):
    model = models.ProductionLine
    extra = 1
    autocomplete_fields = ["menu_item", "semi_finished", "item"]


@admin.register(models.AuditPeriod)
class AuditPeriodAdmin(admin.ModelAdmin):
    list_display = ["outlet", "period", "locked", "created_at"]
    list_filter = ["outlet__brand", "outlet", "period", "locked"]
    inlines = [ProductionInline, SaleInline]
