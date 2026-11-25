-- Apply before deploying mandatory 2FA to the Supabase-backed app.
create table if not exists public.qa_security_state (
  key text primary key,
  value jsonb not null,
  revision bigint not null
);
create table if not exists public.qa_login_events (
  id bigint generated always as identity primary key,
  created_at timestamptz not null,
  account_id text,
  role text,
  outcome text not null
);
create table if not exists public.qa_admin_sessions (
  id text primary key,
  refresh_hash text not null unique,
  expires bigint not null
);
create index if not exists qa_admin_sessions_expiry on public.qa_admin_sessions(expires);

alter table public.qa_security_state enable row level security;
alter table public.qa_login_events enable row level security;
alter table public.qa_admin_sessions enable row level security;
revoke all on public.qa_security_state, public.qa_login_events, public.qa_admin_sessions from public, anon, authenticated;
grant select, delete on public.qa_security_state to service_role;
grant select, insert on public.qa_login_events to service_role;
grant select, insert, update, delete on public.qa_admin_sessions to service_role;
grant usage on sequence public.qa_login_events_id_seq to service_role;

create or replace function public.qa_write_security_state(p_key text, p_value jsonb, p_revision bigint)
returns boolean language plpgsql security definer set search_path = '' as $$
begin
  if p_revision = 0 then
    insert into public.qa_security_state (key, value, revision) values (p_key, p_value, 1)
      on conflict (key) do nothing;
  else
    update public.qa_security_state set value = p_value, revision = revision + 1
      where key = p_key and revision = p_revision;
  end if;
  return found;
end;
$$;
revoke all on function public.qa_write_security_state(text, jsonb, bigint) from public, anon, authenticated;
grant execute on function public.qa_write_security_state(text, jsonb, bigint) to service_role;
