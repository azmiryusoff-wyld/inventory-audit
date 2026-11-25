from io import BytesIO

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import importers, services
from .models import AuditPeriod, Brand, MenuItem, Outlet, PosAlias


def last_month() -> str:
    from datetime import date

    today = date.today()
    return f"{today.year - 1}-12" if today.month == 1 else f"{today.year}-{today.month - 1:02d}"


class NewAuditForm(forms.ModelForm):
    tolerance_pct = forms.DecimalField(
        label="Variance tolerance (%)", initial=2, min_value=0, max_value=50, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.5", "style": "width:5em"}),
        help_text="How far actual F&B COGS may be from theoretical, either way, before the audit is flagged.",
    )

    class Meta:
        model = AuditPeriod
        fields = ["outlet", "period"]
        labels = {"period": "Month"}
        widgets = {"period": forms.TextInput(attrs={"type": "month", "placeholder": "YYYY-MM"})}
        error_messages = {"period": {"invalid": "Pick a month, for example 2026-10."}}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["outlet"].queryset = Outlet.objects.filter(active=True).select_related("brand")
        self.fields["outlet"].empty_label = "Choose an outlet"
        self.fields["period"].initial = last_month()
        self.existing = None

    def clean_period(self):
        from datetime import date

        period = self.cleaned_data["period"]
        today = date.today()
        if period > f"{today.year:04d}-{today.month:02d}":
            raise forms.ValidationError("That month hasn't started yet. Pick this month or an earlier one.")
        return period

    def validate_unique(self):
        outlet, period = self.cleaned_data.get("outlet"), self.cleaned_data.get("period")
        if outlet and period:
            self.existing = AuditPeriod.objects.filter(outlet=outlet, period=period).first()
            if self.existing:
                self.add_error(None, f"{self.existing} already exists.")
                return
        super().validate_unique()

    def save(self, commit=True):
        self.instance.variance_tolerance = self.cleaned_data["tolerance_pct"] / 100
        return super().save(commit)


MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# The permission each upload needs: the chef loads POS sales; purchases and stock counts are procurement's.
UPLOAD_PERMISSIONS = {
    "pos": "audit.change_saleline",
    "purchases": "audit.change_purchaseline",
    "interco": "audit.change_purchaseline",
    "opening": "audit.change_stockcount",
    "closing": "audit.change_stockcount",
}

UPLOADS = {
    "pos": ("POS item sales report", "Item Sales Report Summary export from the POS"),
    "purchases": ("Supplier purchases", "Xero invoice export"),
    "interco": ("Inter-company purchases", "Xero invoice export from sister companies"),
    "opening": ("Opening stock count", "Stock count form, last month's closing"),
    "closing": ("Closing stock count", "Stock count form, end of this month"),
}

# One filled-in line per template, shown beside the download link (not put in the file, where it
# would be loaded if someone forgot to delete it).
UPLOAD_EXAMPLES = {
    "pos": "Latte - Hot, 120",
    "purchases": "INV-1102, 05/10/2026, Ladang Segar Sdn Bhd, MILK-1L, Fresh milk 1L, 24, 168.00",
    "interco": "PB-0042, 07/10/2026, Pastry Boys, PBCK-CROI, Croissant, 50, 250.00",
    "opening": "MILK-1L, Fresh milk 1L, 12 (packs counted), leave the RM value blank to use the item master price",
    "closing": "MILK-1L, Fresh milk 1L, 12 (packs counted), leave the RM value blank to use the item master price",
}

# Header row of the empty CSV template offered beside each upload. Every column name here is one the
# matching reader in importers.py looks for, so a filled-in template always imports.
UPLOAD_TEMPLATES = {
    "pos": ["Full Name", "Quantity"],
    "purchases": ["InvoiceNumber", "InvoiceDate", "ContactName", "InventoryItemCode", "Description",
                  "Quantity", "LineAmount"],
    "interco": ["InvoiceNumber", "InvoiceDate", "ContactName", "InventoryItemCode", "Description",
                "Quantity", "LineAmount"],
    "opening": ["SKU", "Item Name", "Counted Qty (packs)", "Total RM Value"],
    "closing": ["SKU", "Item Name", "Counted Qty (packs)", "Total RM Value"],
}


@login_required
def home(request):
    form = NewAuditForm(request.POST or None)
    if request.method == "POST":
        if not request.user.has_perm("audit.add_auditperiod"):
            messages.error(request, "You don't have permission to start an audit.")
        elif form.is_valid():
            audit = form.save()
            copied = services.copy_previous_closing(audit)
            note = (f" Opening stock was filled in from {services.previous_period(audit.period)}'s closing "
                    f"count ({copied} lines)." if copied else "")
            messages.success(request, f"Started {audit}. Upload this month's files below.{note}")
            return redirect("audit_detail", audit.pk)
    audits = AuditPeriod.objects.select_related("outlet__brand")
    return render(request, "audit/home.html", {"audits": audits, "form": form, "brands": Brand.objects.all()})


def _context(audit, tab, user):
    result, issues, status = services.audit_report(audit)
    counts = services.input_counts(audit)
    ctx = {
        "audit": audit,
        "tab": tab,
        "result": result,
        "dash": result.dashboard(),
        "checks": result.checks + issues,
        "status": status,
        "currency": audit.brand.currency,
        "uploads": {k: (*v, user.has_perm(UPLOAD_PERMISSIONS[k]), UPLOAD_EXAMPLES[k]) for k, v in UPLOADS.items()},
        "counts": counts,
        "can_sign_off": user.has_perm("audit.sign_off_auditperiod"),
        # Linking a POS name to a menu item is the chef's job (procurement can't edit menus).
        "can_map": user.has_perm("audit.add_posalias") and not audit.locked,
        "brand_menu": MenuItem.objects.filter(brand=audit.brand, active=True).order_by("name"),
        "brand_ready": MenuItem.objects.filter(brand=audit.brand).exists(),
        "excluded": {c.key: c.impact for c in issues if c.impact},
        "can_copy_opening": (not audit.locked and user.has_perm(UPLOAD_PERMISSIONS["opening"])
                             and services.previous_closing_count(audit) > 0),
        "previous_period": services.previous_period(audit.period),
        "tolerance": float(audit.variance_tolerance),
    }
    ctx["variance_outside"] = services.variance_outside(ctx["dash"], ctx["tolerance"])
    return ctx


@login_required
def audit_detail(request, pk, tab="overview"):
    audit = get_object_or_404(AuditPeriod.objects.select_related("outlet__brand"), pk=pk)
    ctx = _context(audit, tab, request.user)
    if tab == "inputs":
        ctx["sales"] = audit.sales.select_related("menu_item").order_by("pos_name")
        ctx["purchases"] = audit.purchases.order_by("invoice_date", "invoice_number")
        ctx["counts_rows"] = audit.stock_counts.select_related("item").order_by("kind", "sku")
        ctx["production"] = audit.production.select_related("menu_item", "semi_finished", "item")
    return render(request, f"audit/{tab}.html", ctx)


@require_POST
@login_required
def upload(request, pk, kind):
    if kind not in UPLOADS:
        raise Http404("No such upload.")
    if not request.user.has_perm(UPLOAD_PERMISSIONS[kind]):
        raise PermissionDenied
    audit = get_object_or_404(AuditPeriod.objects.select_related("outlet__brand"), pk=pk)
    f = request.FILES.get("file")
    label = UPLOADS[kind][0]
    if audit.locked:
        messages.error(request, "This audit is signed off and locked.")
    elif not f:
        messages.error(request, "Choose a file to upload.")
    elif f.size > MAX_UPLOAD_BYTES:
        messages.error(request, f"{label}: the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
                                "Nothing was changed.")
    else:
        try:
            if kind == "pos":
                report = importers.load_pos_sales(audit, f)
            elif kind in ("purchases", "interco"):
                source = "interco" if kind == "interco" else "supplier"
                report = importers.load_purchases(audit, f, source=source)
            else:
                report = importers.load_stock_count(audit, f, kind)
        except ValueError as exc:
            messages.error(request, f"{label}: {exc}")
        except Exception:  # an unreadable file must never show a crash page
            import logging

            logging.getLogger(__name__).exception("Upload failed: %s %s", kind, getattr(f, "name", ""))
            messages.error(request, f"{label}: this file couldn't be read, so nothing was changed. "
                                    "Save it again as .xlsx or .csv and upload that.")
        else:
            n = report.created
            skipped = (f", {report.skipped} handling-charge line{'s' if report.skipped > 1 else ''} skipped"
                       if report.skipped else "")
            valued = (f", {report.valued_from_packs} valued from counted packs at item master prices"
                      if report.valued_from_packs else "")
            note = f" {report.note}." if report.note else ""
            messages.success(request, f"{label}: {n} line{'s' if n != 1 else ''} loaded{skipped}{valued}.{note}")
            if report.warnings:
                messages.warning(
                    request, "Please check: " + "; ".join(report.warnings[:8])
                    + (f"; and {len(report.warnings) - 8} more" if len(report.warnings) > 8 else "")
                )
    return redirect("audit_detail", pk)


