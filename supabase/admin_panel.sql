-- Run AFTER auth.sql and workspace_isolation.sql. Metadata only; no pending files.
begin;
create table if not exists public.qa_upload_permissions (
  user_id uuid primary key references auth.users(id) on delete cascade,
  policy text not null default 'approval_required'
    check (policy in ('approval_required', 'allowed', 'blocked'))
);
create table if not exists public.qa_upload_requests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  domain text not null,
  domain_label text not null,
  filename text not null,
  file_hash text not null check (file_hash ~ '^[0-9a-f]{64}$'),
  size bigint not null check (size > 0 and size <= 4000000),
  message text not null default '',
  status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  reason text not null default '',
  created_at timestamptz not null default now(),
  decided_at timestamptz,
  unique(user_id, domain, file_hash)
);
create index if not exists qa_upload_requests_pending on public.qa_upload_requests(status, created_at);
alter table public.qa_upload_permissions enable row level security;
alter table public.qa_upload_requests enable row level security;
revoke all on public.qa_upload_permissions, public.qa_upload_requests from anon, authenticated;
grant all on public.qa_upload_permissions, public.qa_upload_requests to service_role;
commit;
