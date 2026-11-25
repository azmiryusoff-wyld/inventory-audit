Inventory Audit: getting started guide
Nov 25, 2026 · @Chef Azmir Yusoff
The app in one minute
The Inventory Audit app does the monthly COGS audit that used to live in the Google Sheets workbook. Each month, for each outlet, it compares what the stock should have cost (from the recipes) with what it did cost (from purchases and stock counts), and shows where the difference comes from.
An audit is one outlet for one month. Every month follows the same five steps:
1.	Start the month. The CEO or chef starts the audits, usually all outlets at once on Month close. Opening stock is copied from last month's closing count.
2.	Upload the files. The chef uploads the POS item sales report. Procurement uploads the Xero supplier invoices and the closing stock count.
3.	Fix the data checks. The app lists anything that looks wrong, says who fixes it, and shows what it leaves out of the numbers.
4.	Read the result. The dashboard shows theoretical and actual COGS, the variance, and the items driving it.
5.	Sign off. The CEO signs off, which freezes that month's numbers.
 
Find your role below and read that section first. Everyone should read Logging in and Fixing data checks.
Logging in and finding your way
The CEO creates your account and gives you the app's web address, a username and a password. Open the address in any browser, on a computer or a phone, and log in.
If you type the wrong password 5 times, that username is locked for 15 minutes. Wait, or ask the CEO to reset your password.
The dark bar at the top has the same links on every page:
Link	What it is for
Audits	Start an audit and find any past audit. Filter by brand, outlet, month or sign-off.
Month close	One month, every outlet: which files are in, which are missing, and each status.
Trends	Sales, COGS % and variance by outlet and month. Shows the last 12 months by default.
Menus & recipes	Menu items, recipes, semi-finished items, substitutions, the item master and the change log.
Help	The monthly routine and a glossary of the words the app uses.
Admin	Brands, outlets, users and access. Only the CEO sees this.
Inside an audit, the tabs are Dashboard (files, results, data checks), Variance & inventory, Menu engineering, Consumption, Uploaded data (every line, with Edit buttons) and History (every upload and change). Download Excel saves all of them as one workbook.
Tips that work everywhere:
•	Click a column heading to sort a table. Click it again to reverse the order.
•	Words with a dotted underline show an explanation when you point at them (or tap them on a phone).
•	Every upload row has a Download empty template (.csv) link with the exact columns the app expects.
Who does what
Role	Can do	Sees
Chef	Menus, recipes, semi-finished items, substitutions. Uploads POS sales. Links POS names to menu items. Adds wastage.	Every brand and outlet
Procurement	Uploads purchases and stock counts. Fixes purchase and stock lines. Starts audits. Can view menus and recipes but not change them.	Every brand and outlet
CEO	Everything, plus sign-off and unlock, users and access	Everything
Outlet manager	Reads reports only	Only the brands or outlets the CEO ticks for them
The first month for an outlet
In an outlet's first month in the app there is no earlier audit to copy opening stock from, so the opening stock is uploaded once, from the old workbook. The app says so: you see "First month for this outlet" on the dashboard until opening stock is in.
Before the first month:
•	The brand must be set up. Its menu items, recipes and semi-finished items are imported from the brand's workbook once. If they aren't, the dashboard shows "isn't set up yet" and theoretical COGS stays at zero. Ask the CEO.
•	Each outlet must exist under Admin › Outlets. For a POS report that covers several outlets, its POS location name (for example EOC_KLCC-O2) must be filled in.
Then, for the first month:
1.	Start the audit for the month (see the CEO section, or Audits › Start an audit).
2.	In the old workbook, take last month's closing stock count for that outlet. That is this month's opening stock.
3.	On the audit's Dashboard, in the Opening stock count row, click Download empty template (.csv).
4.	Fill in one row per item: SKU (as in the central item master), Item Name, and either Counted Qty (packs) or Total RM Value. If you give packs and leave the RM value blank, the app values them at the item master's price per pack.
5.	Save the file as .csv (or keep it as .xlsx) and upload it in the Opening stock count row.
6.	Carry on with the normal routine below.
From the second month on, opening stock is copied automatically from the previous month's closing count. If last month's closing count is changed later, press Copy (last month)'s closing count in the Opening row to copy it again. A data check flags any item whose opening differs from last month's closing.
Chef guide
The chef owns what the outlet sold and what it should have used: POS sales, menus, recipes and wastage.
Each month: upload POS sales
1.	Export the Item Sales Report Summary from the POS for the outlet and the month.
2.	Open the audit (Audits, or Month close, then click the outlet).
3.	In the POS item sales report row, choose the file and press Upload. Uploading again replaces the earlier file.
4.	Read the green message: it says how many lines loaded and, when one report covers several outlets, which column it read.
The file can be the POS export as it comes, or the empty template with two columns: Full Name (exactly as on the POS) and Quantity.
Link POS names that aren't on the menu
If the POS shows a name the app doesn't know (a renamed item, a promo variant), the data check POS items not found in the menu lists it. Those units count nowhere until linked. Next to each name, pick the menu item it is and press Link. The app saves it as another POS name for that menu item, so every month and every outlet of the brand matches it from then on. You don't need to upload again. Only the chef and the CEO can link names.
Add wastage, transfers or B2B production
On the audit's Uploaded data tab, under Production, transfers, wastage, press Add line. Pick the type, type the SKU (start typing to choose) and the quantity:
•	a menu item or semi-finished item: in portions
•	an item master item: in its base unit (g, ml, pcs)
These count in theoretical usage, so the variance doesn't blame the outlet for them.
Menus
Menus & recipes › pick the brand › Menu items.
•	New menu item or Edit: SKU, POS name (exactly as on the POS report), price including SST, category.
•	Other POS names: one per line. Use it for older names or variants that count as this item.
•	An item with no recipe shows a red add recipe link. Use the filter Active, no recipe to find them all.
•	Set an item inactive instead of deleting it. Items that have been sold can't be deleted.
Recipes
Menus & recipes › pick the brand › Recipes › New recipe (or click a recipe).
1.	Recipe for: the menu item or semi-finished item it makes. The recipe always belongs to that item's brand.
2.	Batch makes: 1 for a single portion; for a batch, how many units it makes (1000 for a 1 kg batch measured in grams).
3.	Ingredients: per line, an item master SKU or one of this brand's semi-finished SKUs, and the quantity per batch in the ingredient's base unit. Use Add another line for more. Clear a line to remove it.
4.	Press Save recipe. The page then shows the cost per unit and each line's cost at today's prices.
 
