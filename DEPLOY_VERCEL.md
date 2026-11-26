# Vercel backend with Supabase persistence

Deployment dependencies no longer include sentence-transformers, PyTorch,
Streamlit or local Tesseract. Local use remains available with
`pip install -r requirements-local.txt` and `STORAGE_BACKEND=local`.

Cloud mode stores PDFs in a private Supabase bucket and metadata/custom knowledge
base names in Postgres. It calls a configurable embeddings API and a
Mistral-compatible OCR API. Qdrant Cloud continues to store vectors.

## Setup

1. Create/select a Supabase project. Run [supabase/setup.sql](supabase/setup.sql)
   in its SQL Editor. It creates `qa_domains`, `qa_documents`, processing
   functions, and a private `knowledge-pdfs` bucket. RLS and grants restrict
   database access to the backend service role. Keep the bucket private.
2. Add all variables in [.env.cloud.example](.env.cloud.example) to the backend
   Vercel project. Set each blank value; never upload `.env` files to GitHub.
   `SUPABASE_SERVICE_ROLE_KEY` is the legacy **service_role JWT**, not the public
   anon/publishable key. Use separate production and preview Supabase/Qdrant
   resources if previews must not change production documents.
3. Choose an embeddings provider with an OpenAI-compatible `/embeddings` endpoint.
   Set its full endpoint, API key, exact model ID and default output dimension.
   The app validates vector dimensions. DeepSeek's chat key is not automatically
   an embeddings key. No embeddings provider is silently selected or billed.
   OCR defaults to Mistral's `/v1/ocr` contract; set its key for image/scanned PDFs.
4. Set `CORS_ORIGINS` to your exact frontend origin. In Vercel, keep the backend
   root at this repository, use the FastAPI preset, and remove custom build
   commands that install `requirements-local.txt` or `.[local]`. The entrypoint
   is `api_server:app` in `pyproject.toml`.
5. Deploy both repositories. Frontend **Connection settings** must contain the
   backend origin, without `/api`. Backend `/api/health` should show `ok: true`
   and `storage: supabase`. This checks settings, not connectivity.
6. Upload a small text PDF, ask a question, reload and verify persistence.
   Upload an image PDF to verify OCR. Delete one PDF and verify the other
   document's vectors and saved file remain intact.

## Authenticator codes fail only on Vercel

If local sign-in succeeds and Production rejects authenticator codes for
multiple roles, confirm both backends use the same Supabase project and
`STORAGE_BACKEND=supabase`. When both environments share authenticator
enrollments, set the backend's Production `AUTH_COOKIE_SECRET` to the exact
existing value from the environment where the enrolled authenticator works.
If omitted, the backend falls back to the Supabase service-role key, which can
derive a different authenticator key from the same saved enrollment.

Redeploy the backend after saving the Production variable. Start sign-in again
and use a fresh code at the next authenticator rollover. Check this configuration
before resetting any enrollment. Keep the secret in backend environment settings.

## Processing and limits

Vercel uploads are limited here to **4,000,000 bytes per PDF**, leaving room for
multipart overhead below the platform request limit. Split larger PDFs first;
direct browser-to-Supabase uploads are not implemented in this version.

The UI starts a durable job and calls one `/documents/{hash}/step` request per
page. Each step performs OCR when needed, optional cleaning, embeddings and
Qdrant upsert. Chunks are persisted before embedding so retries use identical
point IDs/content. Postgres leases prevent concurrent processing of one PDF.
Leases expire after five minutes if a function is terminated.

Keep the tab open while processing. After a failure or closing the tab, select
the base and click **Resume saved documents**. Pending PDFs appear in the list
and can be deleted when not actively leased. Search excludes pending PDFs until
their final page commits. A very complex single page may still time out; split
or simplify that PDF. Function duration is configured to 300 seconds; verify
this fits your Vercel plan/settings. Cloud reset/force rebuild is disabled:
delete an individual PDF, then upload it again.

Cloud processing preserves all chunks instead of applying the local pipeline's
vector near-duplicate removal, keeping retries deterministic. Cleaning remains.

## Existing documents and vectors

