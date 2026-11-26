-- Apply after revision_management.sql. Run before deploying the hierarchical permission endpoints.
begin;

-- Keep ordinary service-role API clients append-only for audit tables.
revoke update, delete, truncate on public.qa_activity_events, public.qa_login_events,
  public.qa_admin_audit, public.qa_setting_revisions from service_role;

create or replace function public.qa_revision_account_state(settings jsonb)
returns jsonb language sql immutable set search_path = '' as $$
  select jsonb_build_object(
    'role', case when settings->>'role' = 'admin' then 'admin' else 'user' end,
    'status', coalesce(settings->>'status', 'active'),
    'permissions', coalesce(settings->'permissions', '{}'::jsonb),
    'delegated_overrides', coalesce(settings->'delegated_overrides', '[]'::jsonb)
  );
$$;

create or replace function public.qa_revision_security_state(setting_key text, value jsonb)
returns jsonb language sql immutable set search_path = '' as $$
  select case
    when setting_key like 'policy:%' then jsonb_build_object(
      'modules', '{}'::jsonb, 'actions', '{}'::jsonb) || value
    when setting_key = 'status:local-admin' then jsonb_build_object('active', coalesce(value->'active', 'true'::jsonb))
    else value
  end;
$$;

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
  if actor is null or actor = '' then return new; end if;
  case kind_key
    when 'security' then
      target_key := new.key;
      if target_key not in ('policy:admin', 'policy:user', 'account:local-admin', 'status:local-admin') then return new; end if;
      before_value := public.qa_revision_security_state(target_key, case when tg_op = 'INSERT' then '{}'::jsonb else old.value end);
      after_value := public.qa_revision_security_state(target_key, new.value);
      if target_key = 'status:local-admin' then category := 'user-details'; end if;
    when 'account' then
      target_key := new.id::text;
      before_value := public.qa_revision_account_state(old.raw_app_meta_data->'qa_access');
      after_value := public.qa_revision_account_state(new.raw_app_meta_data->'qa_access');
      if before_value->>'status' = 'deleting' or after_value->>'status' = 'deleting' then return new; end if;
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
      or (kind_key = 'account' and before_value->'permissions' <> after_value->'permissions') then
    insert into public.qa_activity_events(category, actor_id, target, action)
      values (category, actor, target_key,
              case when reversed is null then 'Updated permissions'
                else 'Reversed RV-C' || lpad(reversed::text, greatest(6, length(reversed::text)), '0') end);
    perform set_config('qa.new_revision', new_id::text, true);
    perform set_config('qa.new_revision_category', category, true);
  end if;
  return new;
end;
$$;


-- Serialize delegation checks with writers of the authority and policy snapshots.
-- Only server revision calls use X-QA-Delegation, and public callers have no write
-- access to these fields. The header carries snapshots, never user-supplied grants.
create or replace function public.qa_check_delegation_snapshot()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  headers jsonb := coalesce(nullif(current_setting('request.headers', true), ''), '{}')::jsonb;
  snapshot jsonb := (headers->>'x-qa-delegation')::jsonb;
  actor text := headers->>'x-qa-revision-actor';
  entry record;
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
    if not exists (select 1 from auth.users
                   where id = actor::uuid and raw_app_meta_data->'qa_access' = snapshot->'account')
       or exists (select 1 from public.qa_role_settings where role = 'admin' and not active) then
      raise sqlstate 'PT409' using message = 'Admin authority changed. Refresh permissions and try again.';
    end if;
  end if;
  for entry in select * from jsonb_each_text(coalesce(snapshot->'state', '{}'::jsonb)) loop
    if coalesce((select revision from public.qa_security_state where key = entry.key), 0) <> entry.value::bigint then
      raise sqlstate 'PT409' using message = 'Permission settings changed. Refresh and try again.';
    end if;
  end loop;
  return new;
end;
$$;

drop trigger if exists qa_delegation_security on public.qa_security_state;
create trigger qa_delegation_security before insert or update on public.qa_security_state
  for each row execute function public.qa_check_delegation_snapshot();
drop trigger if exists qa_delegation_account on auth.users;
create trigger qa_delegation_account before update of raw_app_meta_data on auth.users
  for each row execute function public.qa_check_delegation_snapshot();
drop trigger if exists qa_delegation_role on public.qa_role_settings;
create trigger qa_delegation_role before insert or update on public.qa_role_settings
  for each row execute function public.qa_check_delegation_snapshot();

