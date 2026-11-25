"""The admin: brands, outlets, users and access for the CEO. Day-to-day menus, recipes and lines are edited
in the app's own screens (audit/data_views.py)."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from . import models


class UserAccessInline(admin.StackedInline):
    model = models.UserAccess
    can_delete = False
    filter_horizontal = ["brands", "outlets"]
    verbose_name_plural = "Brands and outlets this user can see (not needed for Chef, Procurement or superusers)"


class AuditUserAdmin(UserAdmin):
    inlines = [*UserAdmin.inlines, UserAccessInline]


admin.site.unregister(User)
admin.site.register(User, AuditUserAdmin)


@admin.register(models.ChangeLog)
class ChangeLogAdmin(admin.ModelAdmin):
    list_display = ["at", "user", "action", "what", "object_repr", "audit"]
    list_filter = ["action", "what"]
    search_fields = ["object_repr", "note", "user__username"]
    list_select_related = ["user", "audit__outlet__brand"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "currency", "sales_tax_rate"]


@admin.register(models.Outlet)
class OutletAdmin(admin.ModelAdmin):
    list_display = ["brand", "code", "name", "pos_location", "active"]
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
    extra = 3
    autocomplete_fields = ["item", "semi_finished"]
    readonly_fields = ["unit"]

    @admin.display(description="Unit")
    def unit(self, obj):
        if obj and obj.item_id:
            return obj.item.base_uom or "-"
        if obj and obj.semi_finished_id:
            return obj.semi_finished.output_uom or "-"
        return "shown after saving"


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
    list_display = ["original", "substitute", "factor", "applies_to", "active"]
    list_filter = ["brand", "active"]
    autocomplete_fields = ["original", "substitute"]

    @admin.display(description="Applies to")
    def applies_to(self, obj):
        return obj.brand or "All brands"


class LockedAuditInlineMixin:
    """Lines of a signed-off audit are read-only for everyone."""

    def _locked(self, obj):
        return bool(obj and obj.locked)

    def has_add_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_delete_permission(request, obj)


class ProductionInline(LockedAuditInlineMixin, admin.TabularInline):
    model = models.ProductionLine
    extra = 1
    autocomplete_fields = ["menu_item", "semi_finished", "item"]
    readonly_fields = ["unit"]

    @admin.display(description="Unit")
    def unit(self, obj):
        return (obj.unit if obj and obj.pk else "") or "shown after saving"


@admin.register(models.AuditPeriod)
class AuditPeriodAdmin(admin.ModelAdmin):
    list_display = ["outlet", "period", "locked", "locked_at", "locked_by", "created_at"]
    list_filter = ["outlet__brand", "outlet", "period", "locked"]
    list_select_related = ["outlet__brand", "locked_by"]
    # Sales, purchase and stock lines are edited on the audit's "Uploaded data" tab in the app; listing
    # thousands of them here made this page take 1,000+ queries.
    inlines = [ProductionInline]

    def get_readonly_fields(self, request, obj=None):
        # Sign-off and unlock happen with the buttons on the audit's dashboard, which take the snapshot.
        fields = ["locked", "locked_at", "locked_by"]
        if obj and obj.locked:
            fields += ["outlet", "period", "variance_tolerance"]
        return fields

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.locked:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        # Bulk delete would skip the signed-off check, so it is removed.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions
