begin;

create table if not exists public.qa_role_settings (
  role text primary key check (role = 'admin'),
  active boolean not null default true
);

alter table public.qa_role_settings enable row level security;
revoke all on public.qa_role_settings from public, anon, authenticated;
grant select on public.qa_role_settings to service_role;

create or replace function public.qa_set_admin_role_active(p_active boolean)
returns void language plpgsql security definer set search_path = '' as $$
begin
  insert into public.qa_role_settings(role, active) values ('admin', p_active)
  on conflict (role) do update set active = excluded.active;
  insert into public.qa_admin_audit(action) values (
    case when p_active then 'Reactivated Admin role' else 'Deactivated Admin role' end
  );
end;
$$;

revoke all on function public.qa_set_admin_role_active(boolean) from public, anon, authenticated;
grant execute on function public.qa_set_admin_role_active(boolean) to service_role;

commit;