This change does not delete old local PDFs, JSON registries or Qdrant collections.
Cloud collection names include a hash of the embeddings endpoint/model/dimension,
preventing incompatible embedding spaces from mixing, even at equal dimensions.
Changing those settings creates a new namespace; use stable endpoint/model IDs.

Recreate custom base names and re-upload PDFs through the cloud UI. They must be
indexed using the new model before cloud search works. Local registries and
vectors are not automatically migrated. Keep old collections until validation.
A base name cannot be removed while another cloud embedding namespace still has
documents in that base.

Email/password accounts now use Supabase Auth. Complete [AUTH_SETUP.md](AUTH_SETUP.md), run `supabase/auth.sql`, set JWT expiry to 900 seconds, and enable Secure cookies. Use `SameSite=none` for unrelated frontend/backend sites (subject to browser third-party-cookie restrictions); prefer same-site hosting. RAG documents and profiles are private to their owner. Before deploying this version over an existing installation, run `supabase/workspace_isolation.sql` and follow [WORKSPACE_ISOLATION.md](WORKSPACE_ISOLATION.md). Existing data remains with admin; new users get separate empty workspaces.

## Validation

Offline tests exercise embeddings validation, resumable ingestion and deletion.
Supabase SQL, provider credentials and actual Linux bundle size still require
verification against the configured deployment.

References: [Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi),
[Vercel limits](https://vercel.com/docs/functions/limitations),
[Supabase REST API](https://supabase.com/docs/guides/api),
[Mistral OCR](https://docs.mistral.ai/studio/document-processing/basic_ocr).

## Hierarchical permissions rollout

Apply the existing security settings, Super Admin, role, upload approval,
activity-log and revision migrations first. Then apply
[hierarchical_permissions.sql](supabase/hierarchical_permissions.sql) and
[scoped_authority.sql](supabase/scoped_authority.sql), followed by
[audit_administration.sql](supabase/audit_administration.sql).
Deploy the backend and frontend together with `STORAGE_BACKEND=supabase`.
No new environment variables are required.

Admin and User share the same action catalog. A role module explicitly disabled
in Permissions List applies that gate to every account in the role. A module
toggle preserves action settings, and individual overrides can enable an
opt-in module when the role has no explicit denial. Under Permission Matrix,
select an individual account and save its delegated account scope separately
from its actions. Existing credentials and records are preserved. Enable scope
for previously delegated accounts before they perform targeted account writes.

Delegated role assignments and account changes require the relevant action and
target scope. Delegates cannot assign scopes, alter their own account authority,
or manage protected accounts. Shared administrative defaults and global role
suspension are reserved for Super Admin to prevent indirect self-escalation and
site-wide lockouts. Role definitions are fixed. Role Management Delete removes
Admin assignment. User Details Delete permanently deletes a scoped, inactive
account. Creation and permanent deletion are audit events that cannot be reversed.
Audit View grants are site-wide.

Stage the SQL and verify scoped promotion and demotion, dynamic revocation,
concurrent stale-authority rejection, audit and revision entries, and reversal
with all three roles. PostgreSQL policy checks run at delegated settings writes,
and RLS keeps workspace and administrative tables inaccessible to direct browser
writes. Requests already in progress may finish. Local SQLite with Supabase Auth
does not provide a single cross-store transaction. Keep the service-role key
server-side.

### Administrative actions

User Details Add creates or invites a User with current role defaults. A delegate
needs User Details View/Add, scope for User accounts, and enough authority for
the effective permissions the new User would inherit. Role Management handles
Admin creation and role assignments.

Permission Matrix Add writes a new explicit action setting, Edit changes an
existing one, and Delete removes it so the inherited value applies. Existing
delegates with Edit need an explicit Add or Delete grant for the corresponding
setting operations. Module switches still use Permission Edit.

Logs and Revisions use an append-only journal. Log corrections attach to the
original event and show the correcting actor and time. Delete archives a record
from the active view, and restores require the same authority. Include archived
reveals those records, and the raw login CSV still exports original events.
Manual revisions document decisions separately from reversible settings
snapshots. All journal operations append a Role Management activity event.
Delegated audit writes require a scoped account. Global records, records without
a resolvable account, and protected or self accounts are reserved for Super
Admin. Verify journal authorization, archive/restore, and old audit and revision
visibility during staging.
