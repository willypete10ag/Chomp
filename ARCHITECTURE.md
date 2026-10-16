# Chomp — Architecture

## Overview
```
Me (Claude or ChatGPT app: voice / text / photos)
        │  conversation
        ▼
Claude / ChatGPT ──(custom connector, MCP over HTTPS)──▶ Cloudflare Tunnel ──▶ Chomp MCP server (Docker)
                                                                    ├── SQLite (chomp.db)
                                                                    ├── USDA FoodData Central API
                                                                    └── Open Food Facts API
Me (phone / browser) ──(HTTPS, /app)──▶ Cloudflare Tunnel ──▶ same server: web dashboard
```
The assistant (Claude, or ChatGPT through the same connector) handles conversation, speech, and vision (reading barcodes and nutrition labels from photos). Chomp handles lookup, math, storage, and totals. No LLM runs on the homelab.

## Core flows

### Log from speech
1. "4 scrambled eggs and 300 g egg whites"
2. Claude calls `search_food` per item → picks best match (prefers foods the user has used before).
3. Claude calls `log_entries` with food_ids + grams → server returns per-item and meal totals.
4. Claude shows the breakdown. I correct ("eggs were 220 g") → `update_entry`.

### Log from barcode
1. I send a barcode photo (or read the number). Claude extracts the UPC.
2. `lookup_barcode(barcode)` → cache → FDC Branded (gtinUpc) → Open Food Facts.
3. Miss → Claude asks for a label photo, reads the panel, calls `save_food_from_label`. Cached forever after.

### Food with no label
Restaurant dish: Claude looks up the restaurant's published nutrition (its own site or PDF
first) or uses numbers I give, then `save_food`, never a generic USDA stand-in. Home
cooking: Claude asks for my numbers. Either way it needs the serving weight in grams (asked
for when not published). It is saved unconfirmed unless I approve the numbers;
`confirm_food` approves it later.

### Review
`get_day(date)` and `get_totals(start, end)` for daily/weekly summaries and protein-target checks.

