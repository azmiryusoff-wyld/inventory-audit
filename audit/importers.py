"""Read the files the team already exports, and load them into the database.

Supported, as they come out of today's systems:
  * POS "Item Sales Report Summary"   (Category / Group, Item, Quantity, Full Name)
  * Xero invoice export                (InventoryItemCode, Description, Quantity, LineAmount ...)
  * Outlet stock count form            (SKU, Item Name, ..., Total RM Value)
  * Central item master                (the '20 Item Master' tab)
  * The workbook's own master tabs     (21 SF Master, 22 Recipe Master, 23 Menu Master, 15 Substitutions)

Every reader accepts .xlsx or .csv, finds its header row by name rather than
by position, and returns plain dicts. The ``load_*`` functions write them.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------


def to_decimal(value, default=Decimal("0")) -> Decimal:
    """Parse sheet-formatted numbers: 'RM1,234.50', '(RM2.00)', '100.00%', '-', ''."""
    if value is None:
        return default
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    s = str(value).strip()
    if s in ("", "-", "—", "#N/A", "#DIV/0!", "#REF!", "#VALUE!"):
        return default
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s.startswith("-"):
        negative, s = True, s[1:]
    percent = s.endswith("%")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return default
    try:
        d = Decimal(s)
    except InvalidOperation:
        return default
    if percent:
        d /= 100
    return -d if negative else d


_NUMBER = re.compile(r"^(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?$")


def parse_number(value) -> Decimal | None:
    """Strict reading of one number cell from an upload.

    Accepts 12, 1234.5, 1,234.50, RM 1,234.50, (2.00), -3, 95%. Blank or '-' gives None.
    Anything else (text, '12,5', '1.234,50', '1e3', '5.00-') raises ValueError, so a typo
    is reported instead of quietly becoming 0.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    s = str(value).strip()
    if s in ("", "-", "—"):
        return None
    t = re.sub(r"\s+", "", s)
    negative = t.startswith("(") and t.endswith(")")
    if negative:
        t = t[1:-1]
    if t.startswith("-"):
        negative, t = not negative, t[1:]
    t = re.sub(r"^(RM|MYR)", "", t, flags=re.IGNORECASE)
    if t.startswith("-"):
        negative, t = not negative, t[1:]
    percent = t.endswith("%")
    if percent:
        t = t[:-1]
    if not t or t == "." or not _NUMBER.match(t):
        raise ValueError(f"'{s}' is not a number")
    d = Decimal(t.replace(",", ""))
    if percent:
        d /= 100
    return -d if negative else d


def parse_date(value) -> date | None:
    """Like to_date, but a cell that has something in it and isn't a date raises ValueError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    d = to_date(value)
    if d is None:
        raise ValueError(f"'{clean(value)}' is not a date (use DD/MM/YYYY)")
    return d


class RowErrors(ValueError):
    """Rows in an upload that can't be read. Nothing from the file is loaded."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        shown = "; ".join(errors[:10]) + (f"; and {len(errors) - 10} more" if len(errors) > 10 else "")
        super().__init__(f"Nothing was loaded. Fix {'this row' if len(errors) == 1 else 'these rows'} "
                         f"in the file and upload it again: {shown}")


