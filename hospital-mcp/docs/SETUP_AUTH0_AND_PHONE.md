# Real sign-in (Auth0) and using the server from Claude on your phone

Claude on the phone cannot run an MCP server. Instead, the server runs online at an HTTPS address.
You add it to Claude as a **custom connector** once, on claude.ai, and it then appears in Claude on
the web, desktop and mobile apps. When you connect, Claude opens the hospital's Auth0 sign-in page; after
that, every tool call carries your token, and the server works out your hospital and role from it.

```
Claude (phone) ──HTTPS + bearer token──▶ hospital-mcp on Render ──▶ MongoDB Atlas
        │                                        ▲
        └──── sign-in (OAuth) ──▶ Auth0 ─────────┘ (server checks Auth0's signature, issuer, audience)
```

Time needed: about 45 minutes the first time. Everything below has a free tier.

## 1. Database: MongoDB Atlas

1. Create a free **M0** cluster at <https://cloud.mongodb.com>.
2. Database Access → add a user (password auth). Network Access → allow `0.0.0.0/0` for the demo
   (Render's free tier has no fixed IP).
3. Connect → Drivers → copy the `mongodb+srv://...` string. This is `MONGO_URI`.
4. Seed it once from your laptop, using **your own e-mail** as the Dummy Hospital admin and platform admin:
   ```bash
   MONGO_URI="mongodb+srv://..." python -m hospital_mcp.cli seed --admin-email you@gmail.com --platform-email you@gmail.com
   ```
   (Windows PowerShell: `$env:MONGO_URI="mongodb+srv://..."; python -m hospital_mcp.cli seed --admin-email ...`)

## 2. Deploy the two services on Render

1. Push this folder to a GitHub repo (the `.gitignore` keeps `.env` out).
2. Render → **New + → Blueprint** → pick the repo. `render.yaml` creates `hospital-mcp` and `hospital-portal`.
3. Note the two URLs, e.g. `https://hospital-mcp-xxxx.onrender.com` and `https://hospital-portal-xxxx.onrender.com`.
   You will fill the environment variables after step 3.

Free Render services sleep after ~15 minutes idle. The first request after that takes ~50 s, so open
`https://<mcp-url>/health` in a browser before a demo.

## 3. Auth0

Create a free tenant at <https://auth0.com>. Then, in the dashboard:

**a. Tenant settings** (Settings → Advanced):
- turn on **Dynamic Client Registration**. Claude registers itself as an OAuth client this way.
- turn on **Resource Parameter Compatibility Profile**. Claude sends `resource=<your MCP URL>` instead of
  Auth0's `audience`. Without this, Auth0 rejects the request with "The userinfo audience is not allowed
  for third party clients".

**b. API** (Applications → APIs → Create API):
- Identifier: **exactly** `https://<mcp-url>/mcp` (no trailing slash). This becomes `AUTH0_AUDIENCE`.
- Signing algorithm: RS256.

**c. Login connection**: Authentication → Social → enable **Google** (simplest: e-mails are verified).
Then make it available to dynamically registered clients by promoting it to a *domain-level connection*
(Authentication → the connection → settings, or via the Management API: `PATCH /api/v2/connections/{id}`
with `{"is_domain_connection": true}`).

**d. Put the user's e-mail into access tokens** (Actions → Library → Build custom → trigger *Login / Post Login*),
then deploy it and drag it into the Login flow:
```js
exports.onExecutePostLogin = async (event, api) => {
  if (event.user.email && event.user.email_verified) {
    api.accessToken.setCustomClaim("https://hospital-mcp/email", event.user.email);
  }
};
```
The server links your Auth0 account to your staff record by this verified e-mail on first sign-in, and
after that by Auth0's user id.

**e. Web portal application** (Applications → Create Application → *Regular Web Application*):
- Allowed Callback URLs: `https://<portal-url>/callback` (and `http://localhost:5000/callback` for local testing)
- Allowed Logout URLs: `https://<portal-url>/signed-out`
- Copy Domain, Client ID and Client Secret.

## 4. Environment variables on Render

| Service | Variable | Value |
|---|---|---|
| both | `MONGO_URI` | Atlas string from step 1 |
| both | `AUTH0_DOMAIN` | e.g. `dev-abc123.us.auth0.com` |
| both | `AUTH0_AUDIENCE` | `https://<mcp-url>/mcp` |
| hospital-mcp | `MCP_PUBLIC_URL` | `https://<mcp-url>` |
| hospital-portal | `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET` | from step 3e |
| hospital-portal | `WEB_BASE_URL` | `https://<portal-url>` |
| hospital-portal | `GROQ_API_KEY` | your new Groq key |

Check: `https://<mcp-url>/health` shows `"auth_mode": "auth0"`, and
`https://<mcp-url>/.well-known/oauth-protected-resource/mcp` lists your Auth0 domain under `authorization_servers`.

## 5. Add it to Claude (then it is on your phone too)

1. On **claude.ai** (browser): Settings → Connectors → **Add custom connector**.
2. Name: `Hospital`. URL: `https://<mcp-url>/mcp` (the same string as the Auth0 API identifier).
3. Click **Connect**. Auth0's page opens; sign in with Google using the e-mail you seeded.
4. Open the Claude app on your phone. The connector is already there (connectors sync across your devices).
   Enable it in a chat from the tools menu and ask: *"What ICU beds are free?"* or
   *"Book Dr. Sharma tomorrow at 3 PM for MRN-1001"*.

What you see depends on who signed in: a receptionist gets ~22 tools, an admin ~36, and a doctor sees only their own patients.

### Giving teammates access

Sign in to the portal as the hospital admin → **Admin console → Add a staff user** with their e-mail and role.
They connect the same connector URL and sign in with that e-mail. To onboard another hospital, sign in as the
platform admin → **Hospitals → Onboard a hospital**.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Auth0: "userinfo audience is not allowed for third party clients" | Step 3a: Resource Parameter Compatibility Profile is off |
| Claude: "doesn't support automatic client registration" | Step 3a: Dynamic Client Registration is off |
| Signed in, but Claude shows no tools; tools return `UNAUTHENTICATED ... not registered` | Your e-mail is not in `staff_users`, or the Action (3d) is not in the Login flow, or the e-mail is unverified |
| Every call is 401 | `AUTH0_AUDIENCE` on the server is not exactly the API identifier / connector URL |
| Auth0 shows no login button | The connection is not domain-level (3c) |
| First call takes a minute | Render free tier woke up from sleep |

## Quick test from your phone without deploying

Run the server on your laptop and expose it with a tunnel (`cloudflared tunnel --url http://localhost:8000`).
Use the printed `https://....trycloudflare.com` as `MCP_PUBLIC_URL`, and `.../mcp` as the Auth0 API identifier
and connector URL. The tunnel URL changes every time you restart it, so this is only for a quick try.
