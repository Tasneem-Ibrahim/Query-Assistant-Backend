"""Approval boundaries with temporary local RAG and mocked Supabase REST."""
import copy
import hashlib
import unittest
from unittest.mock import patch

import test_workspace_isolation as isolation
from test_workspace_isolation import ALICE, BOB
from rag import admin_panel


class ApprovalTests(unittest.TestCase):
    cloud_mode = False
    setUp = isolation.LocalWorkspaceTests.setUp
    req = isolation.LocalWorkspaceTests.req
    upload = isolation.LocalWorkspaceTests.upload

    def prepare(self):
        self.storage.tables['qa_upload_permissions'] = []
        self.storage.tables['users'] = [dict(ALICE, name='Alice', email='alice@example.test', created_at='2026-11-14')]
        self.key = self.req('alice', 'POST', '/domains', json={'name': 'Private'}).json()['key']
        self.row = {'id': '33333333-3333-4333-8333-333333333333', 'user_id': ALICE['id'],
                    'domain': self.key, 'domain_label': 'Private', 'filename': 'private.pdf',
                    'file_hash': hashlib.sha256(self.pdf).hexdigest(), 'size': len(self.pdf),
                    'status': 'pending', 'created_at': '2026-11-14', 'message': '', 'reason': ''}
        self.storage.tables['qa_upload_requests'] = [copy.deepcopy(self.row)]

    def test_pending_blocks_processing_and_approved_file_works(self):
        self.prepare()
        path = f'/domains/{self.key}/ingest'
        with patch('api_server.ingest.ingest_pdf') as process:
            response = self.req('alice', 'POST', path, files={'file': ('private.pdf', self.pdf)})
            self.assertEqual(response.status_code, 403)
            process.assert_not_called()
        result = self.req('admin', 'PUT', '/admin/upload-requests/' + self.row['id'], json={'status': 'approved'})
        self.assertEqual(result.status_code, 200, result.text)
        self.upload('alice', self.key)
        # A different byte sequence needs a separate approval.
        self.assertEqual(self.req('alice', 'POST', path, files={'file': ('private.pdf', self.pdf + b'\n')}).status_code, 403)
        self.assertEqual(self.req('alice', 'POST', path, files={'file': ('renamed.pdf', self.pdf)}).status_code, 403)
        other = self.req('alice', 'POST', '/domains', json={'name': 'Another base'}).json()['key']
        self.assertEqual(self.req('alice', 'POST', f'/domains/{other}/ingest', files={'file': ('private.pdf', self.pdf)}).status_code, 403)

    def test_regular_users_cannot_administer_or_read_other_requests(self):
        self.prepare()
        for who in ['alice', 'bob']:
            self.assertEqual(self.req(who, 'GET', '/admin/users').status_code, 403)
            self.assertEqual(self.req(who, 'PUT', '/admin/users/' + ALICE['id'] + '/permissions', json={'policy': 'allowed'}).status_code, 403)
            self.assertEqual(self.req(who, 'PUT', '/admin/upload-requests/' + self.row['id'], json={'status': 'approved'}).status_code, 403)
        self.assertEqual(len(self.req('alice', 'GET', '/upload-requests').json()['requests']), 1)
        self.assertEqual(self.req('bob', 'GET', '/upload-requests').json()['requests'], [])
        self.assertEqual(self.req('admin', 'GET', '/admin/users').json()['users'][0]['pending'], 1)

    def test_policy_change_blocks_even_approved_uploads(self):
        self.prepare()
        self.storage.tables['qa_upload_requests'][0]['status'] = 'approved'
        response = self.req('admin', 'PUT', '/admin/users/' + ALICE['id'] + '/permissions', json={'policy': 'blocked'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.req('alice', 'POST', f'/domains/{self.key}/ingest', files={'file': ('private.pdf', self.pdf)}).status_code, 403)
        response = self.req('alice', 'POST', '/upload-requests', json={k: self.row[k] for k in ['domain', 'filename', 'file_hash', 'size']})
        self.assertEqual(response.status_code, 403)

    def test_rejection_reason_optional_and_decisions_cannot_be_overwritten(self):
        self.prepare()
        path = '/admin/upload-requests/' + self.row['id']
        result = self.req('admin', 'PUT', path, json={'status': 'rejected'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['reason'], '')
        self.assertEqual(self.req('admin', 'PUT', path, json={'status': 'rejected', 'reason': 'Wrong document'}).status_code, 409)
        self.assertEqual(self.req('admin', 'PUT', path, json={'status': 'approved'}).status_code, 409)

    def test_request_identity_is_server_owned_and_domain_must_belong_to_user(self):
        self.prepare()
        body = {k: self.row[k] for k in ['domain', 'filename', 'file_hash', 'size']}
        self.assertEqual(self.req('bob', 'POST', '/upload-requests', json=body).status_code, 404)
        # Simulate insert representation; verify spoofed owner/status never reach storage.
        with patch.object(admin_panel, 'policy', return_value='approval_required'), patch.object(admin_panel.cloud, '_request') as write:
            write.return_value.json.return_value = [self.row]
            response = self.req('alice', 'POST', '/upload-requests', json={**body, 'user_id': BOB['id'], 'status': 'approved'})
            self.assertEqual(response.status_code, 200)
            sent = write.call_args.kwargs['json']
            self.assertEqual(sent['user_id'], ALICE['id'])
            self.assertNotIn('status', sent)

    def test_admin_upload_remains_independent_of_approval_storage(self):
        with patch.object(admin_panel, 'policy', side_effect=AssertionError('Admin must bypass approvals')):
            self.upload('admin')


if __name__ == '__main__':
    unittest.main()
