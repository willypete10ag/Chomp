# Send weight and activity from your iPhone (Apple Health)

Apple Health has no web API, so an iPhone Shortcut reads it and posts to
`https://chomp.<your-domain>/ingest/health`: your latest weigh-in (a scale like VeSync writes
it to Health) and each of the last 7 days' steps, active and resting energy, exercise minutes
and walking + running distance. Resending the week every run means a day you didn't run it,
or the steps after your last run before midnight, are filled in the next time it runs.

**The quick way: Connect iPhone.** Once someone has shared the Shortcut (see "Share the
Shortcut" below), each person signs in (the full walk-through is
[Add another person, part 4](people.md#part-4-them-connect-their-iphone-on-their-iphone)), goes to **You › Integrations › Apple Health** and taps
**Connect iPhone**. Chomp shows their token once with a **Copy** button and a link to the
Shortcut: they add it, paste the token when it asks, run it once to allow Health access, and
set up the automation ("Then make it run by itself" under "Building it by hand"; iPhone doesn't share automations).
**Replace token** there makes a new one for a new phone, and the old one stops working.

**Building it by hand.** The first time, someone builds the Shortcut. Make the token with
**Connect iPhone** as above, or on the server; it is shown once, and making it again replaces
the old one:

```bash
docker compose exec chomp chomp user ingest-token Alex
docker compose exec chomp chomp user ingest-token Alex --revoke   # stop accepting it
```

The token can only add that person's health data; it opens nothing else. Keep it in a
password manager, not in chat, and copy and paste it onto the phone (from the password
manager, or with Universal Clipboard from a Mac) rather than typing it: it is long, and one
wrong character gets `{"error": "unauthorized"}`.

Then build the Shortcut on that person's iPhone, in the **Shortcuts** app (built in, free).
Tap **+** at the top right and name it `Send to Chomp`. Add each action below by typing its
name in the search box at the bottom; it lands under the one before. Steps, active energy and
weight are enough to start; the rest are optional.

1. **Find Health Samples**. Tap *Type* and pick *Weight*. Turn on *Sort by* *Start Date*,
   *Order* *Latest First*, and *Limit* on with *Get 1 Health Sample*. If Shortcuts added a
   filter *Start Date is in the last 7 days*, tap *days* and pick *years*, so a weigh-in from
   more than a week ago is still found. Leave out any *Source* filter: a smart scale writes
   weight under its own app's name, not *Health*, so that filter finds nothing.
2. **Format Date**. Shortcuts fills *Date* with the *Health Samples* from step 1; tap that
   bubble and choose *Start Date*. Tap *Date Format* and pick *ISO 8601*, then turn on
   *Include ISO 8601 Time*.
3. **Find Health Samples** again, *Type* *Steps*. Leave (or add with *Add Filter*) the
   filter *Start Date* *is in the last* *7* *days*, and set *Group By* to *Day*.
4. Repeat step 3 with *Active Energy*: another **Find Health Samples**. Placed right under
   the Steps action, Shortcuts feeds it the Steps samples and renames it *Filter Health
   Samples*, which only sifts those and finds nothing: tap the blue *Health Samples* bubble at
   the start of its line and tap *Clear* so it reads *Find All Health Samples* again (or add
   a **Nothing** action just above it). Do the same for each one below. Optional, the same way:
   *Resting Energy*, *Exercise Minutes* and *Walking + Running Distance* (set its *Unit* to
   *mi*). Each optional one also needs its row in step 6.
5. Optional, for Shortcuts built before October 2026: **Format Date**, *Current Date*,
   *Custom* `yyyy-MM-dd`, for a `date` field. A field with its `_dates` row doesn't need it.
6. **Get Contents of URL**. Shortcuts puts the last result where the URL goes: tap that
   bubble, tap *Clear*, and type `https://chomp.<your-domain>/ingest/health`. Tap the arrow
   to show more and set *Method* to *POST*. Under *Headers*, tap *Add new header*: *Key*
   `Authorization`, *Text* `Bearer ` (with one space) and then paste the token. Set *Request
   Body* to *JSON*, and for each row below tap *Add new field*, pick *Text*, type the *Key*
   exactly, then tap the *Text* box, tap *Select Variable* and tap the output of the action
   named here. Every Find Health Samples output is called *Health Samples*, so tap the label
   right under the action you mean (choosing *Value* isn't needed; Chomp reads the samples as
   they are).

   Each activity field also gets a `_dates` field with the days its samples are for: add the
   same *Health Samples* again, then tap the blue bubble you just put in the box. In the
   panel that opens, tap *Health Samples* at the top and pick *Start Date* from the list,
   then set *Date Format* to *ISO 8601* (if the panel shows no *Date Format*, leave it:
   Chomp also reads the phone's "Oct 17, 2026 at 12:00 AM"). Tap *Done*; the bubble now
   reads *Start Date*.

   | Key | Text (Select Variable, then tap) |
   |---|---|
   | `steps` | the *Health Samples* of step 3 (Steps) |
   | `steps_dates` | the same, as its *Start Date* |
   | `active_kcal` | the *Health Samples* of Active Energy |
   | `active_kcal_dates` | the same, as its *Start Date* |
   | `weight_lb` | the *Health Samples* of step 1 (Weight) |
   | `weight_date` | the *Formatted Date* of step 2 |
   | or `weight_lb_dates` | with step 1 set up like step 3 (last 7 days, no *Limit*): the same *Health Samples* as its *Start Date*, and `weight_date` isn't needed. Days without a weigh-in (0 with *Fill Missing*) are skipped, and grouped by day each day's weigh-in is stored at midnight |
   | `resting_kcal`, `resting_kcal_dates` | optional: Resting Energy |
   | `exercise_min`, `exercise_min_dates` | optional: Exercise Minutes |
   | `distance_mi`, `distance_mi_dates` | optional: Walking + Running Distance |

7. **Show Content** (called *Show Result* on older iPhones) with *Contents of URL*, to see
   Chomp's answer while setting up. You can delete it once it works.

Tap ▶ to run it. The first time, iPhone asks to let the Shortcut read Health data and to send
it to your Chomp address: allow both (*Always Allow* for the second). Chomp answers with how
many readings it added, updated or already had, names any field it couldn't read, and shows
what each field held under `received`; the
dashboard's **Progress › Activity** and **Weight** show them, with when the phone last sent. A
field that finds nothing in Health is skipped quietly, and one pointing at the wrong action
shows that action's number (active calories equal to your steps): check its *Health Samples*
label.

Then make it run by itself: **Automation** tab > **+** > **App** > choose an app you open a
few times a day (Messages, Hevy) > *Is Opened* > *Run Immediately* > pick `Send to Chomp`.
iPhone keeps Health data locked while the phone is locked, so a time-of-day automation (11:59
PM, say) can fail at night; one that runs when you open an app always finds it unlocked, and
there's no need to catch the end of the day. Each run sends the last 7 days: today's total so
far replaces what it sent before, and a day that's over keeps its largest total, so
yesterday's last steps arrive the first time you open that app in the morning. Running it
often is harmless; skipping a few days is too, as long as it runs within the week.

**Updating a Shortcut that sends only today** (built before October 2026, including a copy
from a shared link). In Shortcuts, tap the Shortcut itself to open the editor, then:

1. In each activity **Find Health Samples** (Steps, Active Energy, and any optional ones),
   tap *is today* and change it to *is in the last*, then make it *7* *days*. Keep *Group
   By* *Day*.
2. In **Get Contents of URL**, under *Request Body*, add a `_dates` field for each activity
   field as step 6 describes: `steps_dates`, `active_kcal_dates`, and so on for the optional
   ones you have. Each is the same *Health Samples* as its field, switched to *Start Date*
   with *Date Format* *ISO 8601*.
3. The `date` field and its **Format Date** can stay; a field with its `_dates` row ignores
   it.
4. Tap ▶. Chomp's answer lists the days it `added` or `updated`, and under `received` the
   samples and dates as they arrived, one line per day. A line like
   `"steps: 8 values but 7 dates"` under `problems` means a `_dates` field points at a
   different action than its field.

If you shared the Shortcut, share it again after this (the same steps as before, and paste
the new link on the Integrations page), so the next person gets the new version.

**Share the Shortcut** (once, so nobody else builds it by hand): Shortcuts can leave your
token out of a shared copy and ask each person for theirs when they add it. The tap-by-tap
steps are in [Add another person, part 1](people.md#part-1-you-share-the-shortcut-once-on-your-iphone).

**Instead, with Health Auto Export** (an App Store app; its automations are a paid upgrade):
add a REST API automation with the URL above, JSON format, a header `Authorization` =
`Bearer <token>`, and the metrics you want. For activity, turn on *Summarize Data*, set *Time
Grouping* to *Day* and leave *Date Range* on *Default* (not *Since Last Sync*, which would
send part of a day as the whole day). Keep weight in its own automation without summarizing,
so weigh-ins keep their times.

**Your history.** The Shortcut sends the last week; for everything before, Health exports it all.
In the Health app: tap your picture > *Export All Health Data* > *Save to Files*. Then on the
dashboard, go to **You › Integrations › Apple Health** and pick `export.zip`. Past
weigh-ins and each day's steps, energy, exercise and distance come in; days the
phone already sent stay as it sent them. Health lists the iPhone's and the Watch's samples
separately, so each imported day keeps its larger source's total (Health's own figure for a
day can be a little higher). An export over 95 MB is too big for the browser (Cloudflare caps
a request at 100 MB); copy it to the server and import it there:

```bash
docker compose exec -T chomp chomp import-health - --user Alex < export.zip
```

Sending the same readings again is harmless: Chomp keeps one weigh-in per time and one total
per day for each activity metric. Don't put Cloudflare Access in front of `/ingest` (the
phone can't sign in to it). Ask Claude "what do I weigh?" or "how many steps today?" to check.
