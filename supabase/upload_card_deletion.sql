-- Soft-removes only the user's request card. Admin history/PDF/job stay saved.
begin;
alter table public.qa_upload_requests add column if not exists user_deleted_at timestamptz;
commit;
notify pgrst, 'reload schema';
