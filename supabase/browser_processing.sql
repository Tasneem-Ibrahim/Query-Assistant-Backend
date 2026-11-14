-- Additive transition: preserves every file, request, decision, attempt and cursor.
-- Run after upload_jobs.sql. No Cron/pg_net/Vault, DROP, or bulk state reset.
begin;
create or replace function public.qa_begin_browser_upload(p_id uuid,p_owner uuid,p_token uuid)
returns setof public.qa_upload_requests language plpgsql security definer set search_path='' as $$
declare r public.qa_upload_requests;
begin
  select * into r from public.qa_upload_requests where id=p_id and user_id=p_owner
    and status='approved' and file_ready and object_path is not null
    limit 1 for update skip locked;
  if not found then return; end if;
  if r.job_state in ('ready','failed') then return next r; return; end if;
  if r.lease_until is not null and r.lease_until>now() then return; end if;
  if r.lease_token is not null and r.attempts>=2 then
    update public.qa_upload_requests set job_state='failed',lease_token=null,lease_until=null,
      job_message='Processing stopped after the single retry timed out. Contact the administrator.',updated_at=now()
      where id=r.id returning * into r;
    return next r; return;
  end if;
  update public.qa_upload_requests set job_state='processing',
    attempts=case when r.job_state in ('none','queued','retrying') or r.lease_token is not null
      then least(2,r.attempts+1) else greatest(1,r.attempts) end,
    lease_token=p_token,lease_until=now()+interval '15 minutes',updated_at=now(),
    job_message='Your approved file is processing on your Dashboard.'
    where id=r.id returning * into r;
  return next r;
end $$;

create or replace function public.qa_end_browser_upload(p_id uuid,p_owner uuid,p_token uuid,p_result text)
returns setof public.qa_upload_requests language plpgsql security definer set search_path='' as $$
begin
  if p_result not in ('ready','continue','error') then raise exception 'Invalid result'; end if;
  return query update public.qa_upload_requests set
    job_state=case when p_result='ready' then 'ready' when p_result='continue' then 'processing'
      when attempts<2 then 'retrying' else 'failed' end,
    job_message=case when p_result='ready' then 'Your document is ready. You can now ask questions and search it.'
      when p_result='continue' then 'Your approved file is processing on your Dashboard.'
      when attempts<2 then 'Processing failed. Retrying once from saved progress; no re-upload is needed.'
      else 'Processing failed again. The single retry is exhausted. Contact the administrator.' end,
    lease_token=null,lease_until=null,updated_at=now()
    where id=p_id and user_id=p_owner and lease_token=p_token and lease_until>now() and status='approved'
    returning *;
end $$;
revoke all on function public.qa_begin_browser_upload(uuid,uuid,uuid),public.qa_end_browser_upload(uuid,uuid,uuid,text)
  from public,anon,authenticated;
grant execute on function public.qa_begin_browser_upload(uuid,uuid,uuid),public.qa_end_browser_upload(uuid,uuid,uuid,text)
  to service_role;
commit;
notify pgrst, 'reload schema';
