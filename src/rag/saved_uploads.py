"""Private saved uploads processed by authenticated, resumable browser requests."""
import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from fastapi import HTTPException

from . import cloud, config, workspace

BUCKET = 'qa-pending-uploads'


def object_url(path):
    parts = path.split('/')
    if len(parts) != 2 or any(str(UUID(p)) != p for p in parts):
        raise ValueError('Invalid saved-upload storage path.')
    return '/storage/v1/object/' + BUCKET + '/' + quote(path, safe='/')


def public_row(row):
    result = {k: v for k, v in row.items() if k not in {'object_path','lease_token','lease_until'}}
    # Legacy metadata-only requests can be decided; incomplete byte transfers cannot.
    result['decision_ready'] = not row.get('object_path') or bool(row.get('file_ready'))
    return result


def page_count(filename, content):
    """Read PDF metadata only; no OCR, text extraction or vectorization here."""
    if Path(filename).suffix.lower() != '.pdf':
        return 1
    import pymupdf
    try:
        with pymupdf.open(stream=content,filetype='pdf') as pdf:
            if pdf.is_encrypted or not 1 <= len(pdf) <= 500:
                raise ValueError('Use an unlocked PDF with 1-500 pages.')
            return len(pdf)
    except (ValueError,RuntimeError) as exc:
        raise HTTPException(400,'Choose a readable, unlocked PDF with 1-500 pages.') from None


def queue_saved_upload(request_id, object_path, mode):
    return cloud._request('PATCH','/rest/v1/qa_upload_requests',
        params={'id':'eq.'+request_id,'status':'in.(pending,approved)' if mode == 'allowed' else 'eq.approved',
                'job_state':'eq.none','file_ready':'eq.true'},
        headers={'Prefer':'return=representation'},json={'status':'approved','object_path':object_path,
            'job_state':'queued','job_message':'Your file is saved. Processing starts when your Dashboard is open.'}).json()


def save(domain, filename, content, message='', use_llm=True, *, request_approval=False, direct_upload=None):
    from .admin_panel import upload_mode
    from .extract import SUPPORTED_EXTENSIONS
    owner = workspace.owner_id()
    if owner == 'admin':
        raise HTTPException(400, 'Admin can use the existing direct upload.')
    mode = upload_mode(owner, direct_upload)
    if mode == 'blocked':
        raise HTTPException(403, 'Uploads are blocked for your account.')
    if request_approval:
        mode = 'approval_required'
    if not content or len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, 'Choose a non-empty file smaller than 4 MB.')
    if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, 'Unsupported file type.')
    pages = page_count(filename,content)
    digest = hashlib.sha256(content).hexdigest()
    request_id = str(uuid5(NAMESPACE_URL, f'qa-upload:{owner}:{domain.key}:{digest}'))
    path = owner + '/' + request_id
    row = dict(id=request_id,user_id=owner,domain=domain.key,domain_label=domain.label,
               filename=filename[:255],file_hash=digest,size=len(content),message=message,
               object_path=path,use_llm=use_llm,pages=pages)
    # Commit a reservation before writing bytes so incomplete uploads can expire.
    cloud._request('POST','/rest/v1/qa_upload_requests',params={'on_conflict':'user_id,domain,file_hash'},
        headers={'Prefer':'resolution=ignore-duplicates'},json=row)
    existing = cloud.rows('qa_upload_requests',user_id='eq.'+owner,domain='eq.'+domain.key,file_hash='eq.'+digest)[0]
    if existing.get('admin_deleted_at'):
        raise HTTPException(409, 'An approval request for this file was removed.')
    if existing.get('file_ready') and mode == 'allowed' and direct_upload and existing['status'] == 'pending':
        queued = queue_saved_upload(existing['id'], existing['object_path'], mode)
        return public_row((queued or cloud.rows('qa_upload_requests', id='eq.'+existing['id']))[0])
    if existing.get('file_ready') or existing['status'] in {'rejected','expired'}:
        return public_row(existing)
    if existing.get('object_path') != path:
        # Upgrade a legacy metadata-only request without resetting its decision.
        cloud._request('PATCH','/rest/v1/qa_upload_requests',params={'id':'eq.'+existing['id'],'file_ready':'eq.false'},
                       json={'object_path':path,'use_llm':use_llm,'pages':pages})
    cloud._request('POST',object_url(path),content=content,
        headers={'Content-Type':mimetypes.guess_type(filename)[0] or 'application/octet-stream','x-upsert':'true'})
    # Conditional status updates cannot undo a concurrent admin rejection/expiry.
    result = cloud._request('PATCH','/rest/v1/qa_upload_requests',
        params={'id':'eq.'+existing['id'],'status':'in.(pending,approved)'},headers={'Prefer':'return=representation'},
        json={'file_ready':True,'object_path':path,'pages':pages,'job_message':'Your file is saved and awaiting admin approval.'}).json()
    queued = queue_saved_upload(existing['id'], path, mode)
    if queued:
        result = queued
    if not result:
        result = cloud.rows('qa_upload_requests',id='eq.'+existing['id'])
    if result[0]['status'] == 'expired':
        # Expiration can win while bytes are in flight. Do not leave an orphan.
        cloud._request('DELETE','/storage/v1/object/'+BUCKET,json={'prefixes':[path]})
    return public_row(result[0])


def cleanup():
    cloud._request('POST','/rest/v1/rpc/qa_expire_uploads',json={})
    count = 0
    # Expired rows remain as status history; bytes are removed idempotently.
    expired = cloud._request('GET','/rest/v1/qa_upload_requests',params={
        'status':'eq.expired','object_path':'not.is.null','order':'created_at.asc,id.asc','limit':5}).json()
    for row in expired[:5]:
        path = row['object_path']
        object_url(path)  # Validate before any deletion.
        cloud._request('DELETE','/storage/v1/object/'+BUCKET,json={'prefixes':[path]})
        cloud._request('PATCH','/rest/v1/qa_upload_requests',params={'id':'eq.'+row['id'],'status':'eq.expired'},
                       json={'object_path':None,'file_ready':False})
        count += 1
    return count


def find(key, digest):
    if workspace.owner_id() == 'admin':
        return None
    rows = cloud.rows('qa_upload_requests',user_id='eq.'+workspace.owner_id(),domain='eq.'+key,file_hash='eq.'+digest)
    return rows[0] if rows and (rows[0].get('file_ready') or rows[0].get('admin_deleted_at')) else None


def process_step(row):
    """Advance one saved page in the current authenticated user's workspace."""
    from .domains import get_domain
    from . import ingest
    from .admin_panel import policy
    owner = workspace.owner_id()
    if row['user_id'] != owner or owner == 'admin':
        raise HTTPException(404,'Upload request not found.')
    if row['status'] != 'approved' or policy(owner) == 'blocked':
        raise HTTPException(403,'This file is not approved for processing.')
    if row.get('job_state') == 'ready':
        return {'status':'ready','report':{'name':row['filename'],'pages':row.get('pages'),'status':'ingested'}}
    if row.get('job_state') == 'failed':
        return {'status':'failed','message':row.get('job_message') or 'Processing failed after the single retry.'}
    token = str(uuid4())
    claimed = cloud._request('POST','/rest/v1/rpc/qa_begin_browser_upload',
        json={'p_id':row['id'],'p_owner':owner,'p_token':token}).json()
    if not claimed:
        return {'status':'paused','message':'Another request is finishing this file. Processing will resume while your Dashboard is open.'}
    row = claimed[0]
    if row.get('job_state') == 'failed':
        return {'status':'failed','message':row.get('job_message') or 'The single retry is exhausted.'}
    if row.get('job_state') == 'ready':
        return {'status':'ready','report':{'name':row['filename'],'pages':row.get('pages')}}
    result = 'error'
    report = {'name':row['filename']}
    try:
        if row['object_path'].split('/')[0] != owner:
            raise ValueError('Saved file does not belong to this workspace.')
        domain = get_domain(row['domain'])
        if config.CLOUD_MODE:
            current = next((r for r in cloud.documents(domain.key) if r['file_hash']==row['file_hash']),None)
            if current is None:
                content = cloud._request('GET',object_url(row['object_path'])).content
                if hashlib.sha256(content).hexdigest() != row['file_hash']:
                    raise ValueError('Saved file checksum mismatch.')
                name = re.sub(r'[^A-Za-z0-9._-]+','_',row['filename'])
                cloud.start(domain,name,content,use_llm=row['use_llm'])
            step = cloud.step(domain,row['file_hash'])
            report = step.get('report',report)
            result = 'ready' if step['status']=='ready' else 'continue'
        else:
            content = cloud._request('GET',object_url(row['object_path'])).content
            if hashlib.sha256(content).hexdigest() != row['file_hash']:
                raise ValueError('Saved file checksum mismatch.')
            domain.pdf_dir.mkdir(parents=True,exist_ok=True)
            target = domain.pdf_dir / (row['file_hash'][:12]+'_'+re.sub(r'[^A-Za-z0-9._-]+','_',row['filename']))
            target.write_bytes(content)
            report = ingest.ingest_pdf(domain,target,use_llm=row['use_llm'])
            result = 'ready'
    except Exception:
        # Provider diagnostics/credentials are never returned to browsers.
        result = 'error'
    finished = cloud._request('POST','/rest/v1/rpc/qa_end_browser_upload',
        json={'p_id':row['id'],'p_owner':owner,'p_token':token,'p_result':result}).json()
    if not finished:
        return {'status':'paused','message':'Processing paused. Saved progress will resume on your Dashboard.'}
    updated = finished[0]
    return {'status':'ready' if updated['job_state']=='ready' else 'failed' if updated['job_state']=='failed' else 'pending',
            'message':updated.get('job_message',''),'report':report}
