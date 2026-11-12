# Private account workspaces

Each authenticated account owns its documents, custom knowledge bases, OCR previews, processing jobs and searchable vectors. Identity comes from the verified backend session, never an owner ID supplied by the browser. Admin keeps the original login credentials and the existing legacy data. Admin does not gain access to other users' workspaces.

## Deployment: required database step

1. For an existing Supabase installation, run **`supabase/workspace_isolation.sql`** in the Supabase SQL Editor. For a new installation, run the updated `supabase/setup.sql` and the existing `supabase/auth.sql`.
2. Deploy the updated backend and frontend together. During the switch, restrict access to the old backend: the old version still exposes shared data to signed-in users. The new backend deliberately fails closed if the owner column has not been migrated.
3. Test with the original admin and two separate Supabase accounts. Admin should retain old documents. Both users should start empty; uploading the same PDF to each must create independent entries. Verify preview, search, questions, deletion and job resume. Test direct URLs copied from another account too.

The migration adds owner-qualified custom-base keys/names and assigns existing custom bases to `admin`. It does not delete or move documents, Storage objects or Qdrant vectors. Existing document namespaces, object paths and collections become admin-only; new users never fall back to them. Previously shared data cannot be retroactively attributed to an individual uploader, so all existing data is assigned to admin as requested. Do not roll back to a shared-workspace backend after enabling private accounts.

No new environment variables or API keys are needed. Existing `.env`, `.env.vercel` and examples retain their provider/auth settings. Backend Supabase service-role credentials and Qdrant credentials stay server-side. Keep the bucket private and do not add browser-access Storage policies or direct table grants. The migration locks the app metadata tables to service-role access and sets the existing `knowledge-pdfs` bucket private. For a custom `SUPABASE_BUCKET`, ensure it is private separately. Existing permissive Storage policies, if added outside this repository, must be removed/reviewed separately; the live database policies have not been audited here.

## Implementation

- `src/rag/workspace.py` binds the verified Supabase UUID or the special local-admin identity to request-local context. HTTP auth rejects invalid identities. Context is reset after requests and explicitly copied into local ingestion worker threads; global config is never changed per account.
- The old `api.py` entrypoint now aliases `api_server.app`; it cannot expose the obsolete unauthenticated shared-workspace API. Its old request/route contract is intentionally removed.
- Custom bases: `qa_domains.owner_id` plus `(owner_id, key)` primary key and owner-qualified name uniqueness. Every registry read/write/delete includes owner identity. There is no shared mutable custom-domain dictionary.
- Cloud documents: the existing `qa_documents.namespace` ownership boundary is combined with embedding configuration: admin keeps `api_<embedding hash>`, users use `api_<embedding hash>_user_<UUID hex>`. Every document query, conflict key, claim/advance/release RPC and deletion uses that namespace. The current document schema/RPCs already include namespace in their keys and predicates, so their signatures do not change.
- Storage object paths and Qdrant collections include that namespace. Object helpers reject foreign paths, and vector helpers reject foreign account collections. Search also restricts results to ready documents in the active namespace. Identical PDF hashes in different accounts do not share storage objects or vector collections.
- Local mode separates PDFs, custom registries, ingestion registries and extraction/LLM caches under `workspaces/user_<UUID hex>`, and uses account-specific collections. Original admin paths remain unchanged. The shared Qdrant client uses separate named collections, not mutable per-request connection settings.
- The frontend clears documents, chats, previews, uploads and processing logs at session changes, aborts old requests, and guards delayed results/page jobs. Login/logout in another tab signals session revalidation through BroadcastChannel where supported; a restored browser history page also revalidates.

This is application-enforced ownership with backend-only database access, not user-token RLS on `qa_documents`: the service role bypasses RLS, and namespace enforcement is therefore essential. A leaked service-role/Qdrant credential can bypass application ownership. No live SQL execution or deployment is implied by the files in this repository. Local CLI/Streamlit remain trusted admin tools rather than multi-user HTTP interfaces.

## Fresh workspaces and welcome guide

Regular users receive only their own custom bases. A brand-new account receives `[]` from `/api/domains`; built-in routes are admin-only, including direct API URLs. Admin and trusted local CLI/Streamlit retain both built-ins. No data is deleted. Any regular-account uploads previously made inside built-in bases remain stored in their prior private namespace but are not automatically moved to custom bases or exposed through the now admin-only routes.

The frontend accepts an empty list as a connected workspace. **New knowledge base** stays enabled; upload is enabled after creating/selecting a base. An accessible card guides the user through creating a base, uploading a PDF/image and asking/searching. Its main action opens the creation form. Close, Escape and **I'll explore first** acknowledge the guide without creating anything.

`POST /api/auth/onboarding/complete` requires the authenticated session and CSRF protection. It updates only `workspace_guide_seen=true` through the signed-in user's Supabase `/user` endpoint. Profile/session responses expose `onboarding_seen`. This is a presentation preference, never an ownership/role authority. Other metadata is preserved ([Supabase metadata updates](https://supabase.com/docs/reference/javascript/auth-updateuser)). Completion persists with the account across sessions/devices; failed saves allow retry, and session changes discard pending UI actions. Existing custom-base users and admin are not interrupted by the empty-workspace guide.

This follow-up needs no new environment values or SQL migration beyond the original workspace migration. Deploy both projects. Live Supabase metadata persistence still needs verification; automated tests use mocked providers.

## Verification

`tests/test_workspace_isolation.py` tests local mode and cloud mode (mocked Supabase REST/auth, real temporary PDF parsing and Qdrant): legacy admin data, empty new accounts, direct foreign URLs, metadata/preview/delete/search/ask boundaries, identical PDFs, concurrent accounts, duplicate base names, invalid identities and missing-migration failure. These are offline tests, not a live Supabase migration or production isolation audit.

The existing Vercel admin SQLite refresh-session persistence limitation is unchanged. OCR without a key is still deferred; this feature does not install an OCR engine.


## Processed document editor

Document **View / edit chunks** opens the actual indexed text, rather than the original PDF preview. Authenticated GET/PUT `/api/domains/{key}/documents/{hash}/chunks` use the same owner-qualified base/namespace as search and deletion. Existing processed uploads can be opened without reuploading. Incomplete cloud uploads must finish processing first.

Save normalizes Unicode/whitespace, removes exact normalized chunk duplicates, splits oversized chunks and removes near-duplicate chunks using the configured embedding threshold. It does not send manual edits through the LLM, which could rewrite user changes. Source filename and original-page references remain attached; new content must be assigned to an original page. Page references identify the source location, not a claim that edited text still appears verbatim in the original PDF. The original upload is unchanged. Force reprocessing the original upload explicitly replaces edited chunks with content extracted from the original.

Edited text persists in Qdrant payloads alongside its embeddings. Save embeds before modifying stored points, replaces only the selected file's points, updates its chunk count and clears local stale answers. Revision checking rejects stale editor saves. Cloud saves reuse the existing five-minute document lease; expired saves are rejected before writes. Failures attempt to restore the previous points; Supabase metadata and Qdrant are separate systems, so this is not a distributed transaction and a process crash/network outage during replacement still requires operational recovery. Unsaved browser drafts remain on normal save errors; navigating away, logging out or force reprocessing can discard drafts.

No new env settings or SQL migrations are introduced by the editor. The earlier workspace isolation migration is still required. Deploy frontend/backend together. Live embedding-provider, hosted Qdrant/Supabase and serverless request-duration limits must be verified for the deployment; saves are synchronous and very large edits may exceed hosting time limits.
