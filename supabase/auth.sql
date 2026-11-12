-- Run in the Supabase SQL Editor. Password hashes and sessions belong to Auth,
-- never to this public profile table. Safe to run again.
begin;
create table if not exists public.users (
  id uuid primary key references auth.users(id) on delete cascade,
  name text not null check (length(trim(name)) between 1 and 80),
  email text not null,
  created_at timestamptz not null default now()
);
alter table public.users enable row level security;
revoke all on public.users from anon, authenticated;
grant select on public.users to authenticated;
grant all on public.users to service_role;
drop policy if exists users_read_own_profile on public.users;
create policy users_read_own_profile on public.users for select to authenticated
  using ((select auth.uid()) = id);

create or replace function public.qa_sync_user_profile()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.users(id, name, email, created_at)
  values (new.id, left(coalesce(nullif(trim(new.raw_user_meta_data->>'name'), ''),
    split_part(new.email, '@', 1), 'User'), 80), coalesce(new.email, ''), new.created_at)
  on conflict (id) do update set name = excluded.name, email = excluded.email;
  return new;
end;
$$;
revoke all on function public.qa_sync_user_profile() from public, anon, authenticated;
drop trigger if exists qa_auth_user_profile on auth.users;
create trigger qa_auth_user_profile after insert or update of email, raw_user_meta_data
  on auth.users for each row execute function public.qa_sync_user_profile();

insert into public.users(id, name, email, created_at)
select id, left(coalesce(nullif(trim(raw_user_meta_data->>'name'), ''),
  split_part(email, '@', 1), 'User'), 80), coalesce(email, ''), created_at from auth.users
on conflict (id) do nothing;
commit;
