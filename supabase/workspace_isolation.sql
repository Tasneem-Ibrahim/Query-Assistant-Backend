-- Run in Supabase SQL Editor BEFORE deploying the workspace-aware backend.
-- Existing custom bases belong to the original admin; documents/storage/vectors
-- keep their legacy namespaces, now used only by admin. No documents are moved.
begin;
alter table public.qa_domains add column if not exists owner_id text not null default 'admin';
alter table public.qa_domains drop constraint if exists qa_domains_pkey;
alter table public.qa_domains add primary key (owner_id, key);
drop index if exists public.qa_domain_names;
create unique index qa_domain_names on public.qa_domains (owner_id, lower(trim(label)));
alter table public.qa_domains enable row level security;
alter table public.qa_documents enable row level security;
revoke all on public.qa_domains, public.qa_documents from public, anon, authenticated;
grant all on public.qa_domains, public.qa_documents to service_role;
-- Storage is accessed only through the backend's account-checked document routes.
update storage.buckets set public=false where id='knowledge-pdfs';
commit;
