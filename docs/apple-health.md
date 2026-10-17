# Send weight and activity from your iPhone (Apple Health)

Apple Health has no web API, so an iPhone Shortcut reads it and posts to
`https://chomp.<your-domain>/ingest/health`: your latest weigh-in (a scale like VeSync writes
it to Health) and today's steps, active and resting energy, exercise minutes and walking +
running distance.

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
   more than a week ago is still found.
2. **Format Date**. Shortcuts fills *Date* with the *Health Samples* from step 1; tap that
   bubble and choose *Start Date*. Tap *Date Format* and pick *ISO 8601*, then turn on
   *Include ISO 8601 Time*.
3. **Find Health Samples** again, *Type* *Steps*. Tap *Add Filter*, set it to *Start Date*
   *is today*, and set *Group By* to *Day*.
4. Repeat step 3 with *Active Energy*: another **Find Health Samples**. Placed right under
   the Steps action, Shortcuts feeds it the Steps samples and renames it *Filter Health
   Samples*, which only sifts those and finds nothing: tap the blue *Health Samples* bubble at
   the start of its line and tap *Clear* so it reads *Find All Health Samples* again (or add
   a **Nothing** action just above it). Do the same for each one below. Optional, the same way:
   *Resting Energy*, *Exercise Minutes* and *Walking + Running Distance* (set its *Unit* to
   *mi*). Each optional one also needs its row in step 6.
5. **Format Date**. Leave *Date* as *Current Date*, set *Date Format* to *Custom* and type
   `yyyy-MM-dd` in *Format String*.
6. **Get Contents of URL**. Shortcuts puts the last result where the URL goes: tap that
   bubble, tap *Clear*, and type `https://chomp.<your-domain>/ingest/health`. Tap the arrow
   to show more and set *Method* to *POST*. Under *Headers*, tap *Add new header*: *Key*
   `Authorization`, *Text* `Bearer ` (with one space) and then paste the token. Set *Request
   Body* to *JSON*, and for each row below tap *Add new field*, pick *Text*, type the *Key*
   exactly, then tap the *Text* box, tap *Select Variable* and tap the output of the action
   named here. Every Find Health Samples output is called *Health Samples*, so tap the label
   right under the action you mean (choosing *Value* isn't needed; Chomp reads the samples as
   they are):

   | Key | Text (Select Variable, then tap) |
   |---|---|
   | `date` | the *Formatted Date* of step 5 |
   | `steps` | the *Health Samples* of step 3 (Steps) |
   | `active_kcal` | the *Health Samples* of Active Energy |
   | `weight_lb` | the *Health Samples* of step 1 (Weight) |
   | `weight_date` | the *Formatted Date* of step 2 |
   | `resting_kcal` | optional: Resting Energy |
   | `exercise_min` | optional: Exercise Minutes |
   | `distance_mi` | optional: Walking + Running Distance |

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
iPhone keeps Health data locked while the phone is locked, so a time-of-day automation can
fail at night; one that runs when you open an app always finds it unlocked. Each run sends
today's totals so far and replaces what it sent before, so running it often is harmless.

**Share the Shortcut** (once, so nobody else builds it by hand): Shortcuts can leave your
token out of a shared copy and ask each person for theirs when they add it. The tap-by-tap
steps are in [Add another person, part 1](people.md#part-1-you-share-the-shortcut-once-on-your-iphone).

**Instead, with Health Auto Export** (an App Store app; its automations are a paid upgrade):
add a REST API automation with the URL above, JSON format, a header `Authorization` =
`Bearer <token>`, and the metrics you want. For activity, turn on *Summarize Data*, set *Time
Grouping* to *Day* and leave *Date Range* on *Default* (not *Since Last Sync*, which would
send part of a day as the whole day). Keep weight in its own automation without summarizing,
so weigh-ins keep their times.

**Your history.** The Shortcut sends today; for everything before, Health exports it all.
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
