-- Apply after hierarchical_permissions.sql. Existing grants and revision rows are retained.
begin;

revoke insert, update, delete, truncate on public.qa_role_settings from service_role;

alter table public.qa_setting_revisions drop constraint if exists qa_setting_revisions_kind_check;
alter table public.qa_setting_revisions add constraint qa_setting_revisions_kind_check
  check (kind in ('security', 'account', 'account-created', 'admin-role', 'upload-policy'));

create or replace function public.qa_capture_setting_revision()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  headers jsonb := coalesce(nullif(current_setting('request.headers', true), ''), '{}')::jsonb;
  actor text := coalesce(nullif(current_setting('qa.revision_actor', true), ''), headers->>'x-qa-revision-actor');
  reversed bigint := nullif(current_setting('qa.reversed_revision', true), '')::bigint;
  before_value jsonb;
  after_value jsonb;
  target_key text;
  kind_key text := tg_argv[0];
  category text := 'role-management';
  new_id bigint;
begin
  if kind_key = 'account-created' then
    actor := new.raw_app_meta_data->'qa_access'->>'created_by';
  end if;
  if actor is null or actor = '' then return new; end if;
  case kind_key
    when 'security' then
      target_key := new.key;
      if target_key not in ('policy:admin', 'policy:user', 'account:local-admin', 'status:local-admin') then return new; end if;
      before_value := public.qa_revision_security_state(target_key, case when tg_op = 'INSERT' then '{}'::jsonb else old.value end);
      after_value := public.qa_revision_security_state(target_key, new.value);
      if target_key = 'status:local-admin' then category := 'user-details'; end if;
    when 'account-created' then
      target_key := new.id::text;
      before_value := '{}'::jsonb;
      after_value := public.qa_revision_account_state(new.raw_app_meta_data->'qa_access');
    when 'account' then
      target_key := new.id::text;
      before_value := public.qa_revision_account_state(old.raw_app_meta_data->'qa_access');
      after_value := public.qa_revision_account_state(new.raw_app_meta_data->'qa_access');
      if after_value->>'status' = 'deleting' then category := 'user-details'; end if;
      if coalesce(new.raw_app_meta_data->'qa_access'->>'invited', 'false') = 'true'
         and old.raw_app_meta_data->'qa_access' is null then
        kind_key := 'account-created';
        before_value := '{}'::jsonb;
      end if;
      if before_value - 'status' = after_value - 'status' then category := 'user-details'; end if;
    when 'admin-role' then
      target_key := new.role;
      before_value := jsonb_build_object('active', case when tg_op = 'INSERT' then true else old.active end);
      after_value := jsonb_build_object('active', new.active);
    when 'upload-policy' then
      target_key := new.user_id::text;
      before_value := jsonb_build_object('policy', case when tg_op = 'INSERT' then 'approval_required' else old.policy end);
      after_value := jsonb_build_object('policy', new.policy);
      category := 'user-details';
  end case;
  if before_value = after_value then return new; end if;
  insert into public.qa_setting_revisions(kind, target, actor_id, before_state, after_state, reversed_id)
    values (kind_key, target_key, actor, before_value, after_value, reversed) returning id into new_id;
  if reversed is not null or (kind_key = 'security' and target_key like 'policy:%')
      or (kind_key = 'security' and target_key = 'account:local-admin')
      or kind_key in ('account', 'account-created', 'admin-role') then
    insert into public.qa_activity_events(category, actor_id, target, action)
      values (category, actor, target_key,
              case when reversed is not null then 'Reversed RV-C' || lpad(reversed::text, greatest(6, length(reversed::text)), '0')
                when kind_key = 'account-created' then 'Assigned initial account role and permissions'
                when kind_key = 'admin-role' then 'Updated Admin role status'
                when kind_key = 'account' and before_value->>'role' <> after_value->>'role' then 'Changed account role'
                when kind_key = 'account' and before_value->>'status' <> after_value->>'status' then 'Changed account status'
                else 'Updated permissions' end);
    perform set_config('qa.new_revision', new_id::text, true);
    perform set_config('qa.new_revision_category', category, true);
  end if;
  return new;
end;
$$;


drop trigger if exists qa_revision_account_creation on auth.users;
create trigger qa_revision_account_creation after insert on auth.users
  for each row execute function public.qa_capture_setting_revision('account-created');

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
       and new_permissions = '{}'::jsonb and allowed ?& array['roles.view','roles.add']
       and (new_access->>'role' = 'user' or
            (new_access->>'role' = 'admin' and scope ? 'role:admin' and allowed ? 'roles.edit')) then
      return new;
    end if;
    if not (scope ? new.id::text or scope ? ('role:' || target_role)) then
      raise insufficient_privilege using message = 'Account outside delegated scope.';
    end if;
    if new_permissions <> old_permissions then
      if not allowed ?& array['permissions.view','permissions.edit'] then
        raise insufficient_privilege using message = 'Permission management authority required.';
      end if;
      for key_name in select jsonb_object_keys(old_permissions || new_permissions) loop
        if old_permissions->key_name is distinct from new_permissions->key_name then
          if key_name like 'module:%' then
            if not exists (select 1 from jsonb_array_elements_text(allowed) as a(key)
                           where split_part(a.key, '.', 1) = substr(key_name, 8)) then
              raise insufficient_privilege using message = 'Module outside delegated scope.';
            end if;
          elsif key_name like 'scope:%' or not (allowed ? key_name) then
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
    if not (scope ? new.key and allowed ?& array['permissions.view','permissions.edit'])
       or new.key not in ('policy:admin','policy:user')
       or new.key = ('policy:' || case when actor = 'local-admin' or actor_access->>'role' = 'admin'
                                       then 'admin' else 'user' end) then
      raise insufficient_privilege using message = 'Role policy outside delegated scope.';
    end if;
  elsif tg_table_name = 'qa_role_settings' then
    raise insufficient_privilege using message = 'Global role status is protected.';
  end if;
  return new;
end;
$$;


create or replace function public.qa_write_delegated_policy(p_key text, p_value jsonb, p_revision bigint)
returns boolean language sql set search_path = '' as $$
  select public.qa_write_permission_state(p_key, p_value, p_revision);
$$;
create or replace function public.qa_update_delegated_account(p_user uuid, p_previous jsonb, p_next jsonb, p_action text)
returns boolean language sql set search_path = '' as $$
  select public.qa_update_permission_account(p_user, p_previous, p_next, p_action);
$$;
create or replace function public.qa_reverse_delegated_change(p_id bigint, p_actor text)
returns jsonb language sql set search_path = '' as $$
  select public.qa_reverse_permission_revision(p_id, p_actor);
$$;
revoke all on function public.qa_write_delegated_policy(text,jsonb,bigint),
  public.qa_update_delegated_account(uuid,jsonb,jsonb,text), public.qa_reverse_delegated_change(bigint,text)
  from public, anon, authenticated;
grant execute on function public.qa_write_delegated_policy(text,jsonb,bigint),
  public.qa_update_delegated_account(uuid,jsonb,jsonb,text), public.qa_reverse_delegated_change(bigint,text)
  to service_role;

notify pgrst, 'reload schema';
commit;
