begin;

create table if not exists public.qa_admin_audit (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  action text not null,
  target uuid
);

alter table public.qa_admin_audit enable row level security;
revoke all on public.qa_admin_audit from public, anon, authenticated;
grant select, insert on public.qa_admin_audit to service_role;
grant usage on sequence public.qa_admin_audit_id_seq to service_role;

-- Compare and swap prevents a stale role or activation change from reopening
-- an account after deletion has started. Keep the audit write atomic with it.
create or replace function public.qa_update_account_access(
  p_user uuid, p_previous jsonb, p_next jsonb, p_action text
) returns boolean language plpgsql security definer set search_path = '' as $$
begin
  update auth.users
  set raw_app_meta_data = jsonb_set(coalesce(raw_app_meta_data, '{}'::jsonb), '{qa_access}', p_next),
      updated_at = now()
  where id = p_user
    and coalesce(raw_app_meta_data->'qa_access', '{}'::jsonb) = p_previous;
  if not found then
    return false;
  end if;
  insert into public.qa_admin_audit(action, target) values (p_action, p_user);
  return true;
end;
$$;
revoke all on function public.qa_update_account_access(uuid, jsonb, jsonb, text) from public, anon, authenticated;
grant execute on function public.qa_update_account_access(uuid, jsonb, jsonb, text) to service_role;

-- List storage through PostgreSQL so retries can remove orphaned objects even
-- when an earlier operation did not finish writing its application metadata.
create or replace function public.qa_account_storage_objects(
  p_user uuid, p_document_bucket text, p_upload_bucket text
) returns table(bucket text, name text) language sql stable security definer set search_path = '' as $$
  select o.bucket_id::text, o.name::text
  from storage.objects as o
  where (o.bucket_id = p_upload_bucket and split_part(o.name, '/', 1) = p_user::text)
     or (o.bucket_id = p_document_bucket and split_part(o.name, '/', 1)
          ~ ('^api_[a-f0-9]{16}_user_' || replace(p_user::text, '-', '') || '$'))
  order by o.bucket_id, o.name;
$$;
revoke all on function public.qa_account_storage_objects(uuid, text, text) from public, anon, authenticated;
grant execute on function public.qa_account_storage_objects(uuid, text, text) to service_role;

commit;
