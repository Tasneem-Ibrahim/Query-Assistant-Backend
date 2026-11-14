# Saved uploads and browser-driven processing

No scheduled worker, Supabase Cron, pg_net, Vault or worker secret is required.
The same repositories, deployments, authenticated sessions and RAG pipeline are used.
This describes local implementation, not live database/deployment verification.

## Upload, approval and resume

1. The User saves a PDF/image once through POST /api/uploads. The backend checks
   ownership, upload policy, size/type/hash and PDF metadata, and saves private bytes
   in qa-pending-uploads. Nothing is OCRed, cleaned or embedded before approval.
2. Admin approves/rejects with an optional reason. Allowed accounts skip approval;
   blocked accounts cannot save/process. Approval does not start a server worker.
3. Opening the User Dashboard loads approved saved uploads and starts/resumes them
   through the existing /api/domains/{key}/documents/{hash}/step endpoint. The browser
   uses runJobs(), sequentially requesting pages. Homepage/Admin pages do not process
   user files. The server verifies the current user, approval and policy on every step.
4. Cloud mode reuses cloud.start()/cloud.step(): existing OCR, cleaning, chunking,
   duplicate filtering, embeddings, Qdrant and saved page cursors are unchanged.
   Local mode runs the same existing local ingest function in the active request.
5. Closing the Dashboard stops further browser requests. An already-running request
   can finish; its progress is saved. Reopening the Dashboard resumes without another
   upload. Interrupted requests/multiple tabs respect database leases; an abandoned
   invocation can wait up to 15 minutes for its lease to expire.
6. Page continuations do not spend a retry. One failed attempt permits one retry;
   the second failure is terminal. Existing attempt counts, failures and cursors are
   preserved across migration/reload. Client parameters cannot reset this budget.
   A timed-out attempt uses the same budget after lease expiry. Network transfer of
   the original file independently retries once on 5xx/network failure.

Dashboard UI, approval/status cards, optional notes, PDF preview, chunk editing,
card soft deletion, account roles and data isolation remain intact. Cards hidden by
users remain hidden, but their approved saved files can still resume on Dashboard
access. Metadata-only historical requests still need their original bytes saved once.

## Retention without scheduling

Pending files remain eligible for removal 30 days after upload; rejected files
30 days after rejection. Cleanup runs in bounded batches during authenticated Admin
user/request listing, not on a schedule. If the app is unused, cleanup waits until
Admin returns. Expired history remains; raw bytes are deleted idempotently. Approved
files are excluded. A failed storage cleanup keeps its pointer for a later attempt.

## Additive rollout to the existing deployment

- If not already applied, run auth.sql, workspace_isolation.sql and admin_panel.sql
  after the existing base setup.sql tables. Run the latest upload_jobs.sql for the
  saved-upload fields/private bucket/approval and expiry RPCs. Its historical name
  is retained for compatibility; it contains no scheduled worker functions.
- Run **supabase/browser_processing.sql** before deploying this change. It creates
  service-role-only, owner-bound request leases/finish RPCs. It does not drop tables,
  files, data, columns or reset attempts. Existing uploads and decisions remain saved.
- Already migrated installations can use upload_request_details.sql and
  upload_card_deletion.sql if their pages/user_deleted_at columns are missing.
- Deploy frontend/backend together to the existing projects; restart local services.
  No new environment setting or paid service is introduced. Existing Supabase,
  OCR, embedding, Qdrant, LLM and authentication settings remain necessary.

## Retiring a previously configured scheduler (manual, project-specific)

No destructive database cleanup is run automatically by the agent or the migration.
The reported scheduler creation failed because cron did not exist; successful live
activation has not been verified. If no job/secrets were created, skip their removal.

If they were created later, deactivate only the qa-upload-worker job in Supabase
before rollout. Remove only its unused qa_upload_worker_url/qa_upload_worker_secret
Vault entries and UPLOAD_WORKER_SECRET from the existing backend deployment settings.
Local env files/examples and Python config no longer contain that key. Do not disable
extensions globally or delete Vault entries used by other applications.

The old internal endpoint, middleware bypass, worker script and scheduler SQL file
have been removed from application source. Old qa_claim_upload_job and
qa_finish_upload_step database functions may remain inert if previously applied;
no current code calls them. Their optional removal can be reviewed separately.
Keep qa_decide_saved_upload/qa_expire_uploads and the original qa_documents page
lease/advance/release RPCs: these support approval, retention and resumable RAG.

## Verification and limits

Verify two real users plus Admin after migration/deployment: upload once, no work
before approval, approve while User is offline, open Dashboard, close mid-PDF, reopen
and resume, Ready document/PDF/search, images/OCR, single retry exhaustion, blocked
and rejected access, hidden cards, and cross-user boundaries.

Offline tests emulate Supabase RPCs while using real temporary PDFs/images/Qdrant
where applicable. Browser tests mock the API. They do not verify live SQL locking,
providers or deployed runtime settings. Existing Vercel duration/payload limits
remain; one slow OCR page or initial PDF preparation can still time out. There is
no guarantee of completing an entire file while the User Dashboard is closed.
Existing OCR configuration and serverless admin-session storage limitations remain.
