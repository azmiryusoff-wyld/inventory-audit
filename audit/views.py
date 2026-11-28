from io import BytesIO
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import access, history, importers, services
from .models import (
    AuditPeriod, AuditSubstitution, Item, MenuItem, Outlet, PosAlias, ProductionLine, PurchaseLine, SaleLine, SemiFinished, StockCount,
)


def last_month() -> str:
    from datetime import date

    today = date.today()
    return f"{today.year - 1}-12" if today.month == 1 else f"{today.year}-{today.month - 1:02d}"


class NewAuditForm(forms.ModelForm):
    tolerance_pct = forms.DecimalField(
        label="Variance tolerance (%)", initial=2, min_value=0, max_value=50, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.5", "style": "width:5em"}),
        help_text="How far actual F&B COGS may be from theoretical, either way, as a % of sales, before the "
                  "audit is flagged.",
    )

    class Meta:
        model = AuditPeriod
        fields = ["outlet", "period"]
        labels = {"period": "Month"}
        widgets = {"period": forms.TextInput(attrs={"type": "month", "placeholder": "YYYY-MM"})}
        error_messages = {"period": {"invalid": "Pick a month, for example 2026-10."}}

    def __init__(self, *a, user=None, **kw):
        super().__init__(*a, **kw)
        outlets = access.outlets_for(user) if user is not None else Outlet.objects.select_related("brand")
        self.fields["outlet"].queryset = outlets.filter(active=True)
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
    "b2b": "audit.change_productionline",
    "opening": "audit.change_stockcount",
    "closing": "audit.change_stockcount",
}

UPLOADS = {
    "pos": ("POS item sales report", "Item Sales Report Summary export from the POS"),
    "purchases": ("Supplier purchases", "Xero invoice export"),
    "b2b": ("B2B sales to sister brands", "Xero export of this outlet's own sales invoices to sister brands"),
    "opening": ("Opening stock count", "Stock count form, last month's closing"),
    "closing": ("Closing stock count", "Stock count form, end of this month"),
}

# One filled-in line per template, shown beside the download link (not put in the file, where it
# would be loaded if someone forgot to delete it).
UPLOAD_EXAMPLES = {
    "pos": "Latte - Hot, 120",
    "purchases": "INV-1102, 05/10/2026, Ladang Segar Sdn Bhd, MILK-1L, Fresh milk 1L, 24, 168.00",
    "b2b": "INV-2207, 09/10/2026, Eight Ounce Coffee, KFF0156, Spinach chicken sandwich (1 set x 5 portions), 2, 59.60",
    "opening": "MILK-1L, Fresh milk 1L, 12 (packs counted), leave the RM value blank to use the item master price. "
               "Made in-house: SF-CARAMEL, Caramel sauce, 1200 in Semi-finished Qty (grams, by weight), packs and RM blank",
    "closing": "MILK-1L, Fresh milk 1L, 12 (packs counted), leave the RM value blank to use the item master price. "
               "Made in-house: SF-CARAMEL, Caramel sauce, 1200 in Semi-finished Qty (grams, by weight), packs and RM blank",
}

# Header row of the empty CSV template offered beside each upload. Every column name here is one the
# matching reader in importers.py looks for, so a filled-in template always imports.
UPLOAD_TEMPLATES = {
    "pos": ["Full Name", "Quantity"],
    "purchases": ["InvoiceNumber", "InvoiceDate", "ContactName", "InventoryItemCode", "Description",
                  "Quantity", "LineAmount"],
    "b2b": ["InvoiceNumber", "InvoiceDate", "ContactName", "InventoryItemCode", "Description",
            "Quantity", "LineAmount"],
    "opening": ["SKU", "Item Name", "Counted Qty (packs)", "Total RM Value", "Semi-finished Qty"],
    "closing": ["SKU", "Item Name", "Counted Qty (packs)", "Total RM Value", "Semi-finished Qty"],
}


def start_audit(request, form) -> AuditPeriod:
    audit = form.save()
    copied = services.copy_previous_closing(audit)
    history.log(request.user, "start", "Audit", audit, audit,
                note=f"Opening stock copied from {services.previous_period(audit.period)} ({copied} lines)" if copied else "")
    if copied:
        note = (f" Opening stock was filled in from {services.previous_period(audit.period)}'s closing "
                f"count ({copied} lines).")
    elif services.is_first_month(audit):
        note = (" This is the first month for this outlet in the app: upload last month's closing count "
                "from the old workbook as opening stock.")
    else:
        note = ""
    messages.success(request, f"Started {audit}. Upload this month's files below.{note}")
    return audit


@login_required
def home(request):
    form = NewAuditForm(request.POST or None, user=request.user)
    if request.method == "POST":
        if not request.user.has_perm("audit.add_auditperiod"):
            messages.error(request, "You don't have permission to start an audit.")
        elif form.is_valid():
            audit = start_audit(request, form)
            return redirect("audit_detail", audit.pk)
    audits = access.audits_for(request.user)
    f = {k: request.GET.get(k, "").strip() for k in ("brand", "outlet", "month", "signed")}
    if f["brand"]:
        audits = audits.filter(outlet__brand__code=f["brand"])
    if f["outlet"]:
        audits = audits.filter(outlet_id=f["outlet"] if f["outlet"].isdigit() else 0)
    if f["month"]:
        audits = audits.filter(period=f["month"])
    if f["signed"] in ("yes", "no"):
        audits = audits.filter(locked=f["signed"] == "yes")
    page = Paginator(audits, 50).get_page(request.GET.get("page"))
    status = services.summaries(page.object_list)
    rows = [{"audit": a, "status": status[a.pk][1]} for a in page.object_list]
    query = "&".join(f"{k}={v}" for k, v in f.items() if v)
    return render(request, "audit/home.html", {
        "rows": rows, "page": page, "form": form, "f": f, "query": query,
        "brands": access.brands_for(request.user), "outlets": access.outlets_for(request.user),
        "any_audits": access.audits_for(request.user).exists(),
        "no_access": not access.sees_all(request.user) and not access.outlets_for(request.user).exists(),
    })


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
        "first_month": services.is_first_month(audit),
        "tolerance": float(audit.variance_tolerance),
    }
    # From the status, so a signed-off month keeps the verdict it was signed off with.
    ctx["variance_outside"] = not status["within_tolerance"]
    ctx["variance_pct_sales"] = services.variance_pct_of_sales(ctx["dash"])
    ctx["can_edit"] = {k: user.has_perm(p) and not audit.locked for k, p in LINE_PERMISSIONS.items()}
    services.store_summary(audit, ctx["dash"], status)
    return ctx


@login_required
def audit_detail(request, pk, tab="overview"):
    audit = access.get_audit(request.user, pk)
    ctx = _context(audit, tab, request.user)
    if tab == "inputs":
        ctx["sales"] = audit.sales.select_related("menu_item").order_by("pos_name")
        ctx["purchases"] = audit.purchases.select_related("item").order_by("invoice_date", "invoice_number")
        ctx["counts_rows"] = list(audit.stock_counts.select_related("item", "semi_finished").order_by("kind", "sku"))
        # A semi-finished line's value: its counted weight at today's recipe cost (what it adds to its ingredients).
        sf_values = {(r["kind"], r["sku"]): r["value"] for r in ctx["result"].sf_stock}
        for c in ctx["counts_rows"]:
            c.sf_value = sf_values.get((c.kind, c.sku), 0.0)
        ctx["production"] = audit.production.select_related("menu_item", "semi_finished", "item")
        ctx["swaps"] = audit.swaps.select_related("original", "substitute").order_by("original__sku")
        ctx["standing_swaps"] = services.standing_swaps(audit.brand)
    elif tab == "variance":
        rows = result_rows = ctx["result"].variance
        ctx["variance_totals"] = {
            k: sum(getattr(r, k) for r in result_rows)
            for k in ("theoretical_cost", "opening", "purchases", "closing", "actual_cost", "variance",
                      "opening_sf", "closing_sf")
        }
        ctx["categories"] = sorted({r.category for r in rows})
    elif tab == "history":
        entries = list(audit.changes.select_related("user")[:300])
        for e in entries:
            e.summary = history.describe(e)
        ctx["entries"] = entries
    return render(request, f"audit/{tab}.html", ctx)


