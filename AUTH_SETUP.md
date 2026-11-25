# Authentication setup

The app now uses Supabase Auth for email/password signup, password hashing, signed JWTs, refresh-token rotation and logout. `public.users` holds the profile; Supabase's `auth.users` holds password hashes. No password or token is stored in browser localStorage/sessionStorage.

## Supabase project (required once)

1. Run [supabase/auth.sql](supabase/auth.sql) in your project's SQL Editor. It creates `public.users`, a trigger that copies new accounts into it, and a policy allowing each authenticated user to read only their own profile. Existing Auth users are backfilled. Do not create a password column in `public.users`.
2. Enable the Email provider and allow new signups in Authentication settings.
3. Set the **Access token (JWT) expiry** to **900 seconds (15 minutes)** in Authentication settings. The app also refuses access tokens older than 900 seconds, even if the project still uses its previous expiry. The Supabase setting is necessary for the JWT's own `exp` claim to be exactly 15 minutes.
4. Keep email confirmation enabled for production. Add the frontend URL as the Site URL and an allowed redirect URL. Users confirm the email and then return to sign in. No browser token import from URL fragments is needed. For a classroom demo, disabling Confirm email allows immediate sign-in after signup.
5. Where available, set **Time-box user sessions** to **168 hours (7 days)**. The app independently enforces a signed, absolute seven-day cookie session limit; refresh does not extend it. The project setting also bounds sessions used outside this app.

## Backend environment

Retain the existing provider/storage settings. Add these settings to the backend environment only:

```dotenv
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_ANON_KEY=YOUR_ANON_OR_PUBLISHABLE_KEY
AUTH_COOKIE_SECRET=YOUR_RANDOM_SECRET_AT_LEAST_32_BYTES
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
AUTH_COOKIE_SECURE=false
AUTH_COOKIE_SAMESITE=lax
```

Generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Keep it stable across workers and restarts. If omitted, the existing backend-only service-role key signs the session deadline. Auth requests use the anon/publishable key when set, otherwise the existing service-role key remains server-side as the API key. Never put a service-role key in frontend configuration. `ADMIN_USERNAME`/`ADMIN_PASSWORD` retain the existing admin credentials. Use the shared **Email or Username** form: administrators enter their configured username; normal users enter their Supabase email/password.

For HTTPS deployment set `AUTH_COOKIE_SECURE=true`, and set exact frontend origins in `CORS_ORIGINS`. For unrelated frontend/backend sites, set `AUTH_COOKIE_SAMESITE=none` as well. Browsers may block third-party cookies: deploy behind one origin or use frontend/backend subdomains on the same site for reliable production sessions. Cookies are host-only, HttpOnly, and scoped to `/api`; the refresh endpoint never returns token values to JavaScript. Protected changes require a CSRF header and an allowed Origin.

For the current separate Vercel projects, set these in the **backend Vercel project's environment variables**, then redeploy that project:

```dotenv
CORS_ORIGINS=https://query-assistant-frontend.vercel.app,http://localhost:3000,http://127.0.0.1:3000
AUTH_COOKIE_SECURE=true
AUTH_COOKIE_SAMESITE=none
```

Keep local HTTP development at `AUTH_COOKIE_SECURE=false` and `AUTH_COOKIE_SAMESITE=lax`. Local `.env` edits do not update Vercel. Frontend Vercel builds use `BACKEND_URL=https://query-assistant-backend.vercel.app`. A generic “Cannot reach the backend” message can also mean an unavailable backend or a blocked/redirected request; it alone does not prove a CORS failure. Check the browser Network panel for the failed request and OPTIONS response. These cookie settings still depend on the browser allowing third-party cookies; admin sessions additionally require the shared persistent store described below.

Restart the backend and frontend after configuration. The frontend build needs only its existing public `BACKEND_URL`; no Supabase browser SDK or additional npm dependency is needed.

## Behavior and demo

- Open the frontend: the RAG workspace stays hidden until the session is verified.
- Choose Create an account; enter name, email, password (minimum 8 characters), and matching confirmation. Both password fields have show/hide controls.
- If confirmation is enabled, confirm the email, return to the app, and sign in. Otherwise signup goes directly to authenticator setup.
- The menu displays the user's name. Reload resumes the session using cookies. Once access expires, the next protected request refreshes and retries automatically. Concurrent requests in a tab share the refresh request.
- Sign out revokes the Supabase refresh session and removes cookies. An expired or altered absolute-session cookie requires a fresh sign-in.

