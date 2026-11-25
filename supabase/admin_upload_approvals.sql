-- Run after upload_jobs.sql and browser_processing.sql.
alter table public.qa_upload_requests
  add column if not exists admin_deleted_at timestamptz;