@require_POST
@login_required
def upload(request, pk, kind):
    if kind not in UPLOADS:
        raise Http404("No such upload.")
    if not request.user.has_perm(UPLOAD_PERMISSIONS[kind]):
        raise PermissionDenied
    audit = access.get_audit(request.user, pk)
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
            elif kind == "b2b":
                report = importers.load_b2b_sales(audit, f)
            elif kind == "purchases":
                report = importers.load_purchases(audit, f, source="supplier")
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
            if report.sf_lines:
                valued += (f", {report.sf_lines} semi-finished item{'s' if report.sf_lines > 1 else ''} counted by "
                           "weight and broken down into their ingredients")
            note = f" {report.note}." if report.note else ""
            messages.success(request, f"{label}: {n} line{'s' if n != 1 else ''} loaded{skipped}{valued}.{note}")
            history.log(request.user, "upload", label, getattr(f, "name", ""), audit,
                        note=f"{n} lines loaded, replacing the previous file{skipped}{valued}"
                        + (f"; {len(report.warnings)} warnings" if report.warnings else ""))
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
    audit = access.get_audit(request.user, pk)
    try:
        n = services.copy_previous_closing(audit)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        prev = services.previous_period(audit.period)
        if n:
            messages.success(request, f"Opening stock: {n} line{'s' if n != 1 else ''} copied from {prev}'s closing count.")
            history.log(request.user, "upload", "Opening stock count", f"copied from {prev}", audit, note=f"{n} lines")
        else:
            messages.error(request, f"There is no closing count for {prev} to copy.")
    return redirect("audit_detail", pk)


@require_POST
@login_required
def map_pos_name(request, pk):
    """Link an unmatched POS name to a menu item by adding it as an alias. Chef (and CEO) only."""
    if not request.user.has_perm("audit.add_posalias"):
        raise PermissionDenied
    audit = access.get_audit(request.user, pk)
    pos_name = (request.POST.get("pos_name") or "").strip()
    menu_item = MenuItem.objects.filter(brand=audit.brand, pk=request.POST.get("menu_item") or 0).first()
    if audit.locked:
        messages.error(request, "This audit is signed off and locked.")
    elif not pos_name or menu_item is None:
        messages.error(request, "Pick the menu item to link it to.")
    else:
        PosAlias.objects.get_or_create(menu_item=menu_item, pos_name=pos_name)
        n = services.rematch_sales(audit)
        history.log(request.user, "add", "POS alias", pos_name, audit, note=f"Linked to {menu_item}")
        messages.success(request, f"\"{pos_name}\" now counts as {menu_item.name} ({n} line{'s' if n != 1 else ''} "
                                  "matched). Other months and outlets of this brand will match it too.")
    return redirect("audit_detail", pk)


@require_POST
@login_required
def sign_off(request, pk):
    """Sign off freezes the month (results, recipes and prices as they are now); unlock reopens it."""
    if not request.user.has_perm("audit.sign_off_auditperiod"):
        raise PermissionDenied
    audit = access.get_audit(request.user, pk)
    if request.POST.get("action") == "unlock":
        services.unlock(audit)
        history.log(request.user, "unlock", "Audit", audit, audit)
        messages.success(request, f"{audit} is unlocked. Its numbers now follow today's recipes and prices "
                                  "again, and files can be uploaded.")
    else:
        services.sign_off(audit, request.user)
        history.log(request.user, "sign off", "Audit", audit, audit)
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
    brand = access.get_brand(request.user, brand_code)
    return render(request, "audit/menu_costing.html", {
        "brand": brand, "rows": services.menu_costing(brand), "currency": brand.currency,
    })