revoke all on function public.qa_check_delegation_snapshot() from public, anon, authenticated;
create or replace function public.qa_reverse_setting_revision(p_id bigint, p_actor text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  item public.qa_setting_revisions%rowtype;
  state jsonb;
  current_revision bigint;
  is_active boolean;
  upload_policy text;
  access_state jsonb;
  next_access jsonb;
  role_active boolean;
  destination bigint;
begin
  select * into item from public.qa_setting_revisions where id = p_id;
  if not found then return jsonb_build_object('id', -1); end if;
  perform set_config('qa.revision_actor', p_actor, true);
  perform set_config('qa.reversed_revision', p_id::text, true);

  case item.kind
    when 'security' then
      select value, revision into state, current_revision from public.qa_security_state where key = item.target for update;
      if not found or public.qa_revision_security_state(item.target, state) <> item.after_state then return jsonb_build_object('id', 0); end if;
      if not public.qa_write_security_state(item.target, item.before_state, current_revision) then return jsonb_build_object('id', 0); end if;
    when 'account' then
      select coalesce(raw_app_meta_data->'qa_access', '{}'::jsonb) into access_state
        from auth.users where id = item.target::uuid for update;
      if not found or public.qa_revision_account_state(access_state) <> public.qa_revision_account_state(item.after_state) then return jsonb_build_object('id', 0); end if;
      if item.before_state->>'role' = 'admin' and item.after_state->>'role' <> 'admin' then
        select active into role_active from public.qa_role_settings where role = 'admin' for share;
        if role_active is false then return jsonb_build_object('id', 0); end if;
      end if;
      next_access := access_state || public.qa_revision_account_state(item.before_state) || jsonb_build_object('updated_at', now());
      if not public.qa_update_account_access(item.target::uuid, access_state, next_access,
          'Reversed account revision ' || p_id::text) then return jsonb_build_object('id', 0); end if;
    when 'admin-role' then
      select active into is_active from public.qa_role_settings where role = item.target for update;
      if not found or jsonb_build_object('active', is_active) <> item.after_state then return jsonb_build_object('id', 0); end if;
      perform public.qa_set_admin_role_active((item.before_state->>'active')::boolean);
    when 'upload-policy' then
      select policy into upload_policy from public.qa_upload_permissions where user_id = item.target::uuid for update;
      if not found or jsonb_build_object('policy', upload_policy) <> item.after_state then return jsonb_build_object('id', 0); end if;
      update public.qa_upload_permissions set policy = item.before_state->>'policy' where user_id = item.target::uuid;
  end case;

  destination := nullif(current_setting('qa.new_revision', true), '')::bigint;
  if destination is null then raise exception 'Reversal history could not be recorded'; end if;
  return jsonb_build_object('id', destination, 'target', item.target,
      'category', current_setting('qa.new_revision_category', true));
end;
$$;

-- New entry points make a mixed-version deployment fail closed for permission
-- writes when this migration has not been applied.
create or replace function public.qa_write_permission_state(p_key text, p_value jsonb, p_revision bigint)
returns boolean language sql set search_path = '' as $$
  select public.qa_write_security_state(p_key, p_value, p_revision);
$$;

create or replace function public.qa_update_permission_account(p_user uuid, p_previous jsonb, p_next jsonb, p_action text)
returns boolean language sql set search_path = '' as $$
  select public.qa_update_account_access(p_user, p_previous, p_next, p_action);
$$;

create or replace function public.qa_reverse_permission_revision(p_id bigint, p_actor text)
returns jsonb language sql set search_path = '' as $$
  select public.qa_reverse_setting_revision(p_id, p_actor);
$$;

revoke all on function public.qa_write_permission_state(text, jsonb, bigint) from public, anon, authenticated;
revoke all on function public.qa_update_permission_account(uuid, jsonb, jsonb, text) from public, anon, authenticated;
grant execute on function public.qa_write_permission_state(text, jsonb, bigint) to service_role;
grant execute on function public.qa_update_permission_account(uuid, jsonb, jsonb, text) to service_role;

revoke all on function public.qa_reverse_permission_revision(bigint, text) from public, anon, authenticated;
grant execute on function public.qa_reverse_permission_revision(bigint, text) to service_role;

notify pgrst, 'reload schema';
commit;
