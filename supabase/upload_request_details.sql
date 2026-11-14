-- Existing installations that already applied upload_jobs.sql: run this once.
-- New uploads report server-verified page count; historical rows remain unknown.
begin;
alter table public.qa_upload_requests
  add column if not exists pages integer check(pages between 1 and 500);
commit;
notify pgrst, 'reload schema';