@require_POST
@login_required
def copy_opening(request, pk):
    """Opening stock = last month's closing count."""
    if not request.user.has_perm(UPLOAD_PERMISSIONS["opening"]):
        raise PermissionDenied
    audit = get_object_or_404(AuditPeriod.objects.select_related("outlet__brand"), pk=pk)
    try:
        n = services.copy_previous_closing(audit)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        prev = services.previous_period(audit.period)
        if n:
            messages.success(request, f"Opening stock: {n} line{'s' if n != 1 else ''} copied from {prev}'s closing count.")
        else:
            messages.error(request, f"There is no closing count for {prev} to copy.")
    return redirect("audit_detail", pk)


@require_POST
@login_required
def map_pos_name(request, pk):
    """Link an unmatched POS name to a menu item by adding it as an alias. Chef (and CEO) only."""
    if not request.user.has_perm("audit.add_posalias"):
        raise PermissionDenied
    audit = get_object_or_404(AuditPeriod.objects.select_related("outlet__brand"), pk=pk)
    pos_name = (request.POST.get("pos_name") or "").strip()
    menu_item = MenuItem.objects.filter(brand=audit.brand, pk=request.POST.get("menu_item") or 0).first()
    if audit.locked:
        messages.error(request, "This audit is signed off and locked.")
    elif not pos_name or menu_item is None:
        messages.error(request, "Pick the menu item to link it to.")
    else:
        PosAlias.objects.get_or_create(menu_item=menu_item, pos_name=pos_name)
        n = services.rematch_sales(audit)
        messages.success(request, f"\"{pos_name}\" now counts as {menu_item.name} ({n} line{'s' if n != 1 else ''} "
                                  "matched). Other months and outlets of this brand will match it too.")
    return redirect("audit_detail", pk)


@require_POST
@login_required
def sign_off(request, pk):
    """Sign off freezes the month (results, recipes and prices as they are now); unlock reopens it."""
    if not request.user.has_perm("audit.sign_off_auditperiod"):
        raise PermissionDenied
    audit = get_object_or_404(AuditPeriod.objects.select_related("outlet__brand"), pk=pk)
    if request.POST.get("action") == "unlock":
        services.unlock(audit)
        messages.success(request, f"{audit} is unlocked. Its numbers now follow today's recipes and prices "
                                  "again, and files can be uploaded.")
    else:
        services.sign_off(audit, request.user)
        messages.success(request, f"{audit} is signed off. Its numbers are frozen as they are now.")
    return redirect("audit_detail", pk)


@login_required
def upload_template(request, kind):
    """An empty CSV with just the header row the importer expects."""
    import csv

    if kind not in UPLOAD_TEMPLATES:
        raise Http404("No template for this file type.")
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{kind}-template.csv"'
    csv.writer(resp).writerow(UPLOAD_TEMPLATES[kind])
    return resp


@login_required
def menu_costing(request, brand_code):
    brand = get_object_or_404(Brand, code=brand_code)
    return render(request, "audit/menu_costing.html", {
        "brand": brand, "rows": services.menu_costing(brand), "currency": brand.currency,
    })


@login_required
def trends(request):
    rows = []
    for audit in AuditPeriod.objects.select_related("outlet__brand").order_by("outlet__brand__code", "outlet__code", "period"):
        if audit.locked and audit.snapshot and "dashboard" in audit.snapshot:
            d, status = audit.snapshot["dashboard"], audit.snapshot["status"]
        else:
            result, issues, status = services.audit_report(audit)
            d = result.dashboard()
        rows.append({"audit": audit, **d, "status": status})
    return render(request, "audit/trends.html", {"rows": rows})


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    """Text that Excel would run as a formula (=, +, -, @) is written as plain text instead."""
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