The app refuses a zero or negative quantity, a semi-finished item from another brand, and a recipe that uses itself.
To see where an item is used, type its SKU or name in the Recipes search box, or open the item from Item master: it lists every recipe that uses it, directly or through a semi-finished item.
Semi-finished items and substitutions
•	Semi-finished (sauces, syrups, doughs): add the item, then press add recipe next to it.
•	Substitutions swap one item for another in every recipe, for one brand or for all of them, with a factor (units of the new item per unit of the old one). The app refuses chains (A to B, then B to C) and a second swap for the same item. Map straight to the final item instead.
Prices, pack sizes, yields and categories come from the central item master workbook. Change them there; the app picks them up at the next sync.
Procurement guide
Procurement owns what the outlet bought and what is on the shelf: supplier invoices, inter-company invoices and stock counts.
Each month: upload the files
Open the audit (Audits, or Month close, then click the outlet). On the Dashboard, each file has its own row. Choose the file, then press Upload (or Replace if one is already in). Uploading again replaces that file's lines; it never adds to them.
 
Row	File	Columns in the empty template	Needed?
Supplier purchases	Xero invoice export for the month	InvoiceNumber, InvoiceDate, ContactName, InventoryItemCode, Description, Quantity, LineAmount	Yes
Inter-company purchases	Xero invoices from sister companies (for example Pastry Boys)	Same as supplier purchases	Only if there are any
Opening stock count	Last month's closing count	SKU, Item Name, Counted Qty (packs), Total RM Value	Copied automatically after the first month
Closing stock count	The count at the end of this month	SKU, Item Name, Counted Qty (packs), Total RM Value	Yes
A Xero export can be uploaded as it comes. Delivery handling charges are skipped on purpose. Lines with no item code are not loaded, and the message lists them with their RM total.
For stock counts, give either the RM value or the packs counted. If you give packs and leave the RM value blank, the app values them at the item master's price per pack.
What the app checks on upload
•	A file that can't be read, has no lines, or belongs in another row changes nothing. The red message says why.
•	A number or date that can't be read stops the upload and names the row, so you can fix the file.
•	After a good upload, a yellow message lists things to check: unknown SKUs (with a "did you mean" suggestion), possible duplicate invoice lines, invoices dated outside the month, and negative lines such as credit notes.
•	SKUs match the item master ignoring capitals and spaces, so "milk-1l " finds MILK-1L.
Fix one wrong line without re-uploading
On the audit's Uploaded data tab, find the line (use the search box at the top), press Edit, correct it and press Save. To add a missing invoice line or count line, press Add line next to that section's heading. To remove a line, open it and press Delete this line.
 
 
Every change is recorded on the audit's History tab with who, when, and the old and new values. A SKU must exist in the item master; if it doesn't, the form suggests the closest one.
Once the CEO signs off a month, its lines can't be changed until it is unlocked.
Fixing data checks
A month with a failed data check shows the status Review, because its variance can't be trusted yet. The Data checks list sits at the bottom of the audit's Dashboard. Each failed check shows a red Review pill, who fixes it, what it leaves out of the numbers (for example "RM 390.00 not counted in actual COGS"), what to do, and the lines involved. A check clears by itself once the data is fixed; there is nothing to tick.
 
