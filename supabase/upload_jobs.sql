-- Historical filename: saved-upload schema. Run after admin_panel.sql, then browser_processing.sql.
-- No scheduler, extensions, Vault or worker is required.
begin;
alter table public.qa_upload_requests drop constraint if exists qa_upload_requests_status_check;
alter table public.qa_upload_requests add constraint qa_upload_requests_status_check
  check (status in ('pending','approved','rejected','expired'));
alter table public.qa_upload_requests
  add column if not exists user_deleted_at timestamptz,
  add column if not exists pages integer check(pages between 1 and 500),
  add column if not exists object_path text,
  add column if not exists file_ready boolean not null default false,
  add column if not exists use_llm boolean not null default true,
  add column if not exists job_state text not null default 'none'
    check (job_state in ('none','queued','processing','retrying','ready','failed')),
  add column if not exists attempts integer not null default 0 check(attempts between 0 and 2),
  add column if not exists job_message text not null default '',
  add column if not exists lease_token uuid,
  add column if not exists lease_until timestamptz,
  add column if not exists updated_at timestamptz not null default now();

insert into storage.buckets(id,name,public,file_size_limit)
values ('qa-pending-uploads','qa-pending-uploads',false,4000000)
on conflict(id) do update set public=false,file_size_limit=4000000;
-- No browser object policies: backend service role only.

create or replace function public.qa_decide_saved_upload(p_id uuid,p_status text,p_reason text)
returns setof public.qa_upload_requests language plpgsql security definer set search_path='' as $$
begin
  if p_status not in ('approved','rejected') then raise exception 'Invalid decision'; end if;
  return query update public.qa_upload_requests set status=p_status,reason=p_reason,decided_at=now(),updated_at=now(),
    job_state=case when p_status='approved' and file_ready then 'queued' else job_state end,
    job_message=case when p_status='approved' and file_ready then 'Your upload was approved. Processing starts when your Dashboard is open.'
      when p_status='rejected' then 'Your upload was rejected. See the administrator reason.' else job_message end
    where id=p_id and status='pending' and (object_path is null or file_ready)
      and created_at>now()-interval '30 days'
    returning *;
end $$;

create or replace function public.qa_expire_uploads()
returns integer language plpgsql security definer set search_path='' as $$
declare n integer;
begin
  update public.qa_upload_requests set status='expired',job_message='This pending or rejected upload was removed after 30 days.',updated_at=now()
    where (status='pending' and created_at<=now()-interval '30 days')
      or (status='rejected' and decided_at<=now()-interval '30 days');
  get diagnostics n=row_count;
  return n;
end $$;
revoke all on function public.qa_decide_saved_upload(uuid,text,text),public.qa_expire_uploads() from public,anon,authenticated;
grant execute on function public.qa_decide_saved_upload(uuid,text,text),public.qa_expire_uploads() to service_role;
commit;
notify pgrst, 'reload schema';
