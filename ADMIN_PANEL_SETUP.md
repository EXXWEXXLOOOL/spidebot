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
   - `ADMIN_PASSWORD`: long, unique password for the first panel owner.
   - On first startup, the app creates an owner account from these two
     variables if the admin database is empty. Later account changes are
     managed in the panel and do not require editing environment variables.
   - `SECRET_KEY`: optional high-entropy Flask session secret. If omitted,
     the app creates `DATA_DIR/admin_session.key` and attempts to restrict
     its permissions.
   - `COOKIE_SECURE=1`: recommended when the panel is served over HTTPS.
     For local HTTP-only development set `COOKIE_SECURE=0`.
   - Keep existing `DISCORD_TOKEN`, `SPIDE_COOKIE`, `PORT` and `DATA_DIR`
     configured as before.

3. Deploy the configured `main` branch and open
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
- Access Management: owner can create administrator and read-only accounts,
  disable/reactivate them, and remove non-owner accounts.
- Backup Center: create/download/restore JSON config backups. Spide cookie is
  deliberately excluded from backups and preserved during restore.
- Automation Center: configurable Telegram polling and daily top schedule (UTC).
- Notification Center: Discord startup/disconnect/command-error alerts and a test send.
- Moderation Center: timeout, kick, ban and unban with permission and role-hierarchy checks.

## Security notes

- Use HTTPS in production. Do not expose the panel directly over plain HTTP.
- Do not commit real tokens, cookies, passwords or generated session keys.
- Admin accounts are stored in `DATA_DIR/admin_users.sqlite3`; keep
  `DATA_DIR` on persistent storage or accounts will be recreated from the
  bootstrap environment credentials after a host reset.
- Passwords are stored as Werkzeug password hashes, not plaintext.
- Roles: Owner (full access and account management), Admin (panel operations
  except account management), Viewer (read-only; all POST changes are blocked).
- Only the initial Owner is bootstrapped from `ADMIN_USERNAME` and
  `ADMIN_PASSWORD`. Additional users are created from the Access Management page.
- Do not delete or disable the owner account from the panel; owner protections
  prevent self-lockout and removal of the main owner.
- The panel uses Flask signed sessions, HttpOnly/SameSite cookies, CSRF
  tokens on state-changing forms, login checks, and POST-only admin actions.
- Existing legacy endpoints are blocked by a before-request guard.
- Moderation is disabled by default. Set `MODERATION_ENABLED=1` only after
  granting the bot the relevant Discord permissions; action attempts are audited.
- Analytics and logs are process-memory based; they reset on restart. Persistent
  analytics require a database/event store in a later iteration.
- Discord member listing depends on the Members intent and the bot's member
  cache. It may not represent every member unless member chunking is enabled.

## Operational caveat

The Flask development server is still used by the existing project bootstrap.
For public production hosting, place it behind a TLS reverse proxy and consider
moving to a production WSGI server in a separate deployment process.