Check	Who fixes it	How to fix it
POS items not found in the menu	Chef	Link the POS name to a menu item right there, or add the item to the menu.
Menu items sold with no recipe	Chef	Add the recipe (Menus & recipes › Recipes). Until then the item costs zero.
Unmapped recipe components	Chef	Fix the ingredient SKU in the recipe.
Recipe lines missing / zero qty	Chef	Enter a quantity above zero on the recipe line.
Recipe loops or nesting too deep	Chef	A recipe uses itself somewhere down the chain. Fix the semi-finished recipes.
Invalid substitution mappings	Chef	Map each item once, straight to its final substitute.
Purchase SKUs not in the item master	Procurement	Edit the line on Uploaded data with the right SKU, or ask HQ to add the SKU to the central item master.
Stock count SKUs not in the item master	Procurement	Same as above, for the count line.
Invoices dated outside the month	Procurement	Delete the line on Uploaded data if it belongs to another month. If it really is this month's, leave it.
Possible duplicate invoice lines	Procurement	Delete the extra copy on Uploaded data, unless they really are separate lines.
Opening stock differs from last month's closing	Procurement	Press Copy (last month)'s closing count on the Opening row, or check which count is right.
Items used but not costed	HQ (central item master)	Enter the price and pack size in the central item master workbook.
Items with invalid yield	HQ (central item master)	Set yield between 0% and 100% in the workbook.
Items with an unknown category	HQ (central item master)	Set one of Food & Beverage, Packaging, LPG, Cleaning & Chemical or Other in the workbook.
When the status at the top of the audit reads Pass or Outside tolerance, the data is complete and clean. Outside tolerance means the data is fine but the F&B variance is bigger than allowed (usually 2%). Open Variance & inventory to see which items drive it.
CEO guide
The CEO starts the month, follows progress across outlets, reads the results and signs each month off. The CEO also manages users and who sees what.
Start the month for every outlet
1.	Open Month close and pick the month (it defaults to last month).
2.	Press Start the (n) missing audits. Each gets a 2% tolerance, and opening stock is copied from last month's closing count where there is one.
3.	To start a single outlet with a different tolerance, use Audits › Start an audit instead.
Follow progress
The Month close grid shows every outlet against every file. A tick and a number means that many lines are in; a dash means the file is still missing. The Status column shows Incomplete (a required file is missing), Review (a data check failed), Outside tolerance or Pass. Click an outlet to open its audit.
 
