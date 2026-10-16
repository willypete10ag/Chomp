# Sync workouts from Hevy

With Hevy Pro, Chomp pulls your workouts from Hevy's API by itself, edits and deletes included.
Each person makes their own key at
[hevy.com/settings?developer](https://hevy.com/settings?developer), then pastes it on
**You › Integrations › Hevy** and taps **Save and sync**. Chomp checks it with Hevy (a key
Hevy refuses isn't kept), and the first sync starts right away. **Remove your Hevy key** stops
syncing; workouts already in Chomp stay.

The owner's key can instead live in `.env` on the server (restart after changing it). A key
pasted on Integrations wins over it:

```bash
HEVY_API_KEY=your-key       # never commit it
# HEVY_USER=Alex            # whose log they go in; blank = the owner
# HEVY_SYNC_MINUTES=15      # how often Chomp asks Hevy for changes
```

The first sync (shortly after the server starts) pulls every workout and takes over ones you
imported from CSV before, matched by start time, so nothing is doubled. After that Chomp asks
Hevy for what changed every 15 minutes, for everyone with a key. **You › Integrations › Hevy**
shows when it last checked and whether that worked, with a **Sync now** button. On the server,
`docker compose exec chomp chomp sync-hevy` runs a sync for everyone (`--user Sam` for one
person, `--full` pulls everything again).
Workouts show under **Progress › Training**.

Without Pro, import Hevy's CSV instead. In Hevy: Profile › Settings › Export & Import Data ›
Export Workouts. Then on **You › Integrations › Hevy**, pick the CSV. Import again whenever
you like; only new or changed workouts are added (workouts the API sync brought in are left
as they are). A big first import can also be done on the server:

```bash
docker compose exec -T chomp chomp import-hevy - --user Alex < workout_data.csv
```

Ask Claude "what did I bench last time?" or "what did I do at the gym this week?".
