# Add another person

Each person gets their own meal log, targets, portions and saved meals; the food list is
shared. You are the owner (everything logged before this existed is yours) and keep signing
in with `AUTH_PASSWORD`.

On the dashboard, go to **You › People**, type their name and tap **Add person**. Chomp shows
an invite link with a **Copy** button: send it to them. It works once, for 7 days; opening it
lets them pick their own password (12+ characters) and signs them in, so you never see it. If
the link gets lost or they forget their password, open them on People and tap **New invite
link** or **Reset password** (their old password works until the new link is used). People is
also where you rename someone or remove them with their whole log (rename yourself with
`chomp user rename owner Alex` on the server). If Cloudflare Access
covers `/app` ([setup, step 7](setup.md#optional-cloudflare-access-in-front-of-the-dashboard)), add their email to its policy first, or the link stops at Access.

The same from the server, if you prefer:

```bash
cd ~/chomp
docker compose exec chomp chomp user add Sam              # asks for their password
docker compose exec chomp chomp user rename owner Alex   # optional: your name on the dashboard
docker compose exec chomp chomp user list
```

Their password must differ from everyone else's: Chomp tells people apart by password alone,
so the sign-in pages stay a single box. Then, in **their own** Claude account,
they add the connector exactly as in [setup, step 6](setup.md#6-add-the-connector-in-claude) (same URL, same `OAUTH_CLIENT_ID` and
`OAUTH_CLIENT_SECRET`) and type **their** password on the "Connect Claude to Chomp" page. The
dashboard is the same address; their password opens their log. Then, signed in as
themselves, they connect their iPhone ([Apple Health](apple-health.md)) and Hevy ([Hevy](hevy.md)) on **You › Integrations**,
and can bring in their Health history there too. To change someone's password from the server
(it signs them out everywhere): `docker compose exec chomp chomp user password Sam`. The
`BEARER_TOKEN` header, if you use it, is always the owner's.

If they use ChatGPT, their connector gets its own callback URL, which must be added to
`CONNECTOR_CALLBACKS` in `chomp/auth.py` first (see [Add the ChatGPT connector](setup.md#add-the-chatgpt-connector)).
