-- Run in the Supabase SQL Editor after setup.sql.
-- Open public.document_overview in the Table Editor to browse registered PDFs.
begin;

create view public.document_overview
with (security_invoker = true) as
select
  d.name as "PDF Filename",
  coalesce(
    k.label,
    case d.domain
      when 'customer_support' then 'Customer Support'
      when 'legal_assistant' then 'Legal Assistant'
      else 'Unknown Knowledge Base'
    end
  ) as "Knowledge Base Name",
  d.namespace || '_' || d.domain as "Collection Name",
  d.domain as "Knowledge Base ID",
  initcap(d.status) as "Status"
from public.qa_documents as d
left join public.qa_domains as k
  on k.key = d.domain
  and (
    (k.owner_id = 'admin' and d.namespace ~ '^api_[0-9a-f]{16}$')
    or (
      k.owner_id <> 'admin'
      and d.namespace ~ '^api_[0-9a-f]{16}_user_[0-9a-f]{32}$'
      and split_part(d.namespace, '_', 4) = replace(k.owner_id, '-', '')
    )
  );

revoke all on public.document_overview from public, anon, authenticated;

commit;
