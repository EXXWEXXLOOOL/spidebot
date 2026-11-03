# SpideBot Admin Panel v2

The new authenticated panel is mounted at `/admin`. The old root page and
legacy unauthenticated management endpoints are intercepted and redirected to
the login page.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Configure environment variables in your hosting provider (do not commit a
   real .env file):

   - `ADMIN_USERNAME`: administrator login.
   - `ADMIN_PASSWORD`: long, unique password.
   - `SECRET_KEY`: optional high-entropy Flask session secret. If omitted,
     the app creates `DATA_DIR/admin_session.key` and attempts to restrict
     its permissions.
   - `COOKIE_SECURE=1`: recommended when the panel is served over HTTPS.
     For local HTTP-only development set `COOKIE_SECURE=0`.
   - Keep existing `DISCORD_TOKEN`, `SPIDE_COOKIE`, `PORT` and `DATA_DIR`
     configured as before.

3. Deploy the branch `feature/admin-panel-v2` and open
   `https://your-host/admin/login`.

## Included pages

- Overview: bot status, guild/member counts, latency, recent logs and actions.
- Analytics: INFO/WARNING/ERROR distribution from the in-memory log buffer.
- Leaderboard: current Spide listeners ranking (requires a valid Spide cookie).
- Users: cached Discord members and role information for a selected guild.
- Servers: connected guilds, member/channel/role counts.
- Logs: search and severity filters over the current process log buffer.
- Telegram Manager: add/remove public channel names and inspect last post IDs.
- Security: admin session settings and masked Spide cookie management.
- Backup Center: create/download/restore JSON config backups. Spide cookie is
  deliberately excluded from backups and preserved during restore.

## Security notes

- Use HTTPS in production. Do not expose the panel directly over plain HTTP.
- Do not commit real tokens, cookies, passwords or generated session keys.
- The panel uses Flask signed sessions, HttpOnly/SameSite cookies, CSRF
  tokens on state-changing forms, login checks, and POST-only admin actions.
- Existing legacy endpoints are blocked by a before-request guard.
- User/server pages are inspection-only. No ban/kick/role mutation controls
  are exposed in this first release.
- Analytics and logs are process-memory based; they reset on restart. Persistent
  analytics require a database/event store in a later iteration.
- Discord member listing depends on the Members intent and the bot's member
  cache. It may not represent every member unless member chunking is enabled.

## Operational caveat

The Flask development server is still used by the existing project bootstrap.
For public production hosting, place it behind a TLS reverse proxy and consider
moving to a production WSGI server in a separate deployment process.
