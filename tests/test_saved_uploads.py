"""Local RAG + durable-store emulator; no live SQL, providers or cloud writes."""
import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

import httpx
import test_workspace_isolation as isolation
from rag import cloud, config, saved_uploads, workspace


class SavedStore(isolation.SupabaseMemory):
    def __init__(self):
        super().__init__()
        self.tables['users']=[dict(isolation.ALICE,name='Alice',email='alice@example.test',created_at='2026-01-01')]
        self.tables['qa_upload_permissions']=[]
        self.fail_bytes=False

    def request(self,method,url,**kw):
        path=url.split('https://supabase.test',1)[1]
        body,params=kw.get('json',{}),kw.get('params',{})
        rows=self.tables['qa_upload_requests']
        if path.startswith('/storage/v1/object/qa-pending-uploads/'):
            if self.fail_bytes and method=='POST':
                return httpx.Response(503,json={})
            return super().request(method,url,**kw)
        if path.endswith('/qa_upload_requests'):
            def matches(r):
                for key,value in params.items():
                    if not isinstance(value,str): continue
                    actual=r.get(key)
                    if value.startswith('eq.') and str(actual).lower()!=value[3:].lower(): return False
                    if value.startswith('in.(') and str(actual) not in value[4:-1].split(','): return False
                    if value=='not.is.null' and actual is None: return False
                    if value=='is.null' and actual is not None: return False
                return True
            selected=[r for r in rows if matches(r)]
            if method=='POST':
                old=next((r for r in rows if all(r[k]==body[k] for k in ['user_id','domain','file_hash'])),None)
                if old: return httpx.Response(200,json=[])
                row=dict(status='pending',file_ready=False,job_state='none',attempts=0,job_message='',
                    lease_token=None,lease_until=None,created_at=datetime.now(timezone.utc).isoformat(),decided_at=None)
                row.update(body); rows.append(row)
                return httpx.Response(201,json=[copy.deepcopy(row)])
            if method=='PATCH':
                for r in selected:r.update(body)
            return httpx.Response(200,json=copy.deepcopy(selected))
        if path.endswith('/qa_begin_browser_upload'):
            r=next((r for r in rows if r['id']==body['p_id'] and r['user_id']==body['p_owner'] and r['status']=='approved' and r['file_ready']),None)
            if not r:return httpx.Response(200,json=[])
            if r['job_state'] in ['ready','failed']:return httpx.Response(200,json=[copy.deepcopy(r)])
            if r['lease_token']:return httpx.Response(200,json=[])
            if r['job_state'] in ['none','queued','retrying']:r['attempts']+=1
            r.update(job_state='processing',lease_token=body['p_token'])
            return httpx.Response(200,json=[copy.deepcopy(r)])
        if path.endswith('/qa_end_browser_upload'):
            r=next((r for r in rows if r['id']==body['p_id'] and r['user_id']==body['p_owner'] and r['lease_token']==body['p_token']),None)
            if not r:return httpx.Response(200,json=[])
            result=body['p_result']
            state='ready' if result=='ready' else 'processing' if result=='continue' else 'retrying' if r['attempts']<2 else 'failed'
            r.update(job_state=state,lease_token=None)
            return httpx.Response(200,json=[copy.deepcopy(r)])
        if path.endswith('/qa_expire_uploads'):
            cutoff=datetime.now(timezone.utc)-timedelta(days=30);n=0
            for r in rows:
                stamp=r['created_at'] if r['status']=='pending' else r.get('decided_at') if r['status']=='rejected' else None
                if stamp and datetime.fromisoformat(stamp)<=cutoff:r['status']='expired';n+=1
            return httpx.Response(200,json=n)
        return super().request(method,url,**kw)


class SavedUploadTests(unittest.TestCase):
    cloud_mode=False
    req=isolation.LocalWorkspaceTests.req

    def test_decision_readiness_distinguishes_legacy_from_incomplete_transfer(self):
        self.assertTrue(saved_uploads.public_row({'file_ready':False,'object_path':None})['decision_ready'])
        self.assertFalse(saved_uploads.public_row({'file_ready':False,'object_path':'private/path'})['decision_ready'])
        self.assertTrue(saved_uploads.public_row({'file_ready':True,'object_path':'private/path'})['decision_ready'])

    def setUp(self):
        isolation.LocalWorkspaceTests.setUp(self)
        self.storage=SavedStore()
        transport=patch.object(cloud._client,'request',side_effect=self.storage.request)
        transport.start();self.addCleanup(transport.stop)
        self.key=self.req('alice','POST','/domains',json={'name':'Private notes'}).json()['key']

    def save(self,who='alice',key=None):
        return self.req(who,'POST','/uploads',data={'domain':key or self.key,'use_llm':'false'},files={'file':('private.pdf',self.pdf)})

    def approve(self,row):
        result=self.req('admin','PUT','/admin/upload-requests/'+row['id'],json={'status':'approved'})
        self.assertEqual(result.status_code,200,result.text)

    def step(self):
        row=self.storage.tables['qa_upload_requests'][0]
        result=self.req('alice','POST',f'/domains/{row["domain"]}/documents/{row["file_hash"]}/step')
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def test_upload_once_then_approve_and_process_into_own_documents(self):
        response=self.save();self.assertEqual(response.status_code,201,response.text)
        row=response.json();self.assertEqual(row['status'],'pending');self.assertTrue(row['file_ready'])
        self.assertNotIn('object_path',row);self.assertNotIn('lease_token',row)
        self.assertEqual(len(self.storage.objects),1)
        self.assertEqual(self.req('alice','POST',f'/domains/{self.key}/documents/{row["file_hash"]}/step').status_code,403)
        self.assertEqual(self.req('alice','GET',f'/domains/{self.key}/status').json()['chunks'],0)
        self.approve(row)
        self.assertEqual(self.step()['status'],'ready')
        saved=self.storage.tables['qa_upload_requests'][0]
        self.assertEqual(saved['job_state'],'ready');self.assertEqual(saved['attempts'],1)
        self.assertGreater(self.req('alice','GET',f'/domains/{self.key}/status').json()['chunks'],0)
        self.assertEqual(self.req('bob','GET',f'/domains/{self.key}/status').status_code,404)
        self.assertEqual(self.req('admin','GET',f'/domains/{self.key}/status').status_code,404)

    def test_processing_failure_retries_only_once_and_resubmit_cannot_reset(self):
        row=self.save().json();self.approve(row)
        with patch('rag.ingest.ingest_pdf',side_effect=RuntimeError('private provider failure')) as process:
            self.step();self.assertEqual(self.storage.tables['qa_upload_requests'][0]['job_state'],'retrying')
            self.step();self.assertEqual(self.storage.tables['qa_upload_requests'][0]['job_state'],'failed')
            for _ in range(3):self.assertEqual(self.step()['status'],'failed')
            again=self.save();self.assertEqual(again.json()['job_state'],'failed')
            self.assertEqual(again.json()['attempts'],2);self.assertEqual(process.call_count,2)
        self.assertNotIn('private provider failure',self.req('alice','GET','/upload-requests').text)

    def test_second_attempt_can_succeed_without_reupload(self):
        row=self.save().json();self.approve(row)
        with patch('rag.ingest.ingest_pdf',side_effect=[RuntimeError('failure'),{'status':'ingested'}]) as process:
            self.step();self.step()
            self.assertEqual(process.call_count,2)
        self.assertEqual(self.storage.tables['qa_upload_requests'][0]['job_state'],'ready')
        self.assertEqual(len(self.storage.tables['qa_upload_requests']),1)

    def test_duplicate_and_failed_transfer_reservations_are_idempotent(self):
        self.storage.fail_bytes=True
        self.assertEqual(self.save().status_code,502)
        self.assertEqual(len(self.storage.tables['qa_upload_requests']),1)
        self.assertFalse(self.storage.tables['qa_upload_requests'][0]['file_ready'])
        self.storage.fail_bytes=False
        first=self.save().json();second=self.save().json()
        self.assertEqual(first['id'],second['id']);self.assertEqual(len(self.storage.objects),1)

    def test_no_worker_endpoint_or_bearer_bypass_and_owner_checks(self):
        self.assertEqual(self.save('bob').status_code,404)
        for who in ['alice','bob','admin']:
            self.assertEqual(self.req(who,'POST','/internal/upload-worker').status_code,404)
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/internal/upload-worker',headers={'Authorization':'Bearer obsolete-secret','X-CSRF-Protection':'1'}).status_code,401)

    def test_30_day_cleanup_does_not_touch_approved_documents(self):
        row=self.save().json();entry=self.storage.tables['qa_upload_requests'][0]
        old=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        entry['created_at']=old
        entry['status']='rejected';entry['decided_at']=datetime.now(timezone.utc).isoformat()
        self.assertEqual(saved_uploads.cleanup(),0)  # rejected clock starts at decision
        entry['decided_at']=old
        self.assertEqual(saved_uploads.cleanup(),1);self.assertEqual(entry['status'],'expired')
        self.assertFalse(self.storage.objects)
        entry.update(status='approved',object_path='11111111-1111-4111-8111-111111111111/'+row['id'])
        self.assertEqual(saved_uploads.cleanup(),0);self.assertEqual(entry['status'],'approved')

    def test_allowed_queues_without_admin_and_blocked_denies(self):
        self.storage.tables['qa_upload_permissions']=[{'user_id':isolation.ALICE['id'],'policy':'blocked'}]
        self.assertEqual(self.save().status_code,403)
        self.storage.tables['qa_upload_permissions'][0]['policy']='allowed'
        row=self.save().json();self.assertEqual(row['job_state'],'queued');self.assertEqual(row['status'],'approved')

    def test_user_role_direct_upload_queues_and_preserves_explicit_block(self):
        pending = self.save().json()
        self.assertEqual(pending['status'], 'pending')
        with patch.dict(isolation.ALICE, {'permissions': {'user_uploads.add': True}}):
            self.assertEqual(self.req('alice', 'GET', '/upload-requests').json()['policy'], 'allowed')
            queued = self.save().json()
            self.assertEqual((queued['id'], queued['status'], queued['job_state']), (pending['id'], 'approved', 'queued'))
            self.storage.tables['qa_upload_permissions'] = [{'user_id': isolation.ALICE['id'], 'policy': 'blocked'}]
            self.assertEqual(self.save().status_code, 403)
        self.storage.tables['qa_upload_permissions'] = []
        self.assertEqual(self.req('alice', 'GET', '/upload-requests').json()['policy'], 'approval_required')

    def test_manual_reprocessing_cannot_bypass_automatic_retry_limit(self):
        row=self.save().json();self.approve(row)
        entry=self.storage.tables['qa_upload_requests'][0];entry.update(job_state='failed',attempts=2)
        self.assertEqual(self.req('alice','POST',f'/domains/{self.key}/ingest',files={'file':('private.pdf',self.pdf)}).status_code,409)

    def test_cloud_page_continuations_do_not_consume_failure_retry(self):
        row=self.save().json();self.approve(row)
        with patch.object(config,'CLOUD_MODE',True),patch('rag.domains.get_domain',return_value=object()),patch.object(cloud,'documents',return_value=[{'file_hash':row['file_hash']}]),patch.object(cloud,'step',side_effect=[{'status':'pending'},{'status':'pending'},{'status':'ready'}]) as step:
            # Give domain the attributes expected by the existing cloud pipeline.
            from types import SimpleNamespace
            with patch('rag.domains.get_domain',return_value=SimpleNamespace(key=self.key)):
                self.step();self.step();self.step()
        entry=self.storage.tables['qa_upload_requests'][0]
        self.assertEqual(entry['attempts'],1);self.assertEqual(entry['job_state'],'ready')
        self.assertEqual(step.call_count,3)

    def test_cleanup_storage_failure_is_retried_without_deleting_history(self):
        self.save();entry=self.storage.tables['qa_upload_requests'][0]
        entry['created_at']=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        original=self.storage.request
        def broken(method,url,**kw):
            if method=='DELETE' and '/storage/' in url:return httpx.Response(503,json={})
            return original(method,url,**kw)
        with patch.object(cloud._client,'request',side_effect=broken):
            with self.assertRaises(cloud.CloudStorageError):saved_uploads.cleanup()
        self.assertEqual(entry['status'],'expired');self.assertIsNotNone(entry['object_path'])
        self.assertEqual(saved_uploads.cleanup(),1);self.assertIsNone(entry['object_path'])

    def test_cloud_two_page_saved_upload_uses_existing_private_pipeline(self):
        import pymupdf
        with pymupdf.open() as pdf:
            for text in ['Private first page about mango orchards.','Private second page about harvest dates.']:
                pdf.new_page().insert_text((50,80),text)
            self.pdf=pdf.tobytes()
        with patch.object(config,'CLOUD_MODE',True):
            self.key=self.req('alice','POST','/domains',json={'name':'Cloud private'}).json()['key']
            row=self.save().json();self.assertEqual(row['pages'],2);self.approve(row)
            self.step()
            self.assertEqual(self.storage.tables['qa_upload_requests'][0]['job_state'],'processing')
            self.step()
            entry=self.storage.tables['qa_upload_requests'][0]
            self.assertEqual(entry['job_state'],'ready');self.assertEqual(entry['attempts'],1)
            result=self.req('alice','GET',f'/domains/{self.key}/status').json()
            self.assertGreater(result['chunks'],0)
            self.assertEqual(result['files'][row['file_hash']]['pages'],2)
            self.assertEqual(self.req('bob','GET',f'/domains/{self.key}/status').status_code,404)
            self.assertEqual(self.req('alice','POST',f'/domains/{self.key}/documents/{row["file_hash"]}/step').json()['status'],'ready')

    def test_cloud_resume_after_browser_closes_does_not_upload_again(self):
        import pymupdf
        from fastapi.testclient import TestClient
        import api_server
        with pymupdf.open() as pdf:
            for text in ['First page.','Second page.','Third page.']:
                pdf.new_page().insert_text((50,80),text)
            self.pdf=pdf.tobytes()
        with patch.object(config,'CLOUD_MODE',True):
            self.key=self.req('alice','POST','/domains',json={'name':'Resume private'}).json()['key']
            row=self.save().json();self.approve(row)
            self.assertEqual(self.step()['status'],'pending')
            self.assertEqual(self.storage.tables['qa_documents'][0]['next_page'],1)
            # A new browser/session request resumes the existing saved document.
            with TestClient(api_server.app) as reopened:
                headers={**isolation.CSRF,'Cookie':'test_identity=alice'}
                response=reopened.get('/api/upload-requests',headers=headers)
                self.assertEqual(response.json()['processing'][0]['file_hash'],row['file_hash'])
                path=f'/api/domains/{self.key}/documents/{row["file_hash"]}/step'
                self.assertEqual(reopened.post(path,headers=headers).json()['status'],'pending')
                self.assertEqual(reopened.post(path,headers=headers).json()['status'],'ready')
            self.assertEqual(self.storage.tables['qa_documents'][0]['next_page'],3)
            self.assertEqual(self.storage.tables['qa_upload_requests'][0]['attempts'],1)
            self.assertEqual(sum(method=='POST' and '/storage/v1/object/qa-pending-uploads/' in path for method,path,*_ in self.storage.calls),1)

    def test_step_cannot_process_someone_elses_or_rejected_saved_upload(self):
        row=self.save().json();self.approve(row)
        path=f'/domains/{self.key}/documents/{row["file_hash"]}/step'
        for who in ['bob','admin']:
            self.assertEqual(self.req(who,'POST',path).status_code,404)
        self.storage.tables['qa_upload_requests'][0]['status']='rejected'
        self.assertEqual(self.req('alice','POST',path).status_code,403)

    def test_live_lease_pauses_duplicate_tab_without_spending_retry(self):
        row=self.save().json();self.approve(row)
        stored=self.storage.tables['qa_upload_requests'][0]
        stored.update(job_state='processing',attempts=1,lease_token='already-in-flight')
        self.assertEqual(self.step()['status'],'paused')
        self.assertEqual(stored['attempts'],1)

    def test_saved_image_browser_step_preserves_ocr_and_private_text(self):
        import io
        from PIL import Image
        with io.BytesIO() as buffer:
            Image.new('RGB',(100,100),'white').save(buffer,format='PNG')
            image=buffer.getvalue()
        with patch.object(config,'CLOUD_MODE',True),patch.object(cloud,'_ocr_page',return_value='Private scanned research result.'):
            key=self.req('alice','POST','/domains',json={'name':'Private image'}).json()['key']
            result=self.req('alice','POST','/uploads',data={'domain':key,'use_llm':'false'},files={'file':('scan.png',image)})
            self.assertEqual(result.status_code,201,result.text)
            row=result.json();self.approve(row)
            # This is the only saved upload, so the normal browser step helper applies.
            self.assertEqual(self.step()['status'],'ready')
            path=f'/domains/{key}/documents/{row["file_hash"]}/text'
            text=self.req('alice','GET',path)
            self.assertEqual(text.status_code,200,text.text)
            self.assertIn('Private scanned research result.',text.json()['text'])
            self.assertEqual(self.req('bob','GET',path).status_code,404)

    def test_admin_receives_verified_pages_size_and_attached_message(self):
        response=self.req('alice','POST','/uploads',data={'domain':self.key,'message':'Please approve these research notes.','pages':'999'},files={'file':('private.pdf',self.pdf)})
        self.assertEqual(response.status_code,201,response.text)
        row=response.json();self.assertEqual(row['pages'],1);self.assertEqual(row['size'],len(self.pdf))
        admin=self.req('admin','GET','/admin/users').json()['requests'][0]
        self.assertEqual(admin['pages'],1);self.assertEqual(admin['size'],len(self.pdf))
        self.assertEqual(admin['message'],'Please approve these research notes.')

    def test_unreadable_pdf_does_not_create_approval_request(self):
        result=self.req('alice','POST','/uploads',data={'domain':self.key},files={'file':('bad.pdf',b'not a PDF')})
        self.assertEqual(result.status_code,400,result.text)
        self.assertEqual(self.storage.tables['qa_upload_requests'],[])

    def test_user_can_remove_own_card_but_admin_retains_details_and_file(self):
        row=self.save().json();before=copy.deepcopy(self.storage.tables['qa_upload_requests'][0])
        objects=copy.deepcopy(self.storage.objects)
        path='/upload-requests/'+row['id']
        self.assertEqual(self.req('bob','DELETE',path).status_code,404)
        self.assertEqual(self.req('admin','DELETE',path).status_code,403)
        self.assertEqual(self.req('alice','DELETE',path).status_code,200)
        self.assertEqual(self.req('alice','GET','/upload-requests').json()['requests'],[])
        admin=self.req('admin','GET','/admin/users').json()['requests'][0]
        self.assertTrue(admin['user_deleted_at']);self.assertEqual(admin['filename'],'private.pdf')
        self.assertEqual(admin['size'],len(self.pdf));self.assertEqual(admin['pages'],1)
        after=self.storage.tables['qa_upload_requests'][0]
        self.assertEqual({k:v for k,v in after.items() if k!='user_deleted_at'},before)
        self.assertEqual(self.storage.objects,objects)
        deleted_at=after['user_deleted_at']
        self.assertEqual(self.req('alice','DELETE',path).status_code,200)
        self.assertEqual(after['user_deleted_at'],deleted_at)
        self.approve(row);self.step()
        self.assertEqual(after['job_state'],'ready')
        self.assertEqual(self.req('alice','GET','/upload-requests').json()['requests'],[])
        self.assertEqual(self.req('alice','GET','/upload-requests').json()['ready_documents'][0]['file_hash'],row['file_hash'])
        self.assertEqual(self.req('bob','GET','/upload-requests').json()['ready_documents'],[])
        self.assertGreater(self.req('alice','GET',f'/domains/{self.key}/status').json()['chunks'],0)


if __name__=='__main__':unittest.main()