RAG workspaces are now private to each account. Complete [WORKSPACE_ISOLATION.md](WORKSPACE_ISOLATION.md), including its required SQL migration, before deploying this version. Existing shared documents/custom bases are retained exclusively in the original admin workspace. Other accounts start with no knowledge bases or documents; Customer Support and Legal Assistant are admin-only. Users create their own custom bases. A first-visit welcome guide stores its presentation preference in Supabase Auth user metadata; no additional SQL migration is needed. Admin has no HTTP access to other users' workspaces. Local CLI/Streamlit entrypoints operate on the original admin workspace.

## Verification

`python -m unittest discover -s tests -p test_admin_auth.py -v` covers signup, confirmation, cookie flags, validation, wrong credentials, provider outages, access/session expiry, refresh rotation, CSRF, protected routes and logout with a mocked Supabase transport. `npm test` covers cookie requests, refresh/retry and concurrent refresh, alongside RAG and voice regressions. A live Supabase signup, table-row check and login still need a reachable, configured project.

References: [Supabase user data](https://supabase.com/docs/guides/auth/managing-user-data), [sessions and JWT expiry](https://supabase.com/docs/guides/auth/sessions), [Auth REST endpoints](https://supabase.github.io/auth/).

## Existing admin access

The login screen has one **Email or Username** and **Password** form, without a separate admin option. `POST /api/auth/login` accepts `identity` and `password`; legacy `email` payloads remain supported. The backend matches the configured admin username and verifies the original `ADMIN_USERNAME`/`ADMIN_PASSWORD`, without Supabase signup. Email identities continue through Supabase Auth. Unknown usernames and wrong credentials return the same generic 401; client role fields and user metadata cannot grant admin access. The legacy `/api/auth/admin/login` API remains secured for backwards compatibility but is not used or displayed by the UI.

Admin sessions retain signed 15-minute JWTs and seven-day refresh sessions. Keyed refresh-token hashes live in `DATA_DIR/admin_sessions.sqlite3` in local mode and in the service-role-only `qa_admin_sessions` table in Supabase mode. After two-factor verification the app navigates to `index.html`, even when sign-in starts from a workspace URL. Verified Admins open `dashboard.html` from the Homepage, and regular Users open `workspace.html`. Already authenticated page reloads resume the current page. Signup remains email-only with existing confirmation behavior.

The local admin session store requires persistent storage shared by backend workers. On Vercel the current `DATA_DIR` is temporary and per-instance; use a persistent shared session store before relying on admin session persistence across serverless instances. Admin login credentials must also be configured in the deployment environment; editing the local `.env` does not update Vercel.

## Admin dashboard and upload approval rollout

Run `supabase/admin_panel.sql` after the existing auth/workspace migrations, then
`supabase/upload_jobs.sql` for saved-upload storage/metadata if not applied yet.
Run **`supabase/browser_processing.sql`** for browser-driven, owner-bound processing.
No Cron, pg_net, Vault, scheduled worker or worker secret is required. These SQL
changes are prepared locally, not run against production. See
[UPLOAD_WORKFLOW.md](UPLOAD_WORKFLOW.md) for safe rollout and existing-job retention.

The single Email or Username form and shared Homepage are unchanged. Its Dashboard
button opens the verified role's Admin Dashboard or private User Workspace. Admin
manages approval metadata; document APIs retain per-account ownership checks.

Regular users save a PDF/image once. Admin approval permits processing when the
User Dashboard is open; the browser resumes the existing page-by-page RAG workflow.
Closing the page stops further requests; stored progress resumes on return without
re-uploading. Pending/rejected/blocked files cannot be processed. One processing
failure permits one retry, preserved across page reloads and restarts. Original
Admin direct ingestion and Supabase user auth/refresh/logout are unchanged.

Pending/rejected 30-day retention is now checked on Admin access, not scheduled.
No database tables, records, private files or existing attempt counts are dropped
by the transition migration. Frontend/backend must be deployed together. Existing
OCR setup, Vercel request limits and serverless admin-session persistence remain
independent limitations. No live deployment/provider checks have been performed.

## Super Admin rollout

1. Apply [supabase/super_admin.sql](supabase/super_admin.sql) after the existing
   auth, workspace isolation, admin panel, and saved-upload migrations. It
   adds a service-role-only audit table and account-management functions.
2. Set SUPER_ADMIN_USERNAME and SUPER_ADMIN_PASSWORD in the backend
   environment. The unified Email or Username form accepts the configured
   Super Admin login. Existing local Admin credentials stay in their
   original variables. Supabase-managed Admins continue signing in by email.
3. Apply the security migration below, then restart the backend and build
   or deploy both applications together.

The Permission Matrix edits managed role defaults and account overrides.
Permissions List enables or disables whole modules for a managed role. A
disabled module blocks its actions even for accounts with overrides. The
original Admin has separately editable permissions in the Admin tab of the
Permission Matrix. Role Management can pause or resume its workspace access
while its environment-backed sign-in stays available. Super Admin can grant
or revoke an Admin's upload approval access from the account row. Role
assignment, permission changes for other accounts, workspace selection, and
permanent account deletion require Super Admin. Existing upload approval
policy continues to apply to regular User uploads.

To let an Admin add or delete files for an active User, enable the relevant
**User uploads** actions in the Permission Matrix. View is required to open
the upload manager. The Admin also needs User details and Knowledge bases View
and the corresponding Documents action. From **All users**, select a User and
choose **Manage uploads**. The manager lists that user's knowledge bases and
files and processes changes in that user's workspace. Document preview, chat,
search, and knowledge-base changes are outside this delegated access.

Apply [supabase/admin_upload_approvals.sql](supabase/admin_upload_approvals.sql)
after the upload and browser-processing migrations to enable the new approval
request actions. Super Admin can grant **Upload approvals → Add** and **Delete**
in the Admin Permission Matrix. **Add approval request** is under **All users →
selected user → upload requests**. Delete removes the approval request from the
lists and leaves registered documents in place. Requests with an active
processing lease can be removed after processing releases that lease.

Deactivate blocks access and retains workspace data. Permanent deletion
clears the account's application records, private files, and search collections
in the configured providers and removes the Supabase Auth account. Audit
history retains the action and target ID. If processing is still finishing or a
provider fails during deletion, retry from Role Management. Check rollout
with a disposable account before using permanent deletion on real data.

### Managed Admin role and invitations

Apply [supabase/super_admin_roles.sql](supabase/super_admin_roles.sql) after
super_admin.sql. Role Management can then deactivate the managed Admin role
without removing accounts or workspace data. The server checks the role's
status on sign-in, session refresh, and authenticated API requests. Reactivate
the role or reassign affected accounts to User to restore access. The original
environment-backed Admin account remains available.

Create or invite an account from Role Management by selecting User or Admin,
then setting its permissions. An invitation generates a one-time link to copy
and share privately. The invitee opens the link and chooses a password. Supabase
verifies the invite token and stores the password. The invitee must set up 2FA
before the app issues a workspace session.

### Mandatory two-factor sign-in and login logs

In Supabase mode, apply [supabase/security_settings.sql](supabase/security_settings.sql)
before starting the updated backend. It persists authenticator enrollment, login
logs, managed-role permissions and environment-backed admin sessions. Local mode
creates its SQLite databases on first use. Keep the same private `AUTH_COOKIE_SECRET`
across instances and restarts. Changing it invalidates existing sessions and
requires coordinated authenticator re-enrollment.

An existing browser session must sign in again after this update. Each account
enrolls once by scanning the QR code or entering the manual time-based key,
then confirms a six-digit code.
Subsequent sign-ins prompt for a fresh code. Setup and verification attempts
expire after five minutes. Sign-in still uses the original environment
credentials for Admin and Super Admin, and Supabase Auth for managed accounts.

For a lost authenticator, verify the account holder through your usual support
process, then clear the affected `factor:<account ID>` key from
`public.qa_security_state` (Supabase) or the `security_state` table in
`DATA_DIR/security.sqlite3` (local). Use `local-admin` for the original Admin
and `00000000-0000-4000-8000-000000000001` for Super Admin. The next
password sign-in prompts for a new setup. If the account may be compromised,
also revoke its sessions and change its password.

Super Admin can browse Login Logs and export them as CSV. Logs record the
timestamp, account ID when known, role when known, and outcome. The app keeps
prior entries for audit when an account is deleted. Workspace documents and
authenticator details are excluded from the log.
