-- Run once in the Supabase SQL Editor. Backend service-role access only.
create table if not exists public.qa_domains (
  key text primary key check (key ~ '^custom_[a-f0-9]{32}$'),
  label text not null check (length(trim(label)) between 1 and 80)
);
create unique index if not exists qa_domain_names on public.qa_domains (lower(trim(label)));

create table if not exists public.qa_documents (
  namespace text not null,
  domain text not null,
  file_hash text not null check (file_hash ~ '^[a-f0-9]{64}$'),
  name text not null,
  object_path text not null,
  pages integer not null check (pages between 1 and 500),
  next_page integer not null default 0,
  chunks integer not null default 0,
  image_pages integer not null default 0,
  use_llm boolean not null default true,
  cached_chunks jsonb,
  cached_image boolean not null default false,
  status text not null default 'pending' check (status in ('pending', 'ready')),
  lease_token uuid,
  lease_until timestamptz,
  primary key(namespace, domain, file_hash)
);
alter table public.qa_domains enable row level security;
alter table public.qa_documents enable row level security;
revoke all on public.qa_domains, public.qa_documents from anon, authenticated;
grant all on public.qa_domains, public.qa_documents to service_role;

create or replace function public.qa_claim_document(
  p_namespace text, p_domain text, p_hash text, p_token uuid, p_delete boolean default false
) returns jsonb language plpgsql set search_path = public as $$
declare r qa_documents;
begin
  select * into r from qa_documents where namespace=p_namespace and domain=p_domain and file_hash=p_hash for update;
  if not found then return null; end if;
  if r.lease_until > now() then return null; end if;
  if r.status='ready' and not p_delete then return to_jsonb(r); end if;
  update qa_documents set lease_token=p_token, lease_until=now()+interval '5 minutes'
    where namespace=p_namespace and domain=p_domain and file_hash=p_hash returning * into r;
  return to_jsonb(r);
end $$;

create or replace function public.qa_advance_document(
  p_namespace text, p_domain text, p_hash text, p_token uuid, p_chunks integer, p_image integer
) returns jsonb language plpgsql set search_path = public as $$
declare r qa_documents;
begin
  select * into r from qa_documents where namespace=p_namespace and domain=p_domain and file_hash=p_hash
    and lease_token=p_token and lease_until>now() for update;
  if not found then return null; end if;
  if r.next_page+1=r.pages and r.chunks+p_chunks=0 then
    raise exception 'No searchable text found in this PDF';
  end if;
  update qa_documents set next_page=next_page+1, chunks=chunks+p_chunks, image_pages=image_pages+p_image,
    status=case when next_page+1=pages then 'ready' else 'pending' end,
    lease_token=null, lease_until=null, cached_chunks=null, cached_image=false
    where namespace=p_namespace and domain=p_domain and file_hash=p_hash returning * into r;
  return to_jsonb(r);
end $$;

create or replace function public.qa_release_document(
  p_namespace text, p_domain text, p_hash text, p_token uuid
) returns void language sql set search_path = public as $$
  update qa_documents set lease_token=null, lease_until=null
    where namespace=p_namespace and domain=p_domain and file_hash=p_hash and lease_token=p_token;
$$;
revoke all on function public.qa_claim_document(text,text,text,uuid,boolean) from public, anon, authenticated;
revoke all on function public.qa_advance_document(text,text,text,uuid,integer,integer) from public, anon, authenticated;
revoke all on function public.qa_release_document(text,text,text,uuid) from public, anon, authenticated;
grant execute on function public.qa_claim_document(text,text,text,uuid,boolean) to service_role;
grant execute on function public.qa_advance_document(text,text,text,uuid,integer,integer) to service_role;
grant execute on function public.qa_release_document(text,text,text,uuid) to service_role;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('knowledge-pdfs', 'knowledge-pdfs', false, 4000000, array['application/pdf'])
on conflict (id) do nothing;