Read the results
On an audit's Dashboard:
•	Total sales, with SST included, as on the POS.
•	Theoretical F&B COGS: what Food & Beverage should have cost from the recipes, and its % of sales.
•	Actual F&B COGS: opening + purchases − closing, and its % of sales.
•	F&B variance: actual minus theoretical. Green when within tolerance; red when outside.
•	Top 10 leakage: the items used most above their recipes, and each one's share of the total leakage. Below it, the items used less than the recipes say.
 
Variance & inventory lists every item. Sort by Variance, filter by category, and read the variance in the item's own unit (g, ml, pcs). Menu engineering classes each menu item as STAR, PLOWHORSE, PUZZLE, DOG or NO RECIPE, with what to do about each. Trends compares outlets and months; filter by brand or outlet.
Sign off
When the status is Pass (or you accept Outside tolerance), press Sign off this month on the audit. This freezes the month: later changes to recipes, prices or menus don't change its numbers, and nobody can upload or edit lines. Unlock reopens it if something must be corrected; the numbers then follow today's recipes and prices again. Sign-off and unlock are recorded on the History tab.
Users and access
In Admin › Users:
1.	Add user, set a username and password, then tick Staff status.
2.	Add them to one group: Chef, Procurement or Outlet manager.
3.	For an outlet manager, tick their brands (all outlets of that brand) or single outlets under Brands and outlets this user can see. With nothing ticked they see no audits.
4.	To let someone else sign off, give them the permission Can sign off and unlock audits.
To reset a password, open the user and use the password form link. A new brand or outlet is added under Admin › Brands and Admin › Outlets.
Outlet manager guide
Outlet managers can read their outlets' audits but can't upload, edit or sign off. They see only the brands or outlets the CEO ticked for them; everything else is hidden.
•	Audits lists your outlets' months. Click a month to open it.
•	Dashboard: sales, theoretical and actual COGS, the variance and the top leakage items. Start with the leakage list: those items are where stock goes missing.
•	Variance & inventory: search for an item to see its opening, purchases, closing and variance in RM and in its own unit.
•	Menu engineering: which items sell well and earn well, and what to do about the rest.
•	Trends: your outlets month by month.
•	Download Excel saves everything for one audit as a workbook.
If a page says you don't have access to any outlet, ask the CEO to tick your brand or outlet.
Words used and common questions
Word	Meaning
COGS	Cost of goods sold: what the ingredients and packaging used in the month cost.
Theoretical COGS	What it should have cost, from the recipes of everything sold, produced, transferred or wasted.
Actual COGS	What it did cost: opening stock + purchases − closing stock.
Variance	Actual minus theoretical. Positive: more used than the recipes say (waste, over-portioning, theft, a missing count). Negative: less (often a missing purchase or a count error).
Tolerance	How far F&B variance may be from theoretical, either way, before the audit shows Outside tolerance. Usually 2%.
Yield	The usable share after trimming or cooking loss. 80% yield means 1 kg bought gives 800 g usable.
Base unit	The unit recipes use for an item: g, ml or pcs.
Semi-finished item	Something made in-house, such as a sauce, syrup or dough, with its own recipe.
SST	Sales and service tax. Dashboards and menu engineering use POS prices with SST; Menu costing takes SST out.
Sign off	Freezes the month's numbers and stops uploads and edits.
I uploaded the wrong file. Upload the right one in the same row; it replaces the wrong one. If you uploaded it in the wrong row, the app usually notices and changes nothing.
One invoice line is wrong. Fix it on Uploaded data › Edit. You don't need to upload the whole file again.
The variance looks far too high. First clear every data check. Then check that the closing count is complete and that wastage was entered.
A menu item shows NO RECIPE. It sold but has no recipe, so its cost counts as zero. The chef adds the recipe.
Where do item prices come from? From the central item master workbook. Change them there, not in the app.
Who changed this? The audit's History tab lists uploads, line edits, sign-offs and unlocks. Menus & recipes › Change log lists menu, recipe and substitution changes.
<img width="468" height="641" alt="image" src="https://github.com/user-attachments/assets/c31a4518-2596-4cfe-b12f-945d9bacd177" />
