# Add another person

This guide takes someone from nothing to a working Chomp: their own log, their iPhone
sending weight and activity, their past Health data, and their workouts. Follow it top to
bottom. Each part says who does it and on which device.

Each person gets their own meal log, goals, weigh-ins, activity and workouts. The food list
is shared.

**About passwords.** You, the owner, sign in with `AUTH_PASSWORD` from the server's `.env`.
Nobody else uses that. Everyone else picks their own password, and Chomp tells people apart
by password, so two people can't have the same one.

## Part 1. You: share the Shortcut (once, on your iPhone)

The Send to Chomp Shortcut reads Apple Health and sends it to Chomp. Do this once, and
everyone you add later gets the Shortcut from a link instead of building it. If you haven't
built your own Shortcut yet, build it first ("Building it by hand" in [Apple Health](apple-health.md)).

You're going to move your token into a box of its own at the top of the Shortcut, then tell
Shortcuts to ask for that box when someone adds it. Your token stays out of the shared copy.

1. Open the **Shortcuts** app and tap your **Send to Chomp** to open it in the editor. (Tap
   the Shortcut itself, not its name: tapping the name only offers Rename, Duplicate and
   so on.)
2. **Add a Text action.** Tap the search bar at the bottom, type `Text`, and pick the action
   named just **Text** (gray icon, a big empty box). Not "Send Message". Drag it to the very
   top, above every other action.
3. **Put your token in it.** Copy the token from your current header: scroll down to **Get
   Contents of URL**, open **Headers**, and copy everything after `Bearer ` in the
   `Authorization` value. Paste it into the Text box from step 2. Just the token, without
   "Bearer".