@login_required
def trends(request):
    """Every audit by outlet and month. Dashboards come from the sign-off snapshot or the stored copy
    (ReportCache), so only audits changed since they were last opened are worked out again."""
    audits = access.audits_for(request.user).order_by("outlet__brand__code", "outlet__code", "period")
    f = {k: request.GET.get(k, "").strip() for k in ("brand", "outlet", "from", "to")}
    if not any(f.values()):
        f["from"] = services.months_back(last_month(), 11)
    if f["brand"]:
        audits = audits.filter(outlet__brand__code=f["brand"])
    if f["outlet"]:
        audits = audits.filter(outlet_id=f["outlet"] if f["outlet"].isdigit() else 0)
    if f["from"]:
        audits = audits.filter(period__gte=f["from"])
    if f["to"]:
        audits = audits.filter(period__lte=f["to"])
    audits = list(audits[:1000])
    data = services.summaries(audits)
    rows = [{"audit": a, **data[a.pk][0], "status": data[a.pk][1],
             "variance_pct_sales": services.variance_pct_of_sales(data[a.pk][0])} for a in audits]
    return render(request, "audit/trends.html", {
        "rows": rows, "f": f, "brands": access.brands_for(request.user), "outlets": access.outlets_for(request.user),
    })


@login_required
def month_close(request):
    """Every outlet x every file for one month: what's done, missing or flagged."""
    period = request.GET.get("month") or request.POST.get("month") or last_month()
    if not services.valid_period(period):
        period = last_month()
    outlets = list(access.outlets_for(request.user).filter(active=True))
    brand = request.GET.get("brand", "")
    if brand:
        outlets = [o for o in outlets if o.brand.code == brand]
    audits = {a.outlet_id: a for a in access.audits_for(request.user).filter(period=period, outlet__in=outlets)}

    if request.method == "POST":
        if not request.user.has_perm("audit.add_auditperiod"):
            raise PermissionDenied
        started = 0
        for o in outlets:
            if o.pk not in audits:
                form = NewAuditForm({"outlet": o.pk, "period": period, "tolerance_pct": "2"}, user=request.user)
                if form.is_valid():
                    a = form.save()
                    copied = services.copy_previous_closing(a)
                    history.log(request.user, "start", "Audit", a, a,
                                note=f"Started from month close; {copied or 0} opening lines copied")
                    started += 1
        messages.success(request, f"Started {started} audit{'s' if started != 1 else ''} for {period}. "
                                  "Opening stock was copied from last month's closing count where there was one.")
        return redirect(f"{reverse('month_close')}?month={period}" + (f"&brand={brand}" if brand else ""))

    ids = [a.pk for a in audits.values()]
    def counts(qs, field=None):
        out = {}
        for row in qs.filter(audit__in=ids).values("audit", *([field] if field else [])).annotate(n=Count("id")):
            out[(row["audit"], row[field]) if field else row["audit"]] = row["n"]
        return out
    sales = counts(SaleLine.objects)
    purchases = counts(PurchaseLine.objects, "source")
    stock = counts(StockCount.objects, "kind")
    b2b = counts(ProductionLine.objects.filter(kind="b2b"))
    status = services.summaries(audits.values())
    rows = []
    for o in outlets:
        a = audits.get(o.pk)
        files = None
        if a:
            files = [
                ("pos", sales.get(a.pk, 0)), ("purchases", purchases.get((a.pk, "supplier"), 0)),
                ("b2b", b2b.get(a.pk, 0)),
                ("opening", stock.get((a.pk, "opening"), 0)), ("closing", stock.get((a.pk, "closing"), 0)),
            ]
        rows.append({"outlet": o, "audit": a, "files": files, "status": status[a.pk][1] if a else None})
    done = sum(1 for r in rows if r["audit"] and r["audit"].locked)
    return render(request, "audit/month_close.html", {
        "rows": rows, "period": period, "brand": brand, "brands": access.brands_for(request.user),
        "missing": sum(1 for r in rows if not r["audit"]), "done": done, "required": services.REQUIRED_INPUTS,
        "can_start": request.user.has_perm("audit.add_auditperiod"),
    })