### Dashboard
`/app` (`chomp/web.py`) is a server-rendered web dashboard on the same app as `/mcp`. Its shell
(#105): four tabs, Today, Progress (`/app/progress` and `/app/progress/{nutrition,weight,activity,training}`, pills
between them and one range control, Today/7D/30D/6M/1Y/All via `?days=` (1 for Today, 182 for 6M, 0 for all time; Nutrition leaves out its charts on Today), the
bar charts grouped by week past 60 days and by month past 400, `logic.chart_unit`), Foods and
You (`/app/you`: goals, integrations at `/app/you/integrations` with the Apple Health and Hevy
imports and the Claude and ChatGPT connector setup, a System/Dark/Light theme kept per device in localStorage
and applied before paint by `static/theme.js`, and Sign out), plus a raised Log (+) button in
the middle that opens `/app/actions` as a bottom sheet (`?part=1` is the sheet's fragment; it
is a page without the script). Past 64rem the tabs become a left sidebar with Progress's
areas and Foods' sides nested under them (`subnav`), and pages go two-column: `render` splits a
body at the `MAIN_COL`/`SIDE_COL` markers (`columns`) into a main column and a sticky side one
(Today's summary beside its meals, a Progress chart beside its list of days or weigh-ins), and
the editors, a food's page, a workout and Overview use wider grids. Keyboard shortcuts
(`SHORTCUT_KEYS`, listed on `/app/shortcuts`, which ? opens): / search or log food, n Log,
←/→ the day before or after, g then t/p/f/y to a tab. On Progress the range, metric and area
links swap the page's `main` in place inside a view transition (pushState; Back steps through
them), and pages cross-fade with cross-document view transitions; both are off under reduced
motion, as are the count-up of the big numbers, the ring's shine at the goal and loading
shimmer. Sheets keep Tab inside them, a skip link leads to `main`, tapping the open tab
scrolls to the top, and the phone's tab bar steps aside while the keyboard is up. Pushed pages
share one header (`back_head`: a round back button, actions and the title). The old
`/app/history`, `/app/activity`, `/app/workouts` and GET `/app/weight` 301 to their Progress
pages.
Charts (#105, `chomp/charts.py`) are server-drawn SVG with no chart library: bars (a missing day
is a gap, today hatched as "so far"; a dashed goal, a dotted average, a 7-day rolling average
line, and the previous period as outlined ghost bars behind a Compare chip), lines (the weight
trend over its readings, energy eaten against burned), sparklines and a training heat strip.
Each sits in a figure with an inspector line above it and a "Show as table" fold, and carries
its points as JSON in `data-points`; `app.js` scrubs them (press and drag, arrow keys), pins a
point on release with a link to its day, and keeps the scroll position across range changes.
Trends come from `logic.py`: `weight_trend` (an exponential average over about 10 days that
steps further after a gap), `weekly_rate`, `previous_period` and `change_percent` for "vs prev
30D" delta chips (green or amber only where up is plainly good; weight and volume stay
neutral), `rolling_average`, `bucket_days(how="sum")` for weekly volume, and `exercise_bests`
(best set per exercise by Epley estimated one-rep max, up to 12 reps; a record has to beat, not
tie). Optional body goals (migration 0017, `store.get_body_goals`, set at `/app/you/body-goals`)
draw the goal weight on Weight and the step goal on Activity; they aren't macro targets, so the
tools don't report them. Icons are Lucide (ISC, `static/LICENSE-lucide.txt`) inlined from `static/icons.svg`.
Screens: Today
(any day via arrows, a seven-day strip, or the month view at `/app/calendar` that tapping the
day's title opens as a sheet, with a dot on logged days and a ring around days within 10% of
every goal, months back to the first logged one and a year list; an Energy card (#105) with a ring
and calories left as the big number, amber up to 100 kcal over the goal and red past that,
eaten, burned so far, net and the goal, and a … sheet (`/app/day-menu`: Breakdown, Edit goals,
Pick a day); then protein/carbs/fat with bars, and More nutrients folding away fiber/sugar/
sodium and vitamins; goals not set count as 2,000 kcal split in even thirds, for the
connector's progress too; Your day tiles for steps (against the step goal or the 30-day
average), exercise, weight (the latest up to that day and the trend's change over the week,
`logic.week_change`) and training (that day's workout or when the last one was), each opening
its Progress screen or, with no data, where it comes from; meals led by their time, with a …
that opens the meal sheet (`/app/meals/{id}`: add food to it, rename or change its time,
combine, save it as a saved meal via POST `/app/meals/{id}/save`, delete); tapping an entry
opens its edit sheet (`/app/entries/{id}`: amount by slider, servings or grams with a live
preview, move to another meal, open the food, delete with Undo); swiping an entry right shows Delete (no asking, Undo brings it back) and holding it opens the sheet. Today reloads itself when it
comes back to the foreground after 5 minutes away, unless a sheet is open or a field has focus;
tapping the calories or a macro opens that day's Breakdown at `/app/breakdown`: the
protein/carbs/fat calorie split, calories by meal, the top 3 foods per macro, and the full
nutrition facts, where tapping a nutrient lists the foods that gave the most; a food with no
value for a nutrient counts as none of it, and a nutrient no food that day reports is left
out), the Progress overview (`/app/progress`, 30D by default: energy balance on full days with
both food and burned energy, the weight trend and its weekly rate, steps a day against the
period before, workouts a week with a heat strip, and calories and protein against the goals;
each card opens its screen), Nutrition (tiles for calories and protein a day against the goals
and the period before, days on target and days logged, averaged over full days; calories and
protein charts with goal lines, the rolling average and Compare; energy balance when activity
is here; each day with a slim macro bar links to that day and its Breakdown),
Weight (#94, `/app/progress/weight`: the trend weight in lb as the hero with the latest reading,
the change over the range and a week, the distance to the goal weight; each day's reading as a
dot under the trend line; lowest, highest, average and the average against the period before;
the weigh-ins by month with where they came from, swiped left to delete after asking, tapped (or
held) to open the weigh-in sheet (`/app/weight/{id}`: change its weight, time or date, or
delete it); the + in the header and the Log sheet open Add a weigh-in (`/app/weight/new`,
prefilled with the last weight), which goes back where it came from (`back`)),
Training (#94, `/app/progress/training`: workouts, volume, average length and sessions a week
against the period before, volume by week (by month past 400 days) with ghosts, a heat strip of
at least 13 weeks, personal bests newest first with a PR mark on records set in the range, and
the range's workouts grouped by week with their count and volume; tap opens its sets per exercise, numbered, with warm-up, failure and drop sets marked W,
F and D, and RPE; swipe left deletes after asking),
Activity (#94, `/app/progress/activity`, which opens on Today (`?days=1`): today's steps (0 until the phone sends some), active and resting energy,
exercise minutes and miles (flights are stored but not shown), calories burned so far when resting energy is known, and when the phone
last sent, and the share of the step goal; on a longer range a switcher picks Steps, Energy (active kcal), Exercise or Distance:
that metric's daily average over the full days against the period before, its best day and
days with data, its bars with the average, the step goal and Compare, then every metric's
averages; and each day, newest first (the latest 100), whose row opens all its numbers in a
sheet (`/app/progress/activity/day?date=`, `?part=1` for the sheet); the Hevy and Health import forms turn into a spinner with a seconds
count while they upload)
and Foods (one search over saved
meals and used foods, most logged first and per serving unless picked otherwise. A sort
button over the list opens the sort sheet (`/app/foods/sort`, #105): sort by (most logged,
recent, newest, unconfirmed first, calories, protein, fat, carbs; `store.FOOD_SORTS`), which
way, and per serving or per 100 g, remembered on the device in a cookie (#91). Each row has a
… that opens the food's menu sheet (`/app/foods/{id}/menu`: Log it, Edit, Rename, its photo,
Confirm or Mark unconfirmed, Delete). A tap opens a food's page (#72, #105): its photo edge to
edge (or a tile to add one; `/app/foods/{id}#photo` lands there, the link the connector gives
Claude), name, brand and Confirmed or Unconfirmed, a per serving / per 100 g switch (radios
the stylesheet reads, so no reload) over calorie and macro tiles and label-style nutrition
facts, vitamins and minerals folded away, your portions of it as a list (#75: swipe one left
to delete it, press and hold it to change its name or grams, or Add a portion), how often
you've logged it, and Log it pinned at the bottom (`/app/foods/{id}/log`, a sheet with the
amount, preview and meal); back and the same … menu float over the photo. Edit
(`/foods/{id}/edit`) changes all values, with Undo. Press and hold a food in the list
for a menu over the blurred page (Edit, Rename, its photo, Confirm on amber foods, Log it,
Delete); swipe it left to Delete, or an amber one right to Confirm). An entry whose
grams were estimated shows a muted "~" before them until new grams are saved, and Log food
offers your portions as chips ("1 handful · 30 g") next to the gram chips. For a food with a serving
size, Log food and entry edits also have a Servings / Grams toggle (#80): Servings is a − / +
stepper in whole servings (a typed 1.5 still counts) and the server logs servings × the serving size; it is picked when the
amount is a whole or half number of servings, and a food without a serving size shows grams only.

Sheets (#105): a link with `data-sheet` opens its page's `?part=1` fragment in a bottom sheet
(`app.js` keeps a stack with back and close, Escape, a tap outside or a drag down closes it, and
a POST from it submits as a page); `data-sheet-swap` replaces the sheet's content, and
`data-sheet-full` makes it tall. Without the script every sheet is a page of its own with a back
link (`sheet_or_page`).

It also works without Claude (#54): Log food (`/app/add`, a tall sheet: a chip picks a new
meal at a time or one of the day's meals, a live search as you type (prefix matching, USDA only
on submit), and Recent / Frequent / Saved meals / Recipes pills; each food row has a + that logs
it again at the amount it was last logged (`store.last_amounts`), or opens to pick an amount;
search covers your foods, portions and saved meals, or
USDA when `FDC_API_KEY` is set, on its own when fewer than 3 of your foods match (#88)), change an
entry's grams, move it to another meal, edit a meal's name, time, date and note, combine or
delete meals, and set daily goals. Every edit is a form post through `store.py` like the
tools, so macros still come only from the database. Each edit records the rows it touched
before and after (`undo_actions`, migration 0005); the next page shows a confirmation with
Undo, which applies only while those rows are unchanged (so it never overwrites a newer edit
from Claude). A small same-origin script (`static/app.js`) adds sliders beside the number
boxes, quick picks (a serving, your usual, half/double), live previews of the calories and
the day's total, and the goals page's calorie bar (drag the borders between protein, carbs
and fat; the calorie total stays fixed, `logic.fit_macros` is the same split); it repeats `logic.scale` only for the preview, and every page works without
it. A web app manifest lets phones add it to the home screen.

Recipes (#82) are the Recipes pill on Foods (`/app/recipes`): a list with
calories per serving (tap to open, … for its menu, press and hold to edit, swipe left to
delete), a recipe page on the food page's template (#105: per serving / whole recipe tiles,
ingredients as a list that opens each food, steps, Log it pinned at the bottom, opening the
food log sheet in servings for the recipe's food row, and Edit and Delete in its … menu,
`/app/recipes/{id}/menu`), and an
editor (name, servings it makes, ingredients found by searching your foods with grams for the
whole recipe, steps one per line; #105 builds it, like the saved meal editor, from the shared
`form_field` and `stepper` components with a running per-serving total beside it). The editor goes back to the server to search, add or remove
an ingredient (`/app/recipes/draft`); the script swaps the new form in place. A recipe's food
row stays out of Foods, and opening it as a food opens the recipe.

The Foods tab has Foods / Meals / Recipes pills under its title, and a + in the title bar for
a new one of whichever is shown (#60, #105); the tab always opens on Foods, and each side's
search box searches only that side. Meals (`/app/saved-meals`) lists the
saved meals (`meal_templates`) with their calories and macros (tap to open, … for its menu,
press and hold to edit, swipe left to delete after a confirm, swipe right to log it as a new
meal now), a saved meal's page shows its totals as tiles and its foods as a list, with Log it
pinned at the bottom (`/app/saved-meals/{id}/log`, a sheet posting to `/app/log-template`, a
new meal at a time and date or into one of today's meals) and Edit and Delete in its … menu,
and the + opens the editor: a name, then foods found
by searching your foods, each in servings (when the food has a serving size) or grams. It
works like the recipe editor (`/app/saved-meals/draft`), saves through
`store.edit_template`, which refuses a name another saved meal already has, and an item's
estimated flag (#75) stays only while its grams are unchanged.

New food (#59, the + on Foods) is a chooser of four ways (#105): Scan a barcode, Read a
nutrition label, Search databases and Type it in (`/app/foods/new?how=scan|label|search|type`),
each full screen with Cancel and its title and no tab bar. The search (#88) looks in USDA FoodData
Central (`search_ranked`) and Open Food Facts (`OFFClient.search`, its full-text
`/cgi/search.pl`, cached and never retried since OFF allows about 10 searches a minute), run
side by side and cached into `foods`. Both sources' hits go through `sources/rank.py` (#97,
shared with `search_food` and Log food): every query word first, then the food the name leads
with, then the plain food before processed or prepared ones the query didn't name (breaded,
dried, "with bacon"), then generic before Branded unless the query names the brand. A hit
missing query words others have is dropped, and listings of one product (same barcode, or the
same name, brand and nutrition) are shown once. When FDC refuses the one generic request, each
generic type is asked for on its own. Results are tagged Raw, Cooked or Breaded from their
names. USDA alone is searched as you type (`?part=usda`, after a pause and from three
letters); Open Food Facts waits for Search. Tapping a result marks it saved and opens its page;
a USDA food counts as confirmed once picked, an Open Food Facts one stays unconfirmed. A
result already in Foods says so and just opens it, keeping its photo, corrections and
confirmation, and says where it came from when another source's barcode found it. Below
that it adds a shared food three ways, all through one form of per-serving values: a barcode (scanned with the camera by `static/scan.js` and the vendored
ZXing reader `static/zxing.min.js`, since the CSP allows only same-origin scripts; or typed),
looked up through the same chain as `lookup_barcode` (`chomp/barcode.py`); a nutrition label
photo, read on the server by `tesseract` (installed in the image) and parsed by
`chomp/label.py`, with a tick box per value; or typed in. Saving a barcode food confirms that
cached row (only values the person changed are rewritten); a label food is saved
`source='label'`, confirmed only when every filled-in value was ticked; a typed one is
`source='manual'`, confirmed only when its Verified box is ticked (#68). Any food can be
confirmed by hand, or marked unconfirmed again, from its page, its hold menu, or a swipe right on Foods. A barcode given with a label or typed food moves to it, as
with `save_food_from_label`. An Open Food Facts hit Claude looks up stays unconfirmed
until `confirm_food`.

Food photos (#74, `chomp/photos.py`): one picture per food, shown at the top of its page, as
a small square on Foods, Today and Log food rows (a food without one shows its first letter),
and added, replaced or removed from its page or its hold menu. A food with a barcode gets
Open Food Facts' front-of-package photo in the background after any barcode lookup (tool or
dashboard), and a one-time backfill (started by the first lookup or Foods page after a
deploy, then marked done with `.off-backfill-done`) does the same for barcode foods saved
earlier. Only images on Open Food Facts' own HTTPS hosts are downloaded. Label photos are
never used as a food's picture. Photos are files, `PHOTOS_DIR/<food_id>.jpg` (default
`photos/` next to the database), re-encoded on the way in: upright, at most 800 px, no EXIF.
Removing one leaves `<food_id>.none` so Open Food Facts doesn't put it back. Chat can't hand
Chomp a picture, so `save_food` and `save_food_from_label` return `add_photo_url` (the
food's page) when it has none, and the server instructions tell Claude to send that link.

### Users
Each person has their own log (#46, migration 0006): `meals` (and so entries), `targets`,
`portions`, `meal_templates`, `recipes`, `undo_actions`, `web_sessions` and `oauth_tokens` carry a
`user_id`; `foods` are shared, so anyone's saved or label foods turn up in everyone's search
(use counts are per person). User 1 is the owner, holds everything logged before users
existed, and signs in with `AUTH_PASSWORD`. Others are added by the owner on You › People
(#109) or with `chomp user add NAME`, and have a scrypt-hashed password in `users`. People
adds a name only and shows a one-time invite link (`/app/invite/<token>`, 7 days, the token
SHA-256 hashed in `user_invites`, migration 0020); opening it sets the person's password and
signs them in, so the owner never sees it. Reset password is a new link; the old password
works until it is used. Removing someone deletes their whole log (foods stay; their recipes'
food rows are marked deleted). A password alone identifies the person (no two
users may share one; the server won't start if `AUTH_PASSWORD` matches another user's), so the consent page and dashboard sign-in stay a single password box;
the sign-in's user becomes the OAuth token's subject or the dashboard session's user.
`store.py` reads the user from the connection (`db.connect(path, user_id)`) and treats
another user's meal or entry as not found. The static `BEARER_TOKEN` is the owner's; in
local-only mode everything is the owner's.

## MCP tools
| Tool | Purpose | Key args |
|---|---|---|
| `search_food` | Names any saved meal template the phrase matches (so "my protein shake" points to `log_meal_template`), then fuzzy search (SQLite FTS5): foods with a portion the phrase names → foods the user has used (verified, from a label, barcode-scanned (`foods.saved`, migration 0002), logged, with a portion or templated; verified first) → FDC (Foundation, SR Legacy, Survey (FNDDS) dishes as eaten (#88), and Branded; ranked by `sources/rank.py` (#97): query words matched, then how much of the name before its first comma the query names, then plain before processed, then generic before Branded unless the brand is named, then Foundation, SR Legacy, Survey, Branded; weak matches dropped, listings of one product grouped). FDC hits are cached into `foods` so every result has a `food_id`, but a cached hit only counts as used once logged. Every hit lists the user's `portions` of it | `query`, `limit` |
| `lookup_barcode` | UPC/EAN lookup through the source chain; caches hits; a miss tells Claude to ask for a label photo | `barcode` |
| `save_food_from_label` | Save a food from a label read; converts per-serving to per-100 g | `name`, `brand?`, `upc?`, `serving_g`, per-serving macros (fat subtypes, cholesterol, added sugars, sugar alcohols optional), `micronutrients?` (amounts), `micronutrients_percent_dv?` (label % DV, converted with FDA Daily Values) |
| `save_food` | Save a food with no label or barcode (restaurant dish from its published nutrition, which Claude may look up on the web; home cooking from the user's numbers); `source='manual'`, confirmed only when `confirmed` | `name`, `brand?`, `serving_g`, per-serving macros (fiber, sugar, sodium, sat/trans fat, cholesterol, added sugars optional), `serving_desc?`, `confirmed?` |
| `log_entries` | Log one or more items as a meal, or add them to a logged meal | `items[{food_id, grams or portion + count, estimated?}]`, `eaten_at` (time or `now`; required for a new meal), `meal_label?`, `meal_id?` |
| `update_entry` | Change grams or swap food; re-snapshots macros | `entry_id`, `grams?`, `food_id?`, `estimated?` (new grams count as exact unless set) |
| `update_meal` | Move a meal to a different time, or change its label or note | `meal_id`, `eaten_at?`, `meal_label?`, `note?` |
| `delete_entry` | Remove a mistaken entry; deletes the meal when its last entry goes | `entry_id` |
| `get_day` | All meals/entries + totals for a local date, with progress against targets | `date` |
| `get_totals` | Aggregates, per-day totals and daily averages over a range (inclusive local dates), with progress against targets | `start`, `end?` |
| `confirm_food` | Mark a food verified once the user approves it (an OFF hit, a restaurant or home-cooked food, anything), optionally correcting its macros | `food_id`, `confirmed?` (false to unconfirm), macros?, `micronutrients?`, `micronutrients_percent_dv?`, `per_serving_g?` |
| `set_targets` | Set daily kcal/protein/carbs/fat targets; only given ones change | `kcal?`, `protein_g?`, `carbs_g?`, `fat_g?`, `clear?` |
| `save_portion` / `forget_portion` | Remember (or forget) what one of the user's portions of a food weighs ("my handful of Cheez-Its = 30 g") | `food_id`, `name`, `grams`, `count?`, `estimated?` / `food_id`, `name` |
| `save_meal_template` / `log_meal_template` | One-phrase repeat meals ("log my usual breakfast") | `name`, `items` / `name`, `eaten_at` |
| `save_recipe` / `get_recipe` / `delete_recipe` | The user's homemade recipes (#82): ingredients from foods with grams, steps, servings it makes; returns nutrition per serving and the recipe's `food_id` for `log_entries` | `name`, `servings`, `ingredients`, `steps?` / `name?` (omit to list) / `recipe_id` |
| `get_workouts` | Strength workouts imported from Hevy (#94) over a range (default the last 30 days, at most 60 listed), newest first, each with its exercises and sets (lb and kg, reps, set type, RPE) and volume; `exercise` narrows to workouts with a matching exercise and lists only its sets | `start?`, `end?`, `exercise?` |
| `get_activity` | Daily activity from Apple Health (#94) over a range (default the last 7 days): steps, active and resting energy and their sum, exercise minutes, walking + running distance (mi and km), flights; each metric's daily average over full days; when the phone last sent | `start?`, `end?` |
| `log_weight` / `get_weight` / `delete_weight` | Body weight (#94): record a weigh-in the user mentions; read weigh-ins over a range (default the last 30 days) with each day's last, the change, the average and the latest; delete a wrong one. Values in kg and lb | `weight`, `unit`, `measured_at?` / `start?`, `end?` / `weight_id` |

Tool descriptions matter: they are Claude's only documentation. Each should say when to use it and that macros must come from tools, never estimates.

## Data rules
- Foods store macros **per 100 g**. Entries store grams plus a **snapshot** of all computed nutrients (kcal, protein, carbs, fat, fiber, sugar, sodium, the label details below, and 17 vitamins and minerals), so editing a food never rewrites history.
- Label details (#63, migration 0009): saturated, trans, polyunsaturated and monounsaturated fat, cholesterol, added sugars and sugar alcohols (`logic.DETAILS`), nullable like fiber, sugar and sodium, mapped from FDC and OFF (`polyols`), read by the label OCR and the New food form, and backfilled into cached unconfirmed foods the same way as vitamins. Net carbs (carbs − fiber − sugar alcohols) is computed on the Breakdown, only when sugar alcohols are known.
- Vitamins and minerals (#53, migration 0007): nullable `foods`/`entries` columns named with their unit (`vitamin_d_mcg`, `iron_mg`, ...; list and FDA Daily Values in `logic.MICROS` / `logic.DAILY_VALUES`). NULL means the source didn't report it. Tool results carry the known ones in `micronutrients`; a total names the ones only some items reported in `micronutrients_partial` (the sum is a floor). FDC and OFF amounts are converted from their reported unit (vitamin D IU → mcg; vitamin A IU is skipped). A cached, unconfirmed upstream food that lacks them gains them the next time the same record comes back; logged entries keep their snapshots.
- One `foods` row per upstream record (unique `(source, source_id)`).
- Portions (#75, migration 0010): people log counts ("3 handfuls"), not grams. `portions` holds
  each user's named amount of a food (`name`, `grams` for one, `estimated`). With no portion
  for a count, the agent asks for an exact weight or an estimate; an estimate is the food's
  `serving_g` × its count of servings, so only the amount is ever estimated and nutrients
  still come from the food. Entries logged that way carry `entries.estimated = 1` until new
  grams replace them (update_entry or the dashboard slider). A real weight for a portion
  (`save_portion`) clears its estimate. Portions replaced nicknames (`aliases`): the
  migration turned each nickname with usual grams into a portion ("1 Birria taco" →
  "Birria taco") and dropped the rest.
- Deleting a food (dashboard, Foods > swipe a food left > Delete; migration 0008) is for everyone,
  since foods are shared. Entries still point at it, so the row stays with `deleted = 1`,
  hidden from search and lists, its barcode released; everyone's portions of it go, and it
  leaves every saved meal (an emptied one goes too). If the same USDA or Open Food Facts
  record turns up again it comes back, barcode included.
- Recipes (#82, migration 0012): each user's `recipes` (name, servings it makes, steps one
  per line) and `recipe_ingredients` (food, grams for the whole recipe). Nutrition is the
  ingredients' sum (`logic.recipe_nutrition`); an optional nutrient counts only if every
  ingredient reports it. Each recipe keeps a confirmed food row (brand `Recipe`, source
  `manual`, `serving_g` = raw ingredient weight / servings) so it is searched and logged like
  any food, by servings. The row follows the recipe and its ingredients (re-summed when an
  ingredient is corrected); it can't be edited, renamed or deleted as a food, only through
  the recipe. Deleting a recipe deletes its food row; logged entries keep their snapshots.
- Hevy API sync (#113, migration 0019, `chomp/hevy_sync.py`, `chomp/sources/hevy.py`): each
  person syncs with their own key (Hevy Pro), pasted on You › Integrations and kept in
  `hevy_keys` (#109, migration 0020; checked by a sync before it is kept). `HEVY_API_KEY` in
  `.env` is the key of `HEVY_USER` (default the owner) unless they paste one. A background
  thread syncs everyone with a key every `HEVY_SYNC_MINUTES` (default 15), one after another;
  Sync now (`POST /app/workouts/sync`) and `chomp sync-hevy [--full] [--user NAME]` run on
  demand, one at a time. A new key resets that person's cursor (it may be another account). The first run lists every workout (`GET /v1/workouts`, 10 a page); later runs
  read `GET /v1/workouts/events?since=` (updated and deleted events, the last per workout
  wins). Workouts carry Hevy's id in `workouts.hevy_id`; a workout without one (CSV) that
  started in the same minute is adopted. `hevy_sync` holds the cursor and the last run's
  result; the cursor moves only after a run without errors, to 10 minutes before that run
  started, so events are re-read rather than missed. A workout Hevy sends that can't be saved
  is logged and fails the run, so the cursor stays put and the next run asks for it again. The CSV import below stays as the fallback and leaves synced
  workouts alone.
- Workouts (#94, migration 0014, `chomp/hevy.py`): without the API, workouts
  come from Hevy's CSV export (one row per set; `weight_lbs`/`distance_miles` or the metric
  columns; local times like `13 Oct 2026, 18:56`), uploaded on You › Integrations or with
  `chomp import-hevy FILE [--user NAME]`. Each user's `workouts` (UTC `started_at`, title,
  description) hold `workout_sets` in order (exercise, notes, superset, set index, type, kg,
  reps, metres, seconds, RPE). A workout is identified by its start minute, so re-importing a
  newer export adds new workouts, replaces changed ones and skips the rest. A workout with
  a bad row is left out whole (and counted), so it never replaces a complete saved copy. Volume (`logic.workout_volume_kg`) is weight × reps over working sets.
- Body weight (#94, migration 0013): each user's `weights` (UTC `measured_at`, `kg`, `source`:
  `health` from the phone, `chat` from `log_weight`, `app` reserved for the dashboard). Stored in
  kg; `logic.to_kg` / `logic.weight_out` convert (lb to 0.1, kg to 0.01). One weigh-in per
  person per second: saving another at the same time replaces it, which de-duplicates the
  phone's repeated sends. Deleting one (tool or Weight page) keeps the row with `deleted = 1`,
  so the phone resending it doesn't bring it back; saving the same time from chat or the
  page does. Editing one on the dashboard (#105, migration 0018, `store.update_weight`) marks it
  `edited`, so the phone resending its original doesn't change it back; a new time moves it,
  leaving the old time deleted.
- Activity (#94, migration 0015): each user's `activity` readings (metric, `period`, UTC
  `start_at` of its time bucket, `qty` in the stored unit: `steps`, `active_kcal`,
  `resting_kcal`, `distance_m`, `exercise_min`, `flights`; `logic.to_activity` converts kJ,
  mi, km, hours and so on). `period` is `day` for a whole day's total (the Shortcut's bare
  date, or Health Auto Export readings that all start at local midnight) and `part` for
  hours. A day's total in local time (`logic.activity_by_day`) is its `day` reading or the
  sum of its parts, whichever was sent last (`updated_at`, bumped on every send, changed or
  not), so switching between the two never adds them up. One reading per person, metric,
  period and bucket start: the phone resends today's growing total and it replaces the last
  one. Averages leave out today, which isn't over.
  Burned is active + resting, only when both arrived. The numbers are Apple Health's.
- Health ingest (#94, decided on #101, `chomp/health.py`): Apple Health has no web API, so the
  iPhone posts readings to `POST /ingest/health`, in one of three shapes. Health Auto Export's
  REST automation JSON (`data.metrics[]`, weight as `weight_&_body_mass` in lb or kg, activity
  as `step_count`, `active_energy`, `basal_energy_burned`, `walking_running_distance`,
  `apple_exercise_time`, `flights_climbed`; names compared without punctuation; dates like
  `2026-10-13 07:02:11 -0400`). Simple lists, `{"weight": [{"date", "value", "unit"}],
  "steps": [{"date": "2026-10-13", "value": 8123}]}` (ISO 8601, no offset = `TZ`; a bare
  date is that day's total). Or the flat fields of the iOS Shortcut in README step 9:
  `date` (the day) with `steps`, `active_kcal`, `resting_kcal`, `exercise_min`,
  `distance_mi`/`distance_km`, `flights`, and `weight_lb`/`weight_kg` with `weight_date`;
  empty fields (nothing in Health) are skipped. A flat field may be a number or text such
  as the Health samples themselves, one per line (`"9,898 count\n0 count"`, or `"9.898"`
  where the region writes numbers that way): activity lines are added up (grouping by day
  adds an empty tomorrow), weight takes the first (latest), and a unit in the text wins
  over the field name's. Bad readings are skipped and reported; metrics Chomp doesn't keep
  are counted under `ignored`. The reply gives added, updated and unchanged counts for
  weights and activity, and echoes the flat fields as they arrived under `received`.
- History from Apple Health's own export (`chomp/health_export.py`): Health > your picture >
  Export All Health Data gives `export.zip` (`apple_health_export/export.xml`, one `<Record>`
  per sample, streamed with its inline DTD dropped). Weigh-ins (`HKQuantityTypeIdentifierBodyMass`)
  are kept as they are; steps, active and basal energy, walking + running distance, exercise
  time and flights are summed per local day per `sourceName`, and each day keeps its largest
  source (Health lists iPhone and Watch samples for the same steps separately). Records nested
  in a `Correlation` are skipped (they repeat top-level ones). `store.import_activity_days`
  adds a day only where the phone sent nothing for that metric (its total or hours), and
  refreshes a day an earlier export brought in; imported rows are marked `source = 'export'`
  (migration 0016), so `last_activity_sync` counts only the phone's, and the phone sending
  a day takes it over. Uploaded on You ›
  Integrations (`POST /app/activity/import`, 95 MB at most because Cloudflare caps a request at
  100 MB, parsed in a worker thread one at a time) or `chomp import-health FILE|-`.
- Every food records its `source` (`fdc`, `off`, `label`, `manual`) and `source_id`.
- Confirmed (`foods.verified`): checked by the person. That is a label food saved through
  `save_food_from_label`, a food confirmed with `confirm_food` (any food the person approves),
  a `save_food` food saved with `confirmed`, a food confirmed by hand on the dashboard (Foods:
  its page, hold menu or swipe right), a typed-in food saved with Verified ticked, a barcode food saved on the
  dashboard's New food page, or a label photo food whose every value was ticked there.
  Everything else is unconfirmed (amber on the dashboard). `confirm_food(confirmed=False)` or
  Mark unconfirmed on the dashboard takes an approval back.
- Timestamps in UTC; display in America/New_York. Day boundaries are computed in `logic.py` with `zoneinfo` (DST-correct); the `daily_totals` view is an approximate convenience only.
- Migrations: `SCHEMA.sql` is baseline v1. Later changes are numbered files in `migrations/` (`0002_<name>.sql`, ...), applied in order and tracked with `PRAGMA user_version`.

## Gotchas
- **UPC normalization:** UPC-A (12 digits) vs EAN-13 (leading 0) vs FDC's GTIN padding. Normalize to 14-digit GTIN for storage and try variants on lookup.
- **FDC Branded data** is per serving in `labelNutrients` and per 100 g in `foodNutrients`; prefer per-100 g, but when a core value differs from the label by more than 10% (and past a small rounding floor), use the label values and flag it.
- **FDC generic foods** (Foundation, SR Legacy) have no label serving. Their household measures (`foodMeasures` on search hits, `foodPortions` in details) give one: one of a thing ("1 large", medium before large) is preferred, else the first single measure ("1 cup"); bare weights never. A cached food without a serving gains one when its record comes back, and `serving_fill.py` (at most every 10 minutes, each food once per process) fetches details for the used FDC foods still missing one.
- **FDC carbs "by difference"** can be slightly negative (seen: -0.104 g). Carbs down to -1 are stored as 0; any other negative value drops the record.
- **Open Food Facts** quality varies; mark OFF-sourced foods unverified until I confirm one.
- **Rate limits:** FDC key allows ~1,000 req/hr; cache aggressively.

## Security
- Cloudflare Tunnel (cloudflared in Compose, outbound only) exposes Chomp's hostname; Chomp answers only `/mcp`, `/consent`, the OAuth routes and metadata, `/ingest/health` (phones), and the `/app` dashboard. `/health` answers loopback only (the container health check). Zero Trust Access browser login would block Claude's servers, so no interactive SSO sits in front of `/mcp` or the OAuth routes; Access may cover the `/app` path only (README, step 7).
- Dashboard sign-in: a password page (the person's own password, sharing the consent page's lockout) starts a 30-day session for that user. The cookie (`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/app`) holds a random token stored as a SHA-256 hash in `web_sessions` (migration 0004). Every POST must carry the public origin (CSRF check). Pages send a strict CSP: scripts and styles from Chomp itself only, nothing inline (the ZXing barcode reader is vendored for this); media may come from the camera (`mediastream:`). Label photos (20 MB at most) are read in memory, one at a time, and never stored. Food photos are served only to signed-in people. In local-only mode the dashboard, like `/mcp`, needs no sign-in.
- Auth (`chomp/auth.py`, decided on #24): Chomp is its own minimal OAuth 2.1 server; the password typed on the consent page picks the user (#46). One pre-registered client (ID and secret entered in Claude or ChatGPT's connector dialog; exact callback allowlist includes both Claude domains and the configured ChatGPT connector), PKCE S256, a password-protected consent page (5 wrong tries lock it for 15 min), 1 h access tokens, 90-day rotating refresh tokens. Tokens are stored as SHA-256 hashes in `oauth_tokens` (migration 0003), so restarts keep the connector signed in. Dynamic client registration is off. An optional static `BEARER_TOKEN` is also accepted (Inspector, or the connector's Request headers beta).
- Health ingest tokens (decided on #101): one per person, made with Connect iPhone on You ›
  Integrations (#109) or `chomp user ingest-token NAME` (shown once, replaces the old one;
  `--revoke` removes it). Connect iPhone links the shared Send to Chomp Shortcut, whose iCloud
  link the owner saves there (`app_settings.health_shortcut_url`); its token is an Import
  Question, so each person pastes their own when adding it. Tokens are stored as SHA-256 hashes in
  `ingest_tokens` (migration 0013). Sent as `Authorization: Bearer` or `X-Chomp-Token`, never
  in the URL. An ingest token only adds its person's health readings at `/ingest/health`; it is
  refused on `/mcp` and everywhere else, and connector tokens are refused at `/ingest`. Bodies
  over 10 MB get 413. The ingest route needs a token even in local-only mode. Cloudflare Access
  must not cover it (the phone can't pass Access).
- With `PUBLIC_URL` unset Chomp runs local-only: no auth, and it refuses any non-loopback `HOST`. With it set, missing or short secrets stop startup. The public host is added to the SDK's DNS-rebinding allowlist (it answers 421 otherwise).
- Secrets (`FDC_API_KEY`, `AUTH_PASSWORD`, `OAUTH_CLIENT_SECRET`, `BEARER_TOKEN`, `TUNNEL_TOKEN`) in `.env`, never committed or logged.
- The container runs as uid 10001 with a read-only root filesystem and no capabilities; the database lives in the `chomp-data` volume.

## Backup and export
- `chomp backup --daily` (the Compose `backup` service) copies the DB nightly at `BACKUP_AT` with SQLite's online backup API (same as `sqlite3 .backup`), integrity-checks the copy, and keeps the newest `BACKUP_KEEP` in `BACKUP_HOST_DIR`. `chomp restore FILE --yes` replaces the DB with a backup after saving the current one. Food photos are copied to `BACKUP_HOST_DIR/photos` (new and changed files only; none deleted there), and a restore keeps photos only for foods the restored database has (a food added later could otherwise hand its id, and photo, to a new food), filling gaps from the backup and moving the old folder aside.
- `chomp export --format csv|json [--user NAME]`: CSV is one row per entry; JSON holds foods, meals with entries, portions, templates, recipes, targets, workouts with their sets, weigh-ins and activity readings. Both cover one user (the owner by default) and take inclusive local `--start`/`--end`.

## Repo and progress tracking
Private GitHub repo: `Chomp`. Progress lives in GitHub, not in chat history or a markdown checklist.

- **Milestones:** M0–M4 below were the initial build and are done. New work is plain issues; `Later` items carry the `later` label and no milestone.
- **Issues:** one per bullet below. Closed by commits/PRs with `Closes #N`.
- **Labels:** `tool`, `source`, `infra`, `security`, `bug`, `decision`, `later`.
- **Project board (optional):** a GitHub Project with Todo / In progress / Done, filtered to this repo.
- **Branches and PRs:** one branch per issue, and every change lands through a pull request to `main` (never stacked on another PR's branch). CI must pass before merging.
- **Commits:** `feat:`, `fix:`, `docs:`, `chore:` prefixes, with the issue number in the message.
- **Tooling:** `uv` for env and lockfile, `ruff` for lint and format; CI runs `ruff check`, `ruff format --check`, `pytest -q`, and builds the Docker image to check it refuses to start without auth and passes its health check with it.
- **Design docs:** this file, `SCHEMA.sql`, and `CLAUDE.md` live in the repo root. A change to a locked decision gets a `decision` issue first.
- **Never committed:** `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `.venv/`, `__pycache__/`. Commit `.env.example` only.

### Bootstrap (how the repo was set up)
```bash
git init -b main
gh repo create Chomp --private --source=. --remote=origin

for l in tool source infra security bug decision later; do gh label create "$l" 2>/dev/null; done

for m in "M0: Repo setup" "M1: Core logging" "M2: Barcode chain" "M3: Deploy and connect" "M4: Quality of life"; do
  gh api repos/:owner/:repo/milestones -f title="$m" >/dev/null
done
```
Then create one issue per bullet below with `gh issue create --milestone "<name>" --label <label> --title "<bullet>"`.

## Milestones
The initial build plan, all done. Later features (dashboard, users, portions, recipes, photos,
saved meals) came from their own issues.

**M0: Repo setup**
- Create the private repo, `.gitignore`, `.env.example`, README stub
- Get a USDA FoodData Central API key (api.data.gov) and store it in `.env`
- GitHub Actions workflow running `pytest -q` on push

**M1: Core logging**
- SQLite schema + migration runner (applies `SCHEMA.sql`)
- FastMCP server skeleton with streamable HTTP transport
- `logic.py` macro math with unit tests (per-serving to per-100 g, gram scaling, rounding)
- `search_food` against FDC
- `log_entries`, `update_entry`, `delete_entry`, `get_day`
- Verify all tools locally with MCP Inspector

**M2: Barcode chain**
- GTIN normalization (UPC-A, EAN-13, 14-digit) with tests
- `lookup_barcode`: cache, then FDC Branded, then Open Food Facts
- `save_food_from_label` with per-serving to per-100 g conversion
- Cache every hit into `foods`; mark OFF-sourced foods unverified

**M3: Deploy and connect**
- Dockerfile + Docker Compose
- Cloudflare Tunnel exposing only the MCP endpoint
- Auth on the endpoint (OAuth or long bearer token)
- Add as a custom connector in Claude and run a real day of logging
- One week of real-world use; file bugs as issues

**M4: Quality of life**
- `save_meal_template`, `log_meal_template` (`save_alias` was replaced by portions, #75)
- `get_totals`
- Nightly SQLite backup

**Later (label `later`, no milestone)**
- Daily summary push (Discord webhook)