@login_required
def export_xlsx(request, pk):
    """Every report for one audit as an Excel file, one tab each."""
    import openpyxl
    from openpyxl.styles import Font

    audit = get_object_or_404(AuditPeriod.objects.select_related("outlet__brand"), pk=pk)
    result, issues, status = services.audit_report(audit)
    dash = result.dashboard()
    wb = openpyxl.Workbook()

    MONEY, PCT, QTY, COST = "#,##0.00;(#,##0.00);-", "0.0%", "#,##0.###", "0.0000"
    ROUND = {MONEY: 2, PCT: 6, QTY: 3, COST: 6}

    def put(ws, row, formats=()):
        ws.append([
            round(v, ROUND[f]) if isinstance(v, float) and f in ROUND else safe_cell(v)
            for v, f in zip(row, list(formats) + [None] * (len(row) - len(formats)))
        ])
        for c, f in zip(ws[ws.max_row], formats):
            if f:
                c.number_format = f

    def finish(ws, header_row=1):
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
        for c in ws[header_row]:
            c.font = Font(bold=True)

    def sheet(title, header, formats, rows):
        ws = wb.create_sheet(title)
        ws.append(header)
        for r in rows:
            put(ws, r, formats)
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
        finish(ws)

    ws = wb.active
    ws.title = "Dashboard"
    for label, value, fmt in [
        ("Outlet", str(audit.outlet), None), ("Month", audit.period, None), ("Status", status["label"], None),
        ("Status detail", "; ".join(status["reasons"]), None),
        ("Signed off", f"{audit.locked_at:%d/%m/%Y %H:%M} by {audit.locked_by or '-'}" if audit.locked and audit.locked_at
         else ("Yes" if audit.locked else "No"), None),
        ("Total sales (POS, SST included)", dash["total_sales"], MONEY),
        ("Theoretical COGS - F&B", dash["theoretical_fb"], MONEY),
        ("Theoretical COGS - all categories", dash["theoretical_all"], MONEY),
        ("Actual COGS - F&B", dash["actual_fb"], MONEY),
        ("Theoretical F&B COGS % of sales", dash["theoretical_fb_pct"], PCT),
        ("Actual F&B COGS % of sales", dash["actual_fb_pct"], PCT),
        ("Variance - F&B", dash["variance_fb"], MONEY),
        ("Variance % of theoretical", dash["variance_fb_pct"], PCT),
        ("Variance tolerance", float(audit.variance_tolerance), PCT),
    ]:
        put(ws, [label, value], [None, fmt])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.append([])
    ws.append(["Category", "Theoretical", "Actual", "Variance", "Actual % of sales"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for c in dash["by_category"]:
        put(ws, [c["category"], c["theoretical"], c["actual"], c["variance"], c["pct_of_sales"]],
            [None, MONEY, MONEY, MONEY, PCT])
    finish(ws, header_row=ws.max_row - len(dash["by_category"]))

    sheet("Variance", ["SKU", "Item", "Category", "Has recipe", "Theoretical", "Opening", "Purchases",
                       "Closing", "Actual", "Variance", "Variance %"],
          [None, None, None, None, MONEY, MONEY, MONEY, MONEY, MONEY, MONEY, PCT],
          [[r.sku, r.name, r.category, "Yes" if r.has_recipe else "No", r.theoretical_cost, r.opening,
            r.purchases, r.closing, r.actual_cost, r.variance, r.variance_pct] for r in result.variance])
    sheet("Top leakage", ["SKU", "Item", "Leakage", "Share of F&B leakage"], [None, None, MONEY, PCT],
          [[r["sku"], r["name"], r["variance"], r["share"]] for r in dash["top_leakage"]])
    sheet("Menu engineering", ["SKU", "Menu item", "Category", "Price (SST incl.)", "Cost / portion", "Margin", "Units",
                               "Total contribution", "Total cost", "Margin %", "Mix % in category",
                               "Category avg margin", "Popularity threshold", "Class", "Total sales"],
          [None, None, None, MONEY, MONEY, MONEY, QTY, MONEY, MONEY, PCT, PCT, MONEY, PCT, None, MONEY],
          [[r.sku, r.name, r.category, r.price, r.cost, r.margin, r.units, r.total_contribution, r.total_cost,
            r.margin_pct, r.mix_share, r.cat_avg_margin, r.popularity_threshold, r.menu_class, r.total_sales]
           for r in result.menu])
    sheet("Consumption", ["SKU", "Item", "Category", "Net", "Unit", "Yield", "Gross", "Cost per unit", "Theoretical", "Status"],
          [None, None, None, QTY, None, PCT, QTY, COST, MONEY, None],
          [[r.sku, r.name, r.category, r.net, r.base_uom, r.yield_pct, r.gross, r.cost_per_base, r.theoretical_cost,
            r.status] for r in result.consumption if r.net])
    sheet("Checks", ["Check", "Count", "Status", "Who fixes it", "Effect", "What to do", "Details"], [],
          [[c.label, c.count, "Pass" if c.passed else "Review", "" if c.passed else c.owner, c.impact, c.action,
            "; ".join(c.details[:50])] for c in result.checks + issues])
    purchases = audit.purchases.order_by("invoice_date", "invoice_number")
    sheet("Purchases", ["Date", "Invoice", "Source", "Supplier", "SKU", "Description", "Qty", "Value"],
          [None, None, None, None, None, None, QTY, MONEY],
          [[p.invoice_date.strftime("%d/%m/%Y") if p.invoice_date else "", p.invoice_number, p.get_source_display(),
            p.supplier, p.sku, p.description, float(p.qty), float(p.value)] for p in purchases])
    sheet("Stock counts", ["Count", "SKU", "Description", "Value"], [None, None, None, MONEY],
          [[c.get_kind_display(), c.sku, c.description, float(c.value)]
           for c in audit.stock_counts.order_by("kind", "sku")])

    buf = BytesIO()
    wb.save(buf)
    name = f"{audit.brand.code}-{audit.outlet.code}-{audit.period}-COGS.xlsx".replace(" ", "_")
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp
