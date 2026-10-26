# Features

**In chat** (custom connector, 20 tools):

- Log meals with the time you ate them, correct grams or swap a food, move or delete a meal.
- Find foods in your own list first, then USDA (including dishes as eaten) and Open Food
  Facts. Barcodes go through your saved foods, USDA Branded, then Open Food Facts; a miss asks
  for a label photo.
- Save foods from a label, a restaurant's published nutrition, or your own numbers. Foods
  from Open Food Facts or the web stay unconfirmed until you approve them. A restaurant dish
  published per order with no weight is saved per serving ("1 appetizer") and logged in
  servings (half an appetizer), never with made-up grams.
- Remember your portions ("my handful of Cheez-Its is 30 g"), saved meals ("log my usual
  breakfast") and homemade recipes (logged by the serving).
- Daily and range totals with averages, progress against calorie and macro goals, fiber,
  sugar, sodium, label details (fat types, cholesterol, added sugars) and 17 vitamins and
  minerals.

**On the dashboard** (`/app`, works on a phone and installs to the home screen):

- **Today:** calories left as the big number, macro bars, meals by time. Arrows, a week strip
  or a month calendar (dots on logged days, a ring around days within 10% of every goal) move
  between days. Tap a number for that day's Breakdown: calories by macro and meal, top foods,
  full nutrition facts.
- **Log and edit:** Add food in servings or grams with your portions as chips. A food whose
  serving is printed as one of something ("1 bottle", set in its edit form) counts in those:
  0.5 bottle, 2 bottles, shown on Today as "1 bottle · 354 g". Change, move
  or delete entries; rename, retime, combine or delete meals. Every change has Undo.
- **Goals:** daily calories split between protein, carbs and fat on one draggable bar.
- **Gym days:** a goal for how many days a week you train (You > Gym days, or tell Claude).
  Any day with a workout counts; Training shows this week against it, how many more you need,
  your streak of weeks that hit it and past weeks, and Today's Training tile shows the count.
- **Progress:** Nutrition, Weight, Activity and Training, each over today, 7 or 30 days, 6 months, a
  year or all time, with averages and charts.
- **Log (+):** the button in the middle of the tab bar logs food, scans a barcode, logs a
  saved meal or adds a weigh-in from any page.
- **You:** goals, Apple Health and Hevy imports, the Claude connector address, a light or
  dark theme (or the phone's), CSV and JSON downloads of your log, and signing out.
- **Foods:** your foods with photos, sorted by most logged, recent, calories and more; each
  food has a page with nutrition facts, portions, Log it, Confirm, Edit and Delete. Search
  filters as you type; a food that isn't there is one tap from a database search or typing it
  in. Add a new food by searching USDA (as you type) and Open Food Facts, with the plain food
  first, scanning its barcode with the camera, reading a label photo (tesseract on the
  server, no AI), or typing it in.
- **Saved meals and Recipes:** build, edit and log them from the Foods tab.
- **On a computer:** a sidebar instead of the tab bar, two columns where they help, and
  keyboard shortcuts (press **?** to see them).

**Running it** ([setup](setup.md)): several people, each with their own log, goals, portions, saved meals and
recipes (the food list is shared); OAuth sign-in for the Claude and ChatGPT connectors;
nightly backups with photos; CSV and JSON export.