4. **Point the header at the box.** Go back to that same `Authorization` header (edit it,
   don't add a new one). Delete everything in its value except `Bearer ` (keep the space)
   and leave the cursor right after it. The bar above the keyboard now shows earlier
   actions' results as blue bubbles: tap the one called **Text**. The header now reads
   `Bearer` followed by a blue **Text** bubble.
5. **Check it still works.** Tap ▶. Chomp's answer should look the same as before.
6. **Make it ask for the token.** At the bottom of the editor, tap the **ⓘ** button (next to
   ▶), open the **Setup** tab, and under **Import Questions** tap **Add Question**. Tap the
   Text action's box and type the question `Paste your Chomp token`. Tap Done.
7. **Copy the link.** Go back to the list of Shortcuts, press and hold **Send to Chomp**, and
   tap **Share › Copy iCloud Link**.
8. **Save the link in Chomp.** On the dashboard, signed in as you: **You › Integrations**.
   In the **Apple Health** box, open **Share your Shortcut**, paste the link and tap **Save
   link**.

Can't find Import Questions in step 6? Then in the Text box, replace your token with
`PASTE YOUR TOKEN HERE`, do steps 7 and 8, and put your own token back afterwards. The
person then pastes their token into that top box themselves after adding the Shortcut.

## Part 2. You: add them (on the dashboard)

1. **You › People**. Type their name and tap **Add person**.
2. Chomp shows an invite link. Tap **Copy** and send it to them (text, email).

The link works once, for 7 days. You never see their password.

If Cloudflare Access covers your dashboard ([setup, step 7](setup.md#optional-cloudflare-access-in-front-of-the-dashboard)),
add their email to the Access policy first, or the link stops at Cloudflare's login page.
Most setups don't use Access; Chomp's own password sign-in protects the dashboard either way.

## Part 3. Them: pick a password (on their iPhone)

1. Open the invite link.
2. Pick a password (12 characters or more), type it twice, and tap **Save and sign in**.
   Save it in their password manager.

They're now on their own dashboard. Add it to the home screen if they like (Share › Add to
Home Screen in Safari).

## Part 4. Them: connect their iPhone (on their iPhone)

Everything here happens on one page of the dashboard and then in Shortcuts.

1. On the dashboard: **You › Integrations**. In the **Apple Health** box, tap **Connect
   iPhone**.
2. Their token appears, with a **Copy** button and a short numbered list under it. Tap
   **Copy**. Stay on this page: the token shows only now.
3. In that numbered list, tap the blue link **the Send to Chomp Shortcut**. It opens
   Shortcuts. Tap **Add Shortcut**.
4. When it asks "Paste your Chomp token", paste and tap Done.
5. In Shortcuts, tap **Send to Chomp** to run it. iPhone asks to let it read Health data:
   tap **Turn On All**, then **Allow**. It then asks to send to your Chomp address: tap
   **Always Allow**.
6. Chomp's answer pops up. Under `received`, `steps` and `active_kcal` should show numbers
   (see [Troubleshooting](#troubleshooting) if they're empty).
7. **Make it run by itself.** In Shortcuts, tap the **Automation** tab › **+** › **App**.
   Choose an app they open a few times a day (Messages works well), tick **Is Opened**,
   choose **Run Immediately**, tap Next and pick **Send to Chomp**. iPhone can't share
   automations, so this step is always done by hand.

Left the page before copying the token? Tap **Replace token** in the same box to get a new
one; the link comes back with it.

Don't change the Shortcut's actions to name their own phone or watch. It reads all of
their Health data as it is.

## Part 5. Them: bring in their past Health data (optional)

The Shortcut sends the last week each time it runs, from now on. For everything before:

1. In the **Health** app, tap their picture (top right) › **Export All Health Data** ›
   **Export**, then **Save to Files**. It takes a minute or two.
2. On the dashboard: **You › Integrations**. In the **Apple Health** box, open **Import your
   history (export.zip)**, tap the file button and pick `export.zip`, then **Import**.

Past weigh-ins and each day's steps, energy, exercise and distance appear under
**Progress**. If the file is over 95 MB the browser can't send it; copy it to the server and
run `docker compose exec -T chomp chomp import-health - --user Sam < export.zip` (with their
name).

## Part 6. Them: workouts from Hevy (optional)

Hevy's API only works with Hevy Pro, and each key only sees its own account. So:

- **With Hevy Pro:** on hevy.com, open
  [hevy.com/settings?developer](https://hevy.com/settings?developer) and make an API key. On
  the dashboard: **You › Integrations**, in the **Hevy** box paste it and tap **Save and
  sync**. Workouts now come in by themselves every 15 minutes.
- **Without Pro:** in the Hevy app, **Profile › Settings › Export & Import Data › Export
  Workouts**. On the dashboard: **You › Integrations**, in the **Hevy** box open **No Hevy
  Pro? Import a CSV export** and pick the file. Repeat whenever they want Chomp up to date.

## Part 7. Them: Claude (optional)

To log meals by chatting, they add Chomp in **their own** Claude account exactly as in
[setup, step 6](setup.md#6-add-the-connector-in-claude): same address, same
`OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` (you give them these), and on the "Connect
Claude to Chomp" page they type **their** password. If they use ChatGPT instead, their
connector's callback URL must be added to `CONNECTOR_CALLBACKS` in `chomp/auth.py` first (see
[Add the ChatGPT connector](setup.md#add-the-chatgpt-connector)).

## Troubleshooting

To see what Chomp got, the Shortcut's last action should be **Show Content** with *Contents
of URL*. Run it and read the answer:

- **`{"error": "unauthorized"}`**: the token is wrong. Open the Shortcut and check the Text
  box at the top holds their whole token and nothing else, or make a new one with **Replace
  token** and paste it there.
- **Fields under `received` are empty (`""`)**: the Shortcut found nothing in Health for
  them. Check Health permissions: **Settings › Privacy & Security › Health › Shortcuts**, and
  turn everything on (a "Don't Allow" on one type leaves that field empty). Also check nobody
  added a filter to the Find Health Samples actions; the Steps one should just be *Type is
  Steps*, *Start Date is in the last 7 days*, *Group By Day*.
- **Active calories equal steps**: a field points at the wrong action. In **Get Contents of
  URL**, check each row's blue bubble is the right one.

**Forgot their password?** You: **You › People**, tap them, **Reset password**, and send
them the new link. Their old password works until they use it.

**Remove someone:** **You › People**, tap them, type their name under **Remove** and tap
**Remove**. This deletes their whole log; foods stay.

**From the server instead** (if you prefer the command line):

```bash
cd ~/chomp
docker compose exec chomp chomp user add Sam              # asks for their password
docker compose exec chomp chomp user password Sam         # change it (signs them out)
docker compose exec chomp chomp user rename owner Alex   # your name on the dashboard
docker compose exec chomp chomp user list
```

The `BEARER_TOKEN` header, if you use it, is always the owner's.