def to_bool(value, default=True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in ("TRUE", "YES", "Y", "1")


def to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def norm(value) -> str:
    """Key used to match names: case- and spacing-insensitive."""
    return clean(value).casefold()


UPLOAD_EXTENSIONS = (".xlsx", ".xlsm", ".csv", ".txt")


def read_rows(file, sheet: str | None = None) -> list[list]:
    """Return every row of a .xlsx (optionally one named tab) or .csv as lists.

    Anything that can't be read raises ValueError with a message a person can act on.
    """
    name = getattr(file, "name", str(file)).lower()
    if not name.endswith(UPLOAD_EXTENSIONS):
        raise ValueError("This isn't an Excel (.xlsx) or CSV file. Export the report as .xlsx or .csv and upload that.")
    if name.endswith((".xlsx", ".xlsm")):
        import zipfile

        import openpyxl

        try:
            wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
        except (zipfile.BadZipFile, OSError, KeyError, ValueError) as exc:
            raise ValueError("This file can't be opened as an Excel workbook. Open it in Excel or Google Sheets, "
                             "save it again as .xlsx (or .csv), and upload that.") from exc
        if sheet and sheet not in wb.sheetnames:
            raise ValueError(f"The workbook has no tab called '{sheet}'.")
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
        return rows
    if hasattr(file, "read"):
        raw = file.read()
    else:
        with open(file, "rb") as fh:
            raw = fh.read()
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")  # CSVs saved by older Excel on Windows
    else:
        text = raw
    head = "\n".join(text.splitlines()[:20])
    # Excel in some regional settings saves "CSV" with semicolons; a few tools export tabs.
    delimiter = max([",", ";", "\t"], key=lambda d: (head.count(d), d == ","))
    try:
        return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    except csv.Error as exc:
        raise ValueError(f"This CSV file can't be read ({exc}). Save it again from Excel as CSV and upload that.") from exc


def find_header(rows: list[list], required: list[str], scan: int = 30) -> tuple[int, dict[str, int]]:
    """Find the first row containing every required column name (case-insensitive)."""
    want = [norm(r) for r in required]
    for i, row in enumerate(rows[:scan]):
        cells = [norm(c) for c in row]
        if all(w in cells for w in want):
            return i, {c: j for j, c in enumerate(cells) if c}
    raise ValueError(f"Could not find a header row with columns: {', '.join(required)}")


def cell(row: list, cols: dict[str, int], name: str):
    j = cols.get(norm(name))
    if j is None or j >= len(row):
        return None
    return row[j]


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------


class PosLocationError(ValueError):
    pass


# Header names that give away which export a file is, so a file in the wrong upload slot is caught.
OTHER_FILE_HINTS = {
    "inventoryitemcode": "a Xero invoice export. Upload it under Supplier purchases or Inter-company purchases",
    "total rm value": "a stock count. Upload it under Opening or Closing stock count",
}
TOTAL_ROW_NAMES = {"total", "subtotal", "sub total", "grand total"}


def _wrong_file(rows: list[list], allowed: set[str] = frozenset()) -> None:
    for row in rows[:30]:
        cells = {norm(c) for c in row}
        for hint, what in OTHER_FILE_HINTS.items():
            if hint in cells and hint not in allowed:
                raise ValueError(f"This looks like {what}.")


def read_pos_sales(file, location: str = "") -> list[dict]:
    """POS item sales export -> [{'pos_name', 'qty'}], summed by name.

    Handles the three shapes seen so far:
      * Item Sales Report Summary: Category / Group, Item, Quantity, Full Name (one outlet)
      * Item Sales (Count_Gross): Item, '<LOCATION> (Count)', '<LOCATION> (Price)', ... (several outlets;
        ``location`` picks the outlet's Count column)
      * a bare two-column paste: name, quantity
    A quantity that isn't a number stops the upload with the row number; Total rows are left out.
    """
    rows = read_rows(file)
    _wrong_file(rows)
    totals: dict[str, tuple[str, Decimal]] = {}
    errors: list[str] = []

    def add(line_no, name, q):
        name = clean(name)
        if not name or norm(name) in TOTAL_ROW_NAMES:
            return
        try:
            qty = parse_number(q)
        except ValueError as exc:
            errors.append(f"Row {line_no}: quantity {exc} ({name})")
            return
        if qty is None:
            return
        key = norm(name)
        prev = totals.get(key, (name, Decimal("0")))
        totals[key] = (prev[0], prev[1] + qty)

    header = None
    for i, row in enumerate(rows[:30]):
        cells = [norm(c) for c in row]
        if "quantity" in cells or any(c.endswith("(count)") for c in cells):
            header = i, {c: j for j, c in enumerate(cells) if c}
            break
    if header is None:
        # A bare paste: exactly a name and a quantity on every line.
        filled = [(i, [c for c in r if clean(c)]) for i, r in enumerate(rows)]
        filled = [(i, cells) for i, cells in filled if cells]
        if not filled or any(len(cells) != 2 for _, cells in filled):
            raise ValueError(
                "Could not find the sales columns. Expected the POS Item Sales Report Summary "
                "(with 'Full Name' and 'Quantity' columns), the multi-outlet report (with "
                "'<outlet> (Count)' columns), or two columns: item name and quantity."
            )
        for n, (i, (name, q)) in enumerate(filled):
            try:
                parse_number(q)
            except ValueError:
                if n == 0:
                    continue  # a heading line such as "Item, Qty"
            add(i + 1, name, q)
    else:
        start, cols = header
        count_cols = {c[: -len("(count)")].strip(): j for c, j in cols.items() if c.endswith("(count)")}
        if count_cols:
            names = ", ".join(sorted(k.upper() for k in count_cols))
            if location and norm(location) in count_cols:
                qty_col = count_cols[norm(location)]
            elif len(count_cols) == 1 and not location:
                qty_col = next(iter(count_cols.values()))
            elif location:
                raise PosLocationError(
                    f"This report has no column for this outlet's POS location ({location}). It covers: "
                    f"{names}. Upload the report for this outlet, or correct the outlet's POS location "
                    "under Master data > Outlets."
                )
            else:
                raise PosLocationError(
                    f"This report covers several outlets ({names}). Set the outlet's POS location "
                    "under Master data > Outlets to one of these names."
                )
            item_col = cols.get("item", 0)
            for i, r in enumerate(rows[start + 1:], start=start + 2):
                if len(r) > max(item_col, qty_col):
                    add(i, r[item_col], r[qty_col])
        else:
            has_full = norm("Full Name") in cols
            for i, r in enumerate(rows[start + 1:], start=start + 2):
                if has_full:
                    name = cell(r, cols, "Full Name")
                else:
                    group, item = clean(cell(r, cols, "Category / Group")), clean(cell(r, cols, "Item"))
                    name = f"{group} - {item}" if group and item else group or item
                add(i, name, cell(r, cols, "Quantity"))
    if errors:
        raise RowErrors(errors)
    return [{"pos_name": n, "qty": q} for n, q in totals.values() if q]


def read_xero_invoices(file) -> list[dict]:
    """Xero invoice line export -> purchase lines.

    Lines with no item code are returned with sku "" so the loader can report them (with their
    value) instead of dropping them unseen. Blank lines are ignored.
    """
    rows = read_rows(file)
    _wrong_file(rows, allowed={"inventoryitemcode"})
    start, cols = find_header(rows, ["InventoryItemCode", "Quantity", "LineAmount"])
    out, errors = [], []
    for i, r in enumerate(rows[start + 1:], start=start + 2):
        sku = clean(cell(r, cols, "InventoryItemCode"))
        description = clean(cell(r, cols, "Description"))
        raw_qty, raw_value = cell(r, cols, "Quantity"), cell(r, cols, "LineAmount")
        if not sku and not description and not clean(raw_value) and not clean(raw_qty):
            continue
        label = sku or description or "line"
        before = len(errors)
        try:
            qty = parse_number(raw_qty)
        except ValueError as exc:
            errors.append(f"Row {i}: Quantity {exc} ({label})")
            qty = None
        try:
            value = parse_number(raw_value)
        except ValueError as exc:
            errors.append(f"Row {i}: LineAmount {exc} ({label})")
            value = None
        try:
            invoice_date = parse_date(cell(r, cols, "InvoiceDate"))
        except ValueError as exc:
            errors.append(f"Row {i}: InvoiceDate {exc} ({label})")
            invoice_date = None
        if sku and value is None and len(errors) == before:
            errors.append(f"Row {i}: LineAmount is blank ({label})")
        out.append({
            "sku": sku,
            "description": description,
            "qty": qty or Decimal("0"),
            "value": value or Decimal("0"),
            "invoice_number": clean(cell(r, cols, "InvoiceNumber")),
            "invoice_date": invoice_date,
            "supplier": clean(cell(r, cols, "ContactName")),
        })
    if errors:
        raise RowErrors(errors)
    return out


def read_stock_count(file) -> list[dict]:
    """Outlet stock count form -> [{'sku', 'description', 'value'}].

    A counted line needs a value (0 if nothing is left); a blank or negative value stops the upload.
    Lines with a value but no SKU come back with sku "" so the loader can report them.
    """
    rows = read_rows(file)
    _wrong_file(rows, allowed={"total rm value"})
    start, cols = find_header(rows, ["SKU", "Total RM Value"])
    out, errors = [], []
    for i, r in enumerate(rows[start + 1:], start=start + 2):
        sku = clean(cell(r, cols, "SKU"))
        description = clean(cell(r, cols, "Item Name"))
        raw = cell(r, cols, "Total RM Value")
        label = sku or description
        try:
            value = parse_number(raw)
        except ValueError as exc:
            errors.append(f"Row {i}: Total RM Value {exc} ({label})")
            continue
        if not sku:
            if value:
                out.append({"sku": "", "description": description, "value": value})
            continue
        if value is None:
            errors.append(f"Row {i}: Total RM Value is blank ({label}). Enter 0 if none is left")
        elif value < 0:
            errors.append(f"Row {i}: Total RM Value can't be negative ({label}: {raw})")
        else:
            out.append({"sku": sku, "description": description, "value": value})
    if errors:
        raise RowErrors(errors)
    return out


ITEM_MASTER_COLUMNS = {
    "sku": "SKU",
    "name": "Item Name",
    "category": "Category",
    "pack_size": "Pack Size (base units per pack)",
    "base_uom": "Base UOM",
    "pack_uom": "Pack UOM",
    "purchase_price": "Purchase Price (per pack)",
    "yield_pct": "Yield %",
    "active": "Active",
    "intermediate_pack_size": "Pack Size (base units per intermediate)",
    "intermediate_uom": "Intermediate UOM",
    "supplier": "SUPPLIER 1",
}


def _category(value) -> str:
    """Match the report's categories ignoring case and spacing; anything else is kept as typed."""
    from .engine import DEFAULT_CATEGORIES

    text = clean(value) or "Other"
    return next((c for c in DEFAULT_CATEGORIES if norm(c) == norm(text)), text)


def parse_item_master(rows: list[list]) -> list[dict]:
    start, cols = find_header(rows, ["SKU", "Item Name", "Pack Size (base units per pack)"])
    out = []
    for r in rows[start + 1:]:
        sku = clean(cell(r, cols, "SKU"))
        if not sku:
            continue
        y = cell(r, cols, "Yield %")
        yield_pct = to_decimal(y, default=Decimal("1"))
        if yield_pct > 1 and not str(y).strip().endswith("%"):
            yield_pct /= 100  # typed as 95 rather than 95%
        inter = cell(r, cols, "Pack Size (base units per intermediate)")
        out.append({
            "sku": sku,
            "name": clean(cell(r, cols, "Item Name")),
            "category": _category(cell(r, cols, "Category")),
            "pack_size": to_decimal(cell(r, cols, "Pack Size (base units per pack)")),
            "base_uom": clean(cell(r, cols, "Base UOM")),
            "pack_uom": clean(cell(r, cols, "Pack UOM")),
            "purchase_price": to_decimal(cell(r, cols, "Purchase Price (per pack)")),
            "yield_pct": yield_pct,
            "active": to_bool(cell(r, cols, "Active")),
            "intermediate_pack_size": to_decimal(inter, default=None) if clean(inter) else None,
            "intermediate_uom": clean(cell(r, cols, "Intermediate UOM")),
            "supplier": clean(cell(r, cols, "SUPPLIER 1")),
        })
    return out


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def __str__(self):
        text = f"{self.created} created, {self.updated} updated, {self.skipped} skipped"
        if self.warnings:
            text += f", {len(self.warnings)} warnings"
        return text


@transaction.atomic
def load_group_substitutions(rows: list[list]) -> ImportReport:
    """'15 Substitutions' in the central workbook: applies to every brand. Replaces the group-wide set."""
    from .models import Item, Substitution

    report = ImportReport()
    if not rows:
        return report
    _, cols = find_header(rows, ["Original SKU", "Substitute SKU"])
    factor_col = next((j for k, j in cols.items() if k.startswith("conversion factor")), None)
    items = {i.sku: i for i in Item.objects.all()}
    Substitution.objects.filter(brand__isnull=True).delete()
    for r in rows[1:]:
        o, s = clean(cell(r, cols, "Original SKU")), clean(cell(r, cols, "Substitute SKU"))
        if not o:
            continue
        if o not in items or s not in items:
            report.skipped += 1
            report.warnings.append(f"Substitution {o} -> {s}: SKU not in item master")
            continue
        factor = to_decimal(r[factor_col] if factor_col is not None and factor_col < len(r) else 1,
                            default=Decimal("1"))
        Substitution.objects.create(brand=None, original=items[o], substitute=items[s], factor=factor,
                                    note=clean(cell(r, cols, "Note")))
        report.created += 1
    return report


@transaction.atomic
def load_item_master(rows: list[list]) -> ImportReport:
    """Upsert items from the central item master. Items missing from the file are deactivated."""
    from .models import Item

    report = ImportReport()
    now = timezone.now()
    seen = set()
    for data in parse_item_master(rows):
        if data["sku"] in seen:
            report.warnings.append(f"Duplicate SKU {data['sku']} in item master; kept the first row")
            report.skipped += 1
            continue
        seen.add(data["sku"])
        _, created = Item.objects.update_or_create(sku=data["sku"], defaults={**data, "synced_at": now})
        report.created += created
        report.updated += not created
    from .engine import DEFAULT_CATEGORIES

    odd = sorted({d["category"] for d in parse_item_master(rows) if d["category"] not in DEFAULT_CATEGORIES})
    if odd:
        report.warnings.append(
            "Categories not in the COGS report (" + ", ".join(odd) + "). Items in them are not counted in "
            "Food & Beverage; change them in the central item master to one of: " + ", ".join(DEFAULT_CATEGORIES)
        )
    gone = Item.objects.exclude(sku__in=seen).filter(active=True).update(active=False)
    if gone:
        report.warnings.append(f"{gone} items no longer in the item master were marked inactive")
    return report


def _menu_lookup(brand):
    from .models import MenuItem, PosAlias

    items = list(MenuItem.objects.filter(brand=brand))
    lookup = {norm(m.name): m for m in items}
    for a in PosAlias.objects.filter(menu_item__brand=brand).select_related("menu_item"):
        lookup.setdefault(norm(a.pos_name), a.menu_item)
    # Reports that list items without their variant: "8oz Mug" for "8oz Mug - Regular Price".
    suffix = " - regular price"
    for m in items:
        n = norm(m.name)
        if n.endswith(suffix):
            lookup.setdefault(n[: -len(suffix)], m)
    return lookup


def _lock_open_audit(audit):
    """Lock the audit row until the upload commits, so two uploads of the same file can't interleave."""
    from .models import AuditPeriod

    locked = AuditPeriod.objects.select_for_update().get(pk=audit.pk)
    if locked.locked:
        raise ValueError("This audit is signed off and locked. Nothing was changed.")
    return locked


def _no_lines(what):
    return ValueError(f"No {what} were found in this file, so nothing was changed. "
                      "Check you picked the right file.")


def _money(d) -> str:
    return f"{d:,.2f}"


def load_pos_sales(audit, file, replace=True) -> ImportReport:
    from .models import SaleLine

    rows = read_pos_sales(file, location=audit.outlet.pos_location)
    if not rows:
        raise _no_lines("sales lines")
    report = ImportReport()
    with transaction.atomic():
        audit = _lock_open_audit(audit)
        if replace:
            audit.sales.all().delete()
        menu = _menu_lookup(audit.brand)
        lines = []
        for row in rows:
            m = menu.get(norm(row["pos_name"]))
            lines.append(SaleLine(audit=audit, pos_name=row["pos_name"], menu_item=m, qty=row["qty"]))
            if m is None:
                report.warnings.append(f"Not on the menu: {row['pos_name']}")
            if row["qty"] < 0:
                report.warnings.append(f"Negative quantity (more refunds than sales): {row['pos_name']} {row['qty']}")
        SaleLine.objects.bulk_create(lines)
        report.created = len(lines)
    return report


def load_purchases(audit, file, source="supplier", replace=True) -> ImportReport:
    from collections import Counter

    from .models import Item, PurchaseLine

    rows = read_xero_invoices(file)
    report = ImportReport()
    skip = [norm(x) for x in settings.PURCHASE_SKIP_DESCRIPTIONS]
    keep, no_code = [], []
    for row in rows:
        if any(x in norm(row["description"]) for x in skip):
            report.skipped += 1  # e.g. delivery handling charges: not stock
        elif not row["sku"]:
            no_code.append(row)
        else:
            keep.append(row)
    if not keep:
        raise _no_lines("invoice lines with an item code")
    if no_code:
        total = sum(r["value"] for r in no_code)
        report.warnings.append(
            f"{len(no_code)} line{'s' if len(no_code) > 1 else ''} with no InventoryItemCode not loaded "
            f"({_money(total)} in total): "
            + ", ".join(f"{r['description'] or 'no description'} {_money(r['value'])}" for r in no_code[:5])
            + (" ..." if len(no_code) > 5 else "")
        )
    year, month = (int(x) for x in audit.period.split("-"))
    outside = [r for r in keep if r["invoice_date"] and (r["invoice_date"].year, r["invoice_date"].month) != (year, month)]
    if outside:
        report.warnings.append(
            f"{len(outside)} line{'s' if len(outside) > 1 else ''} dated outside {audit.period} "
            "(loaded, and flagged under Data checks)"
        )
    dupes = Counter((r["invoice_number"], r["invoice_date"], r["sku"], r["qty"], r["value"]) for r in keep)
    for (inv, _, sku, _, value), n in dupes.items():
        if n > 1:
            report.warnings.append(f"Possible duplicate: {inv or 'no invoice no.'} {sku} {_money(value)} appears {n} times")
    for r in keep:
        if r["value"] < 0 or r["qty"] < 0:
            report.warnings.append(f"Negative line (credit note?): {r['invoice_number']} {r['sku']} {_money(r['value'])}")
    with transaction.atomic():
        audit = _lock_open_audit(audit)
        if replace:
            audit.purchases.filter(source=source).delete()
        items = {i.sku: i for i in Item.objects.all()}
        lines = []
        for row in keep:
            item = items.get(row["sku"])
            lines.append(PurchaseLine(audit=audit, source=source, item=item, **row))
            msg = f"Unknown SKU {row['sku']} ({row['description']})"
            if item is None and msg not in report.warnings:
                report.warnings.append(msg)
        PurchaseLine.objects.bulk_create(lines)
        report.created = len(lines)
    return report


def load_stock_count(audit, file, kind, replace=True) -> ImportReport:
    from collections import Counter

    from .models import Item, StockCount

    rows = read_stock_count(file)
    report = ImportReport()
    keep = [r for r in rows if r["sku"]]
    no_sku = [r for r in rows if not r["sku"]]
    if not keep:
        raise _no_lines("stock count lines with a SKU")
    if no_sku:
        report.warnings.append(
            f"{len(no_sku)} line{'s' if len(no_sku) > 1 else ''} with a value but no SKU not loaded "
            f"({_money(sum(r['value'] for r in no_sku))} in total): "
            + ", ".join(f"{r['description'] or 'no name'} {_money(r['value'])}" for r in no_sku[:5])
        )
    for sku, n in Counter(r["sku"] for r in keep).items():
        if n > 1:
            report.warnings.append(f"{sku} is on {n} rows; their values were added together")
    with transaction.atomic():
        audit = _lock_open_audit(audit)
        if replace:
            audit.stock_counts.filter(kind=kind).delete()
        items = {i.sku: i for i in Item.objects.all()}
        lines = []
        for row in keep:
            item = items.get(row["sku"])
            lines.append(StockCount(audit=audit, kind=kind, item=item, **row))
            msg = f"Unknown SKU {row['sku']} ({row['description']})"
            if item is None and msg not in report.warnings:
                report.warnings.append(msg)
        StockCount.objects.bulk_create(lines)
        report.created = len(lines)
    return report


@transaction.atomic
def load_workbook_masters(brand, file) -> dict[str, ImportReport]:
    """One-off migration: read a brand's workbook (.xlsx download) into the app.

    Reads 21 SF Master, 23 Menu Master, 22 Recipe Master and 15 Substitutions.
    Existing rows for the brand with the same SKU are updated; recipes are rebuilt.
    """
    import openpyxl

    from .models import Item, MenuItem, Recipe, RecipeLine, SemiFinished, Substitution

    wb = openpyxl.load_workbook(file, read_only=True, data_only=True)

    def tab(name):
        return [list(r) for r in wb[name].iter_rows(values_only=True)] if name in wb.sheetnames else []

    reports = {k: ImportReport() for k in ("semi_finished", "menu", "recipes", "substitutions")}

    rows = tab("21 SF Master")
    if rows:
        _, cols = find_header(rows, ["SF SKU", "SF Name"])
        for r in rows[1:]:
            sku = clean(cell(r, cols, "SF SKU"))
            if not sku:
                continue
            _, created = SemiFinished.objects.update_or_create(
                brand=brand, sku=sku,
                defaults={"name": clean(cell(r, cols, "SF Name")),
                          "output_uom": clean(cell(r, cols, "Output UOM"))},
            )
            reports["semi_finished"].created += created
            reports["semi_finished"].updated += not created

    rows = tab("23 Menu Master")
    if rows:
        _, cols = find_header(rows, ["Menu SKU", "Menu Name"])
        seen_menu = set()
        for r in rows[1:]:
            sku = clean(cell(r, cols, "Menu SKU"))
            if not sku:
                continue
            if sku in seen_menu:
                reports["menu"].skipped += 1
                reports["menu"].warnings.append(
                    f"Menu SKU {sku} is used twice; kept the first, skipped '{clean(cell(r, cols, 'Menu Name'))}'"
                )
                continue
            seen_menu.add(sku)
            price = to_decimal(cell(r, cols, "Unit Price"), default=None)
            _, created = MenuItem.objects.update_or_create(
                brand=brand, sku=sku,
                defaults={"name": clean(cell(r, cols, "Menu Name")),
                          "price": price or None,
                          "category": clean(cell(r, cols, "Menu Category")),
                          "note": clean(cell(r, cols, "Note"))},
            )
            reports["menu"].created += created
            reports["menu"].updated += not created

    items = {i.sku: i for i in Item.objects.all()}
    items_by_name = {norm(i.name): i for i in items.values()}
    sfs = {s.sku: s for s in SemiFinished.objects.filter(brand=brand)}
    sfs_by_name = {norm(s.name): s for s in sfs.values()}
    menus = {m.sku: m for m in MenuItem.objects.filter(brand=brand)}
    menus_by_name = {norm(m.name): m for m in menus.values()}

    rows = tab("22 Recipe Master")
    if rows:
        rep = reports["recipes"]
        _, cols = find_header(rows, ["Parent Name", "Component Name"])
        Recipe.objects.filter(brand=brand).delete()
        recipes: dict[str, Recipe] = {}
        for r in rows[1:]:
            p_sku, p_name = clean(cell(r, cols, "Parent SKU")), clean(cell(r, cols, "Parent Name"))
            c_sku, c_name = clean(cell(r, cols, "Component SKU")), clean(cell(r, cols, "Component Name"))
            if not (p_sku or p_name) or not (c_sku or c_name):
                continue
            menu = menus.get(p_sku) or menus_by_name.get(norm(p_name))
            sf = None if menu else (sfs.get(p_sku) or sfs_by_name.get(norm(p_name)))
            if not (menu or sf):
                rep.skipped += 1
                rep.warnings.append(f"Parent not found: {p_sku or p_name}")
                continue
            comp_sf = sfs.get(c_sku) or sfs_by_name.get(norm(c_name))
            comp_item = None if comp_sf else (items.get(c_sku) or items_by_name.get(norm(c_name)))
            if not (comp_sf or comp_item):
                rep.skipped += 1
                rep.warnings.append(f"Component not found: {c_sku or c_name} (in {p_name})")
                continue
            key = (menu or sf).sku
            if key not in recipes:
                batch = to_decimal(cell(r, cols, "Parent Batch Yield Qty"), default=Decimal("1")) or Decimal("1")
                recipes[key] = Recipe.objects.create(brand=brand, menu_item=menu, semi_finished=sf, batch_yield=batch)
            RecipeLine.objects.create(
                recipe=recipes[key], item=comp_item, semi_finished=comp_sf,
                qty_per_batch=to_decimal(cell(r, cols, "Component Qty (per batch)")),
            )
            rep.created += 1

    rows = tab("15 Substitutions")
    if rows:
        rep = reports["substitutions"]
        _, cols = find_header(rows, ["Original SKU", "Substitute SKU"])
        for r in rows[1:]:
            o, s = clean(cell(r, cols, "Original SKU")), clean(cell(r, cols, "Substitute SKU"))
            if not o:
                continue
            if o not in items or s not in items:
                rep.skipped += 1
                rep.warnings.append(f"Substitution {o} -> {s}: SKU not in item master")
                continue
            factor_col = next((k for k in cols if k.startswith("conversion factor")), None)
            factor = to_decimal(r[cols[factor_col]] if factor_col is not None else 1, default=Decimal("1"))
            Substitution.objects.update_or_create(
                brand=brand, original=items[o],
                defaults={"substitute": items[s], "factor": factor, "note": clean(cell(r, cols, "Note"))},
            )
            rep.created += 1
    wb.close()
    return reports
