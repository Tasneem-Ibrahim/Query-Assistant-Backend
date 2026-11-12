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

Generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Keep it stable across workers and restarts. If omitted, the existing backend-only service-role key signs the session deadline. Auth requests use the anon/publishable key when set, otherwise the existing service-role key remains server-side as the API key. Never put a service-role key in frontend configuration. `ADMIN_USERNAME`/`ADMIN_PASSWORD` retain the existing admin credentials. Choose **Admin sign in** and enter that username/password; normal users continue to use Supabase email/password accounts.

For HTTPS deployment set `AUTH_COOKIE_SECURE=true`, and set exact frontend origins in `CORS_ORIGINS`. For unrelated frontend/backend sites, set `AUTH_COOKIE_SAMESITE=none` as well. Browsers may block third-party cookies: deploy behind one origin or use frontend/backend subdomains on the same site for reliable production sessions. Cookies are host-only, HttpOnly, and scoped to `/api`; the refresh endpoint never returns token values to JavaScript. Protected changes require a CSRF header and an allowed Origin.

Restart the backend and frontend after configuration. The frontend build needs only its existing public `BACKEND_URL`; no Supabase browser SDK or additional npm dependency is needed.

## Behavior and demo

- Open the frontend: the RAG workspace stays hidden until the session is verified.
- Choose Create an account; enter name, email, password (minimum 8 characters), and matching confirmation. Both password fields have show/hide controls.
- If confirmation is enabled, confirm the email, return to the app, and sign in. Otherwise signup opens the workspace immediately.
- The menu displays the user's name. Reload resumes the session using cookies. Once access expires, the next protected request refreshes and retries automatically. Concurrent requests in a tab share the refresh request.
- Sign out revokes the Supabase refresh session and removes cookies. An expired or altered absolute-session cookie requires a fresh sign-in.

The existing RAG knowledge bases remain a **shared workspace for signed-in users**; this change adds account authentication and private profile rows, not per-user document ownership. CLI/Streamlit entrypoints remain local tools outside the HTTP login flow.

## Verification

`python -m unittest discover -s tests -p test_admin_auth.py -v` covers signup, confirmation, cookie flags, validation, wrong credentials, provider outages, access/session expiry, refresh rotation, CSRF, protected routes and logout with a mocked Supabase transport. `npm test` covers cookie requests, refresh/retry and concurrent refresh, alongside RAG and voice regressions. A live Supabase signup, table-row check and login still need a reachable, configured project.

References: [Supabase user data](https://supabase.com/docs/guides/auth/managing-user-data), [sessions and JWT expiry](https://supabase.com/docs/guides/auth/sessions), [Auth REST endpoints](https://supabase.github.io/auth/).

## Existing admin access

The login screen includes **Admin sign in**. Admin uses the original `ADMIN_USERNAME` and `ADMIN_PASSWORD` restored in the local `.env`; no Supabase signup is required. `POST /api/auth/admin/login` creates a signed 15-minute JWT and a seven-day refresh session, with only a keyed hash of the refresh token stored in `DATA_DIR/admin_sessions.sqlite3`. Refresh rotates the token; logout revokes the session; changing the admin credentials invalidates existing admin sessions. Normal user signup/login still uses Supabase Auth and the `public.users` profile trigger.

The local admin session store requires persistent storage shared by backend workers. On Vercel the current `DATA_DIR` is temporary and per-instance; use a persistent shared session store before relying on admin session persistence across serverless instances. Admin login credentials must also be configured in the deployment environment; editing the local `.env` does not update Vercel.
