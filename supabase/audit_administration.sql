-- Apply after scoped_authority.sql. Existing audit and revision rows are immutable.
begin;

create table if not exists public.qa_audit_journal (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default clock_timestamp(),
    resource text not null check (resource in ('logs', 'revisions')),
    operation text not null check (operation in ('add', 'correct', 'archive', 'restore')),
    reference text,
    actor_id text not null,
    subject text,
    note text not null check (char_length(btrim(note)) between 1 and 2000),
    check ((operation = 'add') = (reference is null)),
    check (operation <> 'correct' or resource = 'logs')
);
create index if not exists qa_audit_journal_reference
  on public.qa_audit_journal(resource, reference, id desc);
alter table public.qa_audit_journal enable row level security;
revoke all on public.qa_audit_journal from public, anon, authenticated, service_role;
grant select on public.qa_audit_journal to service_role;

create or replace function public.qa_audit_annotations(p_resource text, p_refs text[])
returns table(reference text, operation text, note text, actor_id text, created_at timestamptz)
language sql stable set search_path = '' as $$
  select distinct on (j.reference, j.operation = 'correct')
    j.reference, j.operation, j.note, j.actor_id, j.created_at
  from public.qa_audit_journal j
  where j.resource = p_resource and j.reference = any(p_refs)
  order by j.reference, j.operation = 'correct', j.id desc;
$$;

create or replace function public.qa_append_audit_operation(
    p_resource text, p_operation text, p_reference text, p_subject text, p_note text
) returns bigint language plpgsql security definer set search_path = '' as $$
declare
  headers jsonb := coalesce(nullif(current_setting('request.headers', true), ''), '{}')::jsonb;
  snapshot jsonb := (headers->>'x-qa-delegation')::jsonb;
  actor text := headers->>'x-qa-revision-actor';
  actor_access jsonb;
  actual_subject text;
  owner text;
  owner_role text;
  previous_operation text;
  key_name text;
  entry record;
  event_id bigint;
begin
  if actor is null then
    raise insufficient_privilege using message = 'Audit actor is required.';
  end if;
  perform pg_advisory_xact_lock(114601611, 78190);

  if p_operation = 'add' then
    actual_subject := p_subject;
  elsif p_reference ~ '^manual:[1-9][0-9]{0,17}$' then
    select subject into actual_subject from public.qa_audit_journal
      where resource = p_resource and operation = 'add' and id = split_part(p_reference, ':', 2)::bigint;
    if not found then raise sqlstate 'PT404' using message = 'Audit record not found.'; end if;
  elsif p_resource = 'logs' and p_reference ~ '^login:[1-9][0-9]{0,17}$' then
    select account_id into actual_subject from public.qa_login_events where id = split_part(p_reference, ':', 2)::bigint;
    if not found then raise sqlstate 'PT404' using message = 'Audit record not found.'; end if;
  elsif p_resource = 'logs' and p_reference ~ '^activity:(user-details|knowledge-base|role-management):[1-9][0-9]{0,17}$' then
    select target into actual_subject from public.qa_activity_events
      where category = split_part(p_reference, ':', 2) and id = split_part(p_reference, ':', 3)::bigint;
    if not found then raise sqlstate 'PT404' using message = 'Audit record not found.'; end if;
  elsif p_resource = 'revisions' and p_reference ~ '^revision:C:[1-9][0-9]{0,17}$' then
    select target into actual_subject from public.qa_setting_revisions where id = split_part(p_reference, ':', 3)::bigint;
    if not found then raise sqlstate 'PT404' using message = 'Audit record not found.'; end if;
  else
    raise sqlstate 'PT404' using message = 'Audit record not found.';
  end if;

  if snapshot is null then
    if actor <> '00000000-0000-4000-8000-000000000001' then
      raise insufficient_privilege using message = 'Audit authority is required.';
    end if;
  else
    if snapshot ? 'account' then
      select coalesce(raw_app_meta_data->'qa_access', '{}'::jsonb) into actor_access
        from auth.users where id::text = actor;
      if not found or actor_access is distinct from snapshot->'account'
          or (actor_access->>'role' = 'admin'
              and exists (select 1 from public.qa_role_settings where role = 'admin' and not active)) then
        raise sqlstate 'PT409' using message = 'Account authority changed. Refresh permissions.';
      end if;
    end if;
    for entry in select * from jsonb_each_text(coalesce(snapshot->'state', '{}'::jsonb)) loop
      if coalesce((select revision from public.qa_security_state where key = entry.key), 0) <> entry.value::bigint then
        raise sqlstate 'PT409' using message = 'Permission settings changed. Refresh permissions.';
      end if;
    end loop;
    key_name := p_resource || '.' || case p_operation when 'add' then 'add' when 'correct' then 'edit' else 'delete' end;
    owner := split_part(actual_subject, ' / ', 1);
    select case when raw_app_meta_data->'qa_access'->>'role' = 'admin' then 'admin' else 'user' end
      into owner_role from auth.users where id::text = owner;
    if not found or owner in (actor, '00000000-0000-4000-8000-000000000001')
       or not ((snapshot->'allowed') ?& array[p_resource || '.view', key_name])
       or not ((snapshot->'scope') ? owner or (snapshot->'scope') ? ('role:' || owner_role)) then
      raise insufficient_privilege using message = 'Audit record outside delegated authority.';
    end if;
  end if;
  if p_operation in ('archive', 'restore') then
    select operation into previous_operation from public.qa_audit_journal
      where resource = p_resource and reference = p_reference and operation in ('archive', 'restore')
      order by id desc limit 1;
    if (p_operation = 'archive') = coalesce(previous_operation = 'archive', false) then
      return null;
    end if;
  end if;
  insert into public.qa_audit_journal(resource,operation,reference,actor_id,subject,note)
    values(p_resource,p_operation,p_reference,actor,actual_subject,btrim(p_note)) returning id into event_id;
  insert into public.qa_activity_events(category, actor_id, target, action)
    values('role-management', actor, actual_subject,
           'Audit ' || p_resource || ': ' || p_operation || ' (journal #' || event_id || ')');
  return event_id;
end;
$$;
revoke all on function public.qa_audit_annotations(text,text[]),
  public.qa_append_audit_operation(text,text,text,text,text) from public, anon, authenticated;
grant execute on function public.qa_audit_annotations(text,text[]),
  public.qa_append_audit_operation(text,text,text,text,text) to service_role;

create or replace function public.qa_check_delegation_snapshot()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  headers jsonb := coalesce(nullif(current_setting('request.headers', true), ''), '{}')::jsonb;
  snapshot jsonb := (headers->>'x-qa-delegation')::jsonb;
  actor text := headers->>'x-qa-revision-actor';
  entry record;
  actor_access jsonb;
  scope jsonb;
  allowed jsonb;
  old_access jsonb;
  new_access jsonb;
  old_permissions jsonb;
  new_permissions jsonb;
  target_role text;
  old_actions jsonb;
  new_actions jsonb;
  old_modules jsonb;
  new_modules jsonb;
  key_name text;
begin
  if tg_table_schema = 'auth' and tg_op = 'UPDATE' then
    if new.raw_app_meta_data->'qa_access' is not distinct from old.raw_app_meta_data->'qa_access' then
      return new;
    end if;
  elsif tg_table_name = 'qa_security_state' then
    if new.key not in ('policy:admin', 'policy:user', 'account:local-admin', 'status:local-admin') then
      return new;
    end if;
  end if;

  perform pg_advisory_xact_lock(114601611, 78190);
  if snapshot is null then return new; end if;

  if snapshot ? 'account' then
    select coalesce(raw_app_meta_data->'qa_access', '{}'::jsonb) into actor_access
      from auth.users where id = actor::uuid;
    if not found or actor_access is distinct from snapshot->'account'
       or (actor_access->>'role' = 'admin'
           and exists (select 1 from public.qa_role_settings where role = 'admin' and not active)) then
      raise sqlstate 'PT409' using message = 'Account authority changed. Refresh and try again.';
    end if;
  end if;
  for entry in select * from jsonb_each_text(coalesce(snapshot->'state', '{}'::jsonb)) loop
    if coalesce((select revision from public.qa_security_state where key = entry.key), 0) <> entry.value::bigint then
      raise sqlstate 'PT409' using message = 'Permission settings changed. Refresh and try again.';
    end if;
  end loop;
  scope := coalesce(snapshot->'scope', '[]'::jsonb);
  allowed := coalesce(snapshot->'allowed', '[]'::jsonb);
  if tg_table_schema = 'auth' then
    old_access := coalesce(old.raw_app_meta_data->'qa_access', '{}'::jsonb);
    new_access := coalesce(new.raw_app_meta_data->'qa_access', '{}'::jsonb);
    old_permissions := coalesce(old_access->'permissions', '{}'::jsonb);
    new_permissions := coalesce(new_access->'permissions', '{}'::jsonb);
    target_role := case when old_access->>'role' = 'admin' then 'admin' else 'user' end;
    if new.id::text in (actor, '00000000-0000-4000-8000-000000000001') then
      raise insufficient_privilege using message = 'Protected account.';
    end if;
    -- An invitation initializes a fresh account with bounded role defaults.
    if old_access = '{}'::jsonb and new_access->>'invited' = 'true'
       and new_permissions = '{}'::jsonb
       and (allowed ?& array['roles.view','roles.add']
            or (new_access->>'role' = 'user' and scope ? 'role:user'
                and allowed ?& array['accounts.view','accounts.add']))
       and (new_access->>'role' = 'user' or
            (new_access->>'role' = 'admin' and scope ? 'role:admin' and allowed ? 'roles.edit')) then
      return new;
    end if;
    if not (scope ? new.id::text or scope ? ('role:' || target_role)) then
      raise insufficient_privilege using message = 'Account outside delegated scope.';
    end if;
    if new_permissions <> old_permissions then
      if not allowed ? 'permissions.view' then
        raise insufficient_privilege using message = 'Permission management authority required.';
      end if;
      for key_name in select jsonb_object_keys(old_permissions || new_permissions) loop
        if old_permissions->key_name is distinct from new_permissions->key_name then
          if key_name like 'module:%' then
            if not allowed ? 'permissions.edit'
               or not exists (select 1 from jsonb_array_elements_text(allowed) as a(key)
                           where split_part(a.key, '.', 1) = substr(key_name, 8)) then
              raise insufficient_privilege using message = 'Module outside delegated scope.';
            end if;
          elsif key_name like 'scope:%' or not (allowed ? key_name)
             or not (allowed ? case when not (old_permissions ? key_name) then 'permissions.add'
                                    when not (new_permissions ? key_name) then 'permissions.delete'
                                    else 'permissions.edit' end) then
            raise insufficient_privilege using message = 'Action outside delegated scope.';
          end if;
        end if;
      end loop;
    end if;
    if coalesce(new_access->>'role', 'user') <> target_role and not (
       allowed ? 'roles.view' and (allowed ? 'roles.edit'
       or (new_access->>'role' = 'user' and allowed ? 'roles.delete'))) then
      raise insufficient_privilege using message = 'Role assignment authority required.';
    end if;
    if coalesce(new_access->>'status', 'active') <> coalesce(old_access->>'status', 'active')
       and not (allowed ? 'accounts.view' and case when new_access->>'status' = 'deleting'
           then allowed ? 'accounts.delete' else allowed ? 'accounts.edit' end) then
      raise insufficient_privilege using message = 'Account status authority required.';
    end if;
  elsif tg_table_name = 'qa_security_state' then
    if not (scope ? new.key and allowed ? 'permissions.view'
            and allowed ?| array['permissions.add','permissions.edit','permissions.delete'])
       or new.key not in ('policy:admin','policy:user')
       or new.key = ('policy:' || case when actor = 'local-admin' or actor_access->>'role' = 'admin'
                                       then 'admin' else 'user' end) then
      raise insufficient_privilege using message = 'Role policy outside delegated scope.';
    end if;
    old_actions := case when tg_op = 'INSERT' then '{}'::jsonb else coalesce(old.value->'actions', '{}'::jsonb) end;
    new_actions := coalesce(new.value->'actions', '{}'::jsonb);
    old_modules := case when tg_op = 'INSERT' then '{}'::jsonb else coalesce(old.value->'modules', '{}'::jsonb) end;
    new_modules := coalesce(new.value->'modules', '{}'::jsonb);
    for key_name in select jsonb_object_keys(old_actions || new_actions) loop
      if old_actions->key_name is distinct from new_actions->key_name
         and (split_part(key_name, '.', 1) not in ('bases','documents') or not (allowed ? key_name)
              or not (allowed ? case when not (old_actions ? key_name) then 'permissions.add'
                                    when not (new_actions ? key_name) then 'permissions.delete'
                                    else 'permissions.edit' end)) then
        raise insufficient_privilege using message = 'Role action outside delegated scope.';
      end if;
    end loop;
    for key_name in select jsonb_object_keys(old_modules || new_modules) loop
      if old_modules->key_name is distinct from new_modules->key_name and (
         key_name not in ('bases','documents') or not (allowed ?& array['permissions.edit',
            key_name || '.view', key_name || '.add', key_name || '.edit', key_name || '.delete'])) then
        raise insufficient_privilege using message = 'Role module outside delegated scope.';
      end if;
    end loop;
  elsif tg_table_name = 'qa_role_settings' then
    raise insufficient_privilege using message = 'Global role status is protected.';
  end if;
  return new;
end;
$$;



notify pgrst, 'reload schema';
commit;
