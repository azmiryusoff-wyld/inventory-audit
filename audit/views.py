from io import BytesIO

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import importers, services
from .models import AuditPeriod, Brand, Outlet


class NewAuditForm(forms.ModelForm):
    class Meta:
        model = AuditPeriod
        fields = ["outlet", "period", "variance_tolerance"]
        widgets = {"period": forms.TextInput(attrs={"placeholder": "2026-10"})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["outlet"].queryset = Outlet.objects.filter(active=True).select_related("brand")


UPLOADS = {
    "pos": ("POS item sales report", "Item Sales Report Summary export from the POS"),
    "purchases": ("Supplier purchases", "Xero invoice export"),
    "interco": ("Inter-company purchases", "Xero invoice export from sister companies"),
    "opening": ("Opening stock count", "Stock count form, last month's closing"),
    "closing": ("Closing stock count", "Stock count form, end of this month"),
}


@login_required
def home(request):
    form = NewAuditForm(request.POST or None)
    if request.method == "POST":
        if not request.user.has_perm("audit.add_auditperiod"):
            messages.error(request, "You don't have permission to start an audit.")
        elif form.is_valid():
            audit = form.save()
            messages.success(request, f"Started {audit}. Upload this month's files below.")
            return redirect("audit_detail", audit.pk)
    audits = AuditPeriod.objects.select_related("outlet__brand")
    return render(request, "audit/home.html", {"audits": audits, "form": form, "brands": Brand.objects.all()})


def _context(audit, tab):
    result = services.run_audit(audit)
    checks = result.checks + services.input_issues(audit)
    return {
        "audit": audit,
        "tab": tab,
        "result": result,
        "dash": result.dashboard(),
        "checks": checks,
        "status": "PASS" if all(c.passed for c in checks) else "REVIEW",
        "currency": audit.brand.currency,
        "uploads": UPLOADS,
        "counts": {
            "pos": audit.sales.count(),
            "purchases": audit.purchases.filter(source="supplier").count(),
            "interco": audit.purchases.filter(source="interco").count(),
            "opening": audit.stock_counts.filter(kind="opening").count(),
            "closing": audit.stock_counts.filter(kind="closing").count(),
        },
    }


@login_required
def audit_detail(request, pk, tab="overview"):
    audit = get_object_or_404(AuditPeriod.objects.select_related("outlet__brand"), pk=pk)
    ctx = _context(audit, tab)
    if tab == "inputs":
        ctx["sales"] = audit.sales.select_related("menu_item").order_by("pos_name")
        ctx["purchases"] = audit.purchases.order_by("invoice_date", "invoice_number")
        ctx["counts_rows"] = audit.stock_counts.select_related("item").order_by("kind", "sku")
        ctx["production"] = audit.production.select_related("menu_item", "semi_finished", "item")
    return render(request, f"audit/{tab}.html", ctx)


@require_POST
@login_required
@permission_required("audit.change_auditperiod", raise_exception=True)
def upload(request, pk, kind):
    audit = get_object_or_404(AuditPeriod, pk=pk)
    f = request.FILES.get("file")
    if audit.locked:
        messages.error(request, "This audit is signed off and locked.")
    elif kind not in UPLOADS or not f:
        messages.error(request, "Choose a file to upload.")
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
            messages.error(request, f"{UPLOADS[kind][0]}: {exc}")
        else:
            messages.success(request, f"{UPLOADS[kind][0]}: {report.created} lines loaded.")
            if report.warnings:
                messages.warning(
                    request, f"{len(report.warnings)} lines didn't match: " + "; ".join(report.warnings[:8])
                    + (" ..." if len(report.warnings) > 8 else "")
                )
    return redirect("audit_detail", pk)


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
        d = services.run_audit(audit).dashboard()
        rows.append({"audit": audit, **d})
    return render(request, "audit/trends.html", {"rows": rows})


@login_required
def export_xlsx(request, pk):
    """Every report for one audit as an Excel file, one tab each."""
    import openpyxl
    from openpyxl.styles import Font

    audit = get_object_or_404(AuditPeriod.objects.select_related("outlet__brand"), pk=pk)
    result = services.run_audit(audit)
    dash = result.dashboard()
    wb = openpyxl.Workbook()

    def sheet(title, header, rows):
        ws = wb.create_sheet(title)
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "Dashboard"
    for label, value in [
        ("Outlet", str(audit.outlet)), ("Period", audit.period), ("Status", dash["status"]),
        ("Total sales (POS)", dash["total_sales"]),
        ("Theoretical COGS - F&B", dash["theoretical_fb"]),
        ("Theoretical COGS - all categories", dash["theoretical_all"]),
        ("Actual COGS - F&B", dash["actual_fb"]),
        ("Theoretical F&B COGS %", dash["theoretical_fb_pct"]),
        ("Actual F&B COGS %", dash["actual_fb_pct"]),
        ("Variance - F&B", dash["variance_fb"]),
        ("Variance %", dash["variance_fb_pct"]),
    ]:
        ws.append([label, value])
    ws.append([])
    ws.append(["Category", "Theoretical", "Actual", "Variance", "Actual % of sales"])
    for c in dash["by_category"]:
        ws.append([c["category"], c["theoretical"], c["actual"], c["variance"], c["pct_of_sales"]])

    sheet("Variance", ["SKU", "Item", "Category", "Has recipe", "Theoretical", "Opening", "Purchases",
                       "Closing", "Actual", "Variance", "Variance %"],
          [[r.sku, r.name, r.category, "Yes" if r.has_recipe else "No", r.theoretical_cost, r.opening,
            r.purchases, r.closing, r.actual_cost, r.variance, r.variance_pct] for r in result.variance])
    sheet("Menu engineering", ["SKU", "Menu item", "Category", "Price", "Cost / portion", "Margin", "Units",
                               "Total contribution", "Total cost", "Margin %", "Mix % in category",
                               "Category avg margin", "Popularity threshold", "Class", "Total sales"],
          [[r.sku, r.name, r.category, r.price, r.cost, r.margin, r.units, r.total_contribution, r.total_cost,
            r.margin_pct, r.mix_share, r.cat_avg_margin, r.popularity_threshold, r.menu_class, r.total_sales]
           for r in result.menu])
    sheet("Consumption", ["SKU", "Item", "Category", "Net", "Yield", "Gross", "Cost / base", "Theoretical", "Status"],
          [[r.sku, r.name, r.category, r.net, r.yield_pct, r.gross, r.cost_per_base, r.theoretical_cost, r.status]
           for r in result.consumption if r.net])
    sheet("Checks", ["Check", "Count", "Status", "What to do", "Details"],
          [[c.label, c.count, "PASS" if c.passed else "REVIEW", c.action, "; ".join(c.details[:50])]
           for c in result.checks + services.input_issues(audit)])

    buf = BytesIO()
    wb.save(buf)
    name = f"{audit.brand.code}-{audit.outlet.code}-{audit.period}-COGS.xlsx".replace(" ", "_")
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp
