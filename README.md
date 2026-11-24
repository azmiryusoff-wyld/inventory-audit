# Inventory Audit

A web app version of the **COGS & Inventory Audit** Google Sheet. It keeps the sheet's
logic and monthly routine, but holds every brand, outlet and month in one database.

Each month, for each outlet:

1. Start an audit (outlet + month).
2. Upload the files you already export: POS item sales report, Xero invoices
   (supplier and inter-company), opening and closing stock count forms.
3. Fix anything the data checks flag (unknown POS names, unknown SKUs, missing recipes...).
4. Read the dashboard, variance and menu engineering reports, or download them as Excel.

## How the numbers are worked out

Same definitions as the sheet. The code is in `audit/engine.py`, which is plain Python
with no Django in it, so it can be read and tested on its own.

| Step | What happens | Sheet tab |
|---|---|---|
| Demand | POS qty sold + production / transfer / wastage qty | 10, 11 |
| Explode | Push demand through recipes, semi-finished items included, any depth | 30, 31 |
| Substitute | ACTIVE one-way substitutions redirect a component and scale it by the factor | 15 |
| Gross up | Gross = net ÷ yield % | 40 |
| Theoretical cost | Gross × cost per base unit (pack price ÷ pack size) | 40, 50 |
| Actual cost | Opening value + purchases − closing value | 61 |
| Variance | Actual − theoretical (positive = leakage) | 60, 61 |
| Menu engineering | Kasavana-Smith per menu category: margin vs category average, mix vs 70% of an equal share | 62 |

Differences from the sheet, on purpose:

* No 5-level limit on recipe nesting. Loops (a recipe that contains itself) are caught and flagged.
* No fixed row limits.
* Extra checks the sheet couldn't show: POS names not on the menu, purchase and stock count
  SKUs not in the item master. Menu items with no price count as free (RM0) and are not flagged.
* POS names are matched case- and spacing-insensitively, and a menu item can have extra
  POS names ("aliases") for renamed items.
* Inter-company purchases have their own upload and count towards actual cost.
* Invoice lines described as a handling charge are skipped (set `PURCHASE_SKIP_DESCRIPTIONS` to change the list).
* Some central item master rows are menu or semi-finished items with a typed price. When such an
  item also has a recipe, the recipe is used and the typed price is ignored, so it is not counted twice.

## Who can do what

| User | Access |
|---|---|
| Group CEO | Superuser: everything |
| Chef | Menus, recipes, semi-finished items, substitutions, audits, sales; read-only items and purchases |
| Procurement | Audits, purchases, stock counts, sales; read-only menus and recipes |

Run `python manage.py setup_roles` once, then in **Master data › Users** create each person,
tick *Staff status*, and add them to the Chef or Procurement group.

## Run it on a laptop

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py setup_roles
python manage.py setup_outlets
python manage.py createsuperuser
python manage.py load_demo        # optional: a made-up café to click around in
python manage.py runserver
```

Open http://127.0.0.1:8000. Master data (items, menus, recipes, users) is under **Master data**.

Tests: `python manage.py test audit`

## Getting the real data in

### Item master (central workbook, shared by all brands)

The central item master stays the source of truth; the app reads from it.

* **From a download:** File › Download › .xlsx on the central workbook, then
  `python manage.py sync_item_master --file item-master.xlsx`
* **Straight from Google Sheets:** create a Google Cloud service account, download its JSON key,
  share the central workbook with the service account's email (Viewer), then set
  `GOOGLE_SERVICE_ACCOUNT_FILE=/path/key.json` and `ITEM_MASTER_SHEET_ID=<the workbook id>` and run
  `python manage.py sync_item_master`. Schedule it (e.g. nightly cron) to keep prices current.

Items no longer in the workbook are marked inactive, never deleted. The workbook's
`15 Substitutions` tab is loaded too and applies to every brand; a substitution added to one
brand under Master data overrides it for that brand. Duplicate SKUs keep the first row and are listed.

### Each brand's menu, recipes and semi-finished items (one-off)

Download the brand's current workbook as .xlsx and run:

```bash
python manage.py import_workbook EOC "EOC MASTER - COGS & Inventory Audit.xlsx" --name "Eight Ounce Coffee"
```

It reads `21 SF Master`, `23 Menu Master`, `22 Recipe Master` and `15 Substitutions`, and lists
any recipe lines whose parent or component it couldn't match. Then add the brand's outlets under
**Master data › Outlets**.

### Brands and outlets

`outlets.csv` lists every brand and outlet. Run `python manage.py setup_outlets` to create them
(safe to re-run). To add a brand or outlet later, add a line to the file and run it again, or add
it under **Master data › Outlets**. The `pos_location` column is the outlet's name in the POS
report headings; fill in the blanks when you have them.

### Outlets and POS reports

Brands share the item master; outlets of a brand share its menu and recipes. Each outlet has its
own audits, so its own POS, purchase and stock count uploads.

The POS upload accepts the single-outlet *Item Sales Report Summary* and the multi-outlet
*Item Sales (Count_Gross)* report. For the multi-outlet one, set each outlet's **POS location**
(e.g. `EOC_KLCC-O2`, exactly as in the report's column headings) and the app picks that outlet's
column. POS names match menu names ignoring case and spacing, and "Latte" also matches a menu item
called "Latte - Regular Price". Anything else can be linked with a POS alias on the menu item.

Use the report that matches how the brand's menu is set up. EOC's menu is per variant
("Americano - Cold", "Americano - Hot"), so it needs each outlet's *Item Sales Report Summary*,
which has the Full Name; the multi-outlet report only says "Americano". Chonky's menu has no
variants, so its multi-outlet report works.

Adding a brand or outlet later needs no code change: add it under Master data and import the
brand's recipes once.

## Deploying

Any host that runs Python works (Railway, Render, Fly.io, a company VM). Set:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `DJANGO_SECRET_KEY` | A long random string |
| `DJANGO_DEBUG` | `0` |
| `DJANGO_ALLOWED_HOSTS` | Your domain, e.g. `audit.example.com` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://audit.example.com` |

Start command: `python manage.py migrate && python manage.py collectstatic --noinput && gunicorn config.wsgi`.

## Code map

```
audit/engine.py        the calculations (pure Python)
audit/services.py      database -> engine, menu costing, upload checks
audit/importers.py     readers for POS, Xero, stock count, item master and workbook files
audit/models.py        tables
audit/admin.py         master data screens
audit/views.py         dashboard, reports, uploads, Excel export
audit/management/      sync_item_master, import_workbook, setup_roles, load_demo
audit/tests/           engine, importer and end-to-end tests
```