@login_required
def help_page(request):
    return render(request, "audit/help.html", {})


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

    audit = access.get_audit(request.user, pk)
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
        ("POS sales (SST included)", dash.get("pos_sales", dash["total_sales"]), MONEY),
        ("B2B sales to sister brands", dash.get("b2b_sales", 0.0), MONEY),
        ("Total sales (POS + B2B)", dash["total_sales"], MONEY),
        ("Theoretical COGS - F&B", dash["theoretical_fb"], MONEY),
        ("Theoretical COGS - all categories", dash["theoretical_all"], MONEY),
        ("Actual COGS - F&B", dash["actual_fb"], MONEY),
        ("Theoretical F&B COGS % of sales", dash["theoretical_fb_pct"], PCT),
        ("Actual F&B COGS % of sales", dash["actual_fb_pct"], PCT),
        ("Variance - F&B", dash["variance_fb"], MONEY),
        ("Variance % of sales (tested against the tolerance)", services.variance_pct_of_sales(dash), PCT),
        ("Variance % of theoretical", dash["variance_fb_pct"], PCT),
        ("Gross leakage - F&B (used more than the recipes say)", dash.get("leakage_total", 0.0), MONEY),
        ("Offsets - F&B (used less than the recipes say)", dash.get("offsets_total", 0.0), MONEY),
        ("Variance tolerance (% of sales)", float(audit.variance_tolerance), PCT),
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
                       "Closing", "Actual", "Variance", "Variance %", "Variance qty", "Unit",
                       "Opening: in semi-finished", "Closing: in semi-finished"],
          [None, None, None, None, MONEY, MONEY, MONEY, MONEY, MONEY, MONEY, PCT, QTY, None, MONEY, MONEY],
          [[r.sku, r.name, r.category, "Yes" if r.has_recipe else "No", r.theoretical_cost, r.opening,
            r.purchases, r.closing, r.actual_cost, r.variance, r.variance_pct, r.variance_qty, r.base_uom,
            r.opening_sf, r.closing_sf]
           for r in result.variance])
    top_cols = ["Opening", "Purchases", "Closing", "Actual", "Theoretical"]
    sheet("Top leakage", ["SKU", "Item", *top_cols, "Leakage", "Share of F&B leakage"], [None, None] + [MONEY] * 6 + [PCT],
          [[r["sku"], r["name"], r.get("opening"), r.get("purchases"), r.get("closing"), r.get("actual"),
            r.get("theoretical"), r["variance"], r.get("share")] for r in dash["top_leakage"]])
    sheet("Top underuse", ["SKU", "Item", *top_cols, "Variance", "Share of F&B underuse"], [None, None] + [MONEY] * 6 + [PCT],
          [[r["sku"], r["name"], r.get("opening"), r.get("purchases"), r.get("closing"), r.get("actual"),
            r.get("theoretical"), r["variance"], r.get("share")] for r in dash["top_gains"]])
    sheet("Menu engineering", ["SKU", "Menu item", "Category", "Price (SST incl.)", "Cost / portion", "Margin", "Units",
                               "Total contribution", "Total cost", "Margin %", "Mix % in category",
                               "Category avg margin", "Popularity threshold", "Class", "Total sales",
                               "Profit (H/L)", "Popularity (H/L)"],
          [None, None, None, MONEY, MONEY, MONEY, QTY, MONEY, MONEY, PCT, PCT, MONEY, PCT, None, MONEY, None, None],
          [[r.sku, r.name, r.category, r.price, r.cost, r.margin, r.units, r.total_contribution, r.total_cost,
            r.margin_pct, r.mix_share, r.cat_avg_margin, r.popularity_threshold, r.menu_class, r.total_sales,
            r.profit_category, r.popularity_category]
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
    sf_values = {(r["kind"], r["sku"]): r["value"] for r in result.sf_stock}
    sheet("Stock counts", ["Count", "SKU", "Description", "Value", "Semi-finished qty", "Unit"],
          [None, None, None, MONEY, QTY],
          [[c.get_kind_display(), c.sku, c.description,
            sf_values.get((c.kind, c.sku), 0.0) if c.semi_finished_id else float(c.value),
            float(c.qty) if c.semi_finished_id else None, c.unit if c.semi_finished_id else ""]
           for c in audit.stock_counts.select_related("item", "semi_finished").order_by("kind", "sku")])

    buf = BytesIO()
    wb.save(buf)
    name = f"{audit.brand.code}-{audit.outlet.code}-{audit.period}-COGS.xlsx".replace(" ", "_")
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


# Single-line fixes on the "Uploaded data" tab (finding C-25) ------------------------------------------

LINE_PERMISSIONS = {
    "sale": "audit.change_saleline",
    "purchase": "audit.change_purchaseline",
    "count": "audit.change_stockcount",
    "production": "audit.change_productionline",
    "swap": "audit.change_auditsubstitution",
}


def _find_sku(sku):
    """An item master SKU, ignoring case and spaces, or a ValidationError with a suggestion."""
    items, skus = importers._item_lookup()
    item = items.get(importers.norm(sku))
    if item is None:
        import difflib

        close = difflib.get_close_matches(sku.strip().upper(), skus, n=1, cutoff=0.75)
        raise forms.ValidationError(f"{sku} isn't in the item master." + (f" Did you mean {close[0]}?" if close else ""))
    return item


class SaleLineForm(forms.ModelForm):
    class Meta:
        model = SaleLine
        fields = ["pos_name", "menu_item", "qty"]
        labels = {"pos_name": "POS name", "qty": "Quantity sold"}

    def __init__(self, *a, audit, **kw):
        super().__init__(*a, **kw)
        self.fields["menu_item"].queryset = MenuItem.objects.filter(brand=audit.brand).order_by("name")
        self.fields["menu_item"].help_text = "Leave empty if it isn't on the menu (it is then flagged)."


class PurchaseLineForm(forms.ModelForm):
    class Meta:
        model = PurchaseLine
        fields = ["source", "invoice_date", "invoice_number", "supplier", "sku", "description", "qty", "value"]
        labels = {"sku": "SKU", "qty": "Quantity (packs)", "value": "Line amount (RM)"}
        widgets = {"invoice_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *a, audit, **kw):
        super().__init__(*a, **kw)
        self.fields["sku"].widget.attrs["list"] = "item-skus"

    def clean_sku(self):
        self.item = _find_sku(self.cleaned_data["sku"])
        return self.item.sku

    def save(self, commit=True):
        self.instance.item = self.item
        return super().save(commit)


class StockCountForm(forms.ModelForm):
    class Meta:
        model = StockCount
        fields = ["kind", "sku", "description", "value", "qty"]
        labels = {"kind": "Count", "sku": "SKU", "value": "Total RM value", "qty": "Semi-finished qty"}

    def __init__(self, *a, audit, **kw):
        super().__init__(*a, **kw)
        self.audit = audit
        self.fields["sku"].widget.attrs["list"] = "item-skus"
        self.fields["sku"].help_text = "An item master SKU, or a semi-finished SKU of this brand."
        self.fields["value"].required = False
        self.fields["value"].help_text = "Supplier items: the RM value counted. Leave empty for a semi-finished item."
        self.fields["qty"].required = False
        self.fields["qty"].help_text = ("Semi-finished items only: the weight counted, in the unit its recipe makes "
                                        "(g, ml, pcs). It is broken down into its ingredients through the recipe.")

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        self.sf = SemiFinished.objects.filter(brand=self.audit.brand, sku__iexact=sku).select_related("recipe").first()
        if self.sf is not None:
            if not hasattr(self.sf, "recipe"):
                raise forms.ValidationError(f"{self.sf.sku} has no recipe. Every semi-finished item needs a recipe "
                                            "and batch yield before it can be counted.")
            self.item = None
            return self.sf.sku
        self.item = _find_sku(sku)
        return self.item.sku

    def clean(self):
        data = super().clean()
        if "sku" in self.errors:
            return data
        value, qty = data.get("value"), data.get("qty")
        if any(x is not None and x < 0 for x in (value, qty)):
            raise forms.ValidationError("A stock count can't be negative.")
        if self.sf is not None:
            if qty is None:
                self.add_error("qty", f"{self.sf.sku} is made in-house: enter the weight counted "
                                      f"({self.sf.output_uom or 'in the unit its recipe makes'}).")
            elif value:
                self.add_error("value", "Leave the RM value empty: a semi-finished item is valued from its recipe.")
            data["value"] = Decimal("0")
        else:
            if value is None:
                self.add_error("value", "Enter the RM value counted (0 if none is left).")
            elif qty:
                self.add_error("qty", f"{self.item.sku} is a supplier item: count it by RM value, not semi-finished qty.")
            data["qty"] = Decimal("0")
        return data

    def save(self, commit=True):
        self.instance.item = self.item
        self.instance.semi_finished = self.sf
        if not self.instance.description:
            self.instance.description = (self.sf or self.item).name
        return super().save(commit)


class ProductionLineForm(forms.ModelForm):
    component = forms.CharField(
        label="SKU", help_text="A menu item, semi-finished item or item master SKU. Menu and semi-finished items "
                               "are counted in portions; items in their base unit (g, ml, pcs).")

    class Meta:
        model = ProductionLine
        fields = ["kind", "component", "qty", "customer", "note"]
        labels = {"kind": "Type", "qty": "Quantity"}

    def __init__(self, *a, audit, **kw):
        super().__init__(*a, **kw)
        self.audit = audit
        self.fields["component"].widget.attrs["list"] = "component-skus"
        if self.instance.kind != "b2b":
            # B2B sales come from the invoice upload (which replaces them), not from typed lines.
            self.fields["kind"].choices = [c for c in self.fields["kind"].choices if c[0] != "b2b"]
        if self.instance.pk:
            self.fields["component"].initial = self.instance.sku

    def clean_component(self):
        sku = self.cleaned_data["component"].strip()
        brand = self.audit.brand
        self.target = (MenuItem.objects.filter(brand=brand, sku__iexact=sku).first()
                       or SemiFinished.objects.filter(brand=brand, sku__iexact=sku).first())
        if self.target is None:
            self.target = Item.objects.filter(sku__iexact=sku).first()
        if self.target is None:
            import difflib

            skus = (list(MenuItem.objects.filter(brand=brand).values_list("sku", flat=True))
                    + list(SemiFinished.objects.filter(brand=brand).values_list("sku", flat=True))
                    + list(Item.objects.values_list("sku", flat=True)))
            close = difflib.get_close_matches(sku.upper(), skus, n=1, cutoff=0.6)
            raise forms.ValidationError(f"{sku} isn't a {brand} menu item, semi-finished item or item master SKU."
                                        + (f" Did you mean {close[0]}?" if close else ""))
        line = self.instance  # set before the model's own check that exactly one is filled in
        line.menu_item = self.target if isinstance(self.target, MenuItem) else None
        line.semi_finished = self.target if isinstance(self.target, SemiFinished) else None
        line.item = self.target if isinstance(self.target, Item) else None
        return self.target.sku

    def _post_clean(self):
        if "component" not in self._errors:  # don't add "pick exactly one" on top of "unknown SKU"
            super()._post_clean()


class AuditSubstitutionForm(forms.ModelForm):
    original_sku = forms.CharField(label="Item in the recipe", help_text="The item the recipes call for (it ran out).")
    substitute_sku = forms.CharField(
        label="Bought instead", required=False,
        help_text="The item used in its place this month. Leave empty to turn off the standing swap for this "
                  "item this month, so recipes use it as written.")

    class Meta:
        model = AuditSubstitution
        fields = ["original_sku", "substitute_sku", "factor", "note"]
        labels = {"factor": "Units per 1 recipe unit"}

    def __init__(self, *a, audit, **kw):
        super().__init__(*a, **kw)
        self.audit = audit
        for f in ("original_sku", "substitute_sku"):
            self.fields[f].widget.attrs["list"] = "item-skus"
        if self.instance.pk:
            self.fields["original_sku"].initial = self.instance.original.sku
            self.fields["substitute_sku"].initial = self.instance.substitute.sku if self.instance.substitute_id else ""

    def clean_original_sku(self):
        item = _find_sku(self.cleaned_data["original_sku"])
        clash = AuditSubstitution.objects.filter(audit=self.audit, original=item).exclude(pk=self.instance.pk).first()
        if clash:
            raise forms.ValidationError(f"{item.sku} already has a swap this month ({clash}). Edit that one instead.")
        self.instance.original = item
        return item.sku

    def clean_substitute_sku(self):
        sku = (self.cleaned_data.get("substitute_sku") or "").strip()
        self.instance.substitute = _find_sku(sku) if sku else None
        return self.instance.substitute.sku if self.instance.substitute else ""


LINE_FORMS = {
    "sale": (SaleLineForm, "POS sales line", ["pos_name", "menu_item", "qty"]),
    "purchase": (PurchaseLineForm, "Purchase line",
                 ["source", "invoice_date", "invoice_number", "supplier", "sku", "description", "qty", "value"]),
    "count": (StockCountForm, "Stock count line", ["kind", "sku", "description", "value", "qty"]),
    "production": (ProductionLineForm, "Production / wastage line", ["kind", "sku", "qty", "customer", "note"]),
    "swap": (AuditSubstitutionForm, "Swap this month", ["original", "substitute", "factor", "note"]),
}


def _line_repr(kind, line):
    if kind == "swap":
        return str(line)
    if kind == "sale":
        return line.pos_name
    if kind == "purchase":
        return f"{line.invoice_number or 'no invoice'} {line.sku}"
    if kind == "count":
        return f"{line.get_kind_display()} {line.sku}"
    return f"{line.get_kind_display()} {line.sku}"


@login_required
def line_edit(request, pk, kind, line_pk=None):
    """Add, correct or delete one line without re-uploading the whole file. Every change is logged."""
    if kind not in LINE_FORMS:
        raise Http404("No such line type.")
    if not request.user.has_perm(LINE_PERMISSIONS[kind]):
        raise PermissionDenied
    audit = access.get_audit(request.user, pk)
    form_class, label, fields = LINE_FORMS[kind]
    model = form_class._meta.model
    line = model.objects.filter(audit=audit, pk=line_pk).first() if line_pk else model(audit=audit)
    if line is None:
        raise Http404("No such line.")
    back = reverse("audit_inputs", args=[audit.pk]) + f"#{kind}"
    if audit.locked:
        messages.error(request, "This audit is signed off and locked. Unlock it to change lines.")
        return redirect(back)
    before = history.snapshot(line, fields) if line.pk else {}
    if request.method == "POST" and request.POST.get("action") == "delete" and line.pk:
        line.delete()
        history.log(request.user, "delete", label, _line_repr(kind, line), audit,
                    changes={f: [v, None] for f, v in before.items() if v not in (None, "")})
        messages.success(request, f"{label} deleted: {_line_repr(kind, line)}.")
        return redirect(back)
    initial = {}
    if not line.pk and kind == "count":
        initial["kind"] = request.GET.get("count", "closing")
    if not line.pk and kind == "production":
        initial["kind"] = "wastage"
    form = form_class(request.POST or None, instance=line, audit=audit, initial=initial)
    if request.method == "POST" and form.is_valid():
        adding = not line.pk
        line = form.save()
        after = history.snapshot(line, fields)
        history.log(request.user, "add" if adding else "change", label, _line_repr(kind, line), audit,
                    changes=history.diff(before, after))
        messages.success(request, f"{label} {'added' if adding else 'saved'}: {_line_repr(kind, line)}.")
        return redirect(back)
    ctx = {"audit": audit, "form": form, "label": label, "line": line, "kind": kind, "back": back}
    if kind in ("purchase", "count", "production", "swap"):
        ctx["item_skus"] = list(Item.objects.filter(active=True).values_list("sku", "name"))
    if kind == "count":
        ctx["item_skus"] = list(SemiFinished.objects.filter(brand=audit.brand, active=True)
                                .values_list("sku", "name")) + ctx["item_skus"]
    if kind == "production":
        ctx["brand_skus"] = list(MenuItem.objects.filter(brand=audit.brand).values_list("sku", "name")) + list(
            SemiFinished.objects.filter(brand=audit.brand).values_list("sku", "name"))
    return render(request, "audit/line_form.html", ctx)
