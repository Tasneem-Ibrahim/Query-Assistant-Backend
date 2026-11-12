"""Account boundaries with real PDF parsing/Qdrant and mocked identity/Supabase.

No production credentials, network calls, or live database changes.
"""
import copy
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote

import httpx
import numpy as np
import pymupdf
from PIL import Image
from qdrant_client import QdrantClient
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_server
from rag import cloud, config, embed, ingest, vectorstore, workspace
from rag.domains import create_domain, get_domain, list_domains

ADMIN = {'id': 'local-admin', 'role': 'admin'}
ALICE = {'id': '11111111-1111-4111-8111-111111111111', 'role': 'user'}
BOB = {'id': '22222222-2222-4222-8222-222222222222', 'role': 'user'}
CSRF = {'X-CSRF-Protection': '1', 'Origin': 'http://localhost:3000'}


@contextmanager
def as_user(user):
    token = workspace.bind(user)
    try:
        yield
    finally:
        workspace.reset(token)


class SupabaseMemory:
    """REST predicate/lease emulator, deliberately uses real application scopes."""
    def __init__(self):
        self.tables = {'qa_domains': [], 'qa_documents': []}
        self.objects = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        path = url.split('https://supabase.test', 1)[1]
        params, body = kwargs.get('params', {}), kwargs.get('json', {})
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        if path.startswith('/storage/v1/object/'):
            key = unquote(path[len('/storage/v1/object/'):])
            if method == 'GET':
                return httpx.Response(200, content=self.objects[key]) if key in self.objects else httpx.Response(404)
            if method == 'POST':
                self.objects[key] = kwargs['content']
            if method == 'DELETE':
                for suffix in body['prefixes']:
                    self.objects.pop(key + '/' + suffix, None)
            return httpx.Response(200, json={})
        if '/rpc/' in path:
            found = next((row for row in self.tables['qa_documents']
                          if all(row.get(key) == body['p_' + arg]
                                 for key, arg in [('namespace', 'namespace'), ('domain', 'domain'), ('file_hash', 'hash')])), None)
            if not found:
                return httpx.Response(200, json=None)
            if path.endswith('qa_claim_document'):
                if found.get('lease_token'):
                    return httpx.Response(200, json=None)
                found['lease_token'] = body['p_token']
            elif path.endswith('qa_advance_document'):
                if found.get('lease_token') != body['p_token']:
                    return httpx.Response(200, json=None)
                found['next_page'] += 1
                found['chunks'] += body['p_chunks']
                found['image_pages'] += body['p_image']
                found.update(cached_chunks=None, lease_token=None)
                found['status'] = 'ready' if found['next_page'] == found['pages'] else 'pending'
            elif path.endswith('qa_release_document') and found.get('lease_token') == body['p_token']:
                found['lease_token'] = None
            return httpx.Response(200, json=copy.deepcopy(found))
        table = path.rsplit('/', 1)[1]
        rows = self.tables[table]
        def matches(row):
            for field, value in params.items():
                if isinstance(value, str) and value.startswith('eq.') and str(row.get(field)) != value[3:]:
                    return False
            return True
        if method == 'GET':
            offset = params.get('offset', 0)
            return httpx.Response(200, json=copy.deepcopy([r for r in rows if matches(r)][offset:offset + params.get('limit', 1000)]))
        if method == 'POST':
            conflict = params['on_conflict'].split(',')
            found = next((r for r in rows if all(r.get(k) == body[k] for k in conflict)), None)
            if found:
                if 'ignore-duplicates' not in kwargs.get('headers', {}).get('Prefer', ''):
                    found.update(body)
            else:
                found = copy.deepcopy(body)
                if table == 'qa_documents':
                    found.update(next_page=0, chunks=0, image_pages=0, cached_chunks=None, cached_image=False, status='pending', lease_token=None)
                rows.append(found)
            return httpx.Response(200, json=[copy.deepcopy(found)])
        found = [r for r in rows if matches(r)]
        if method == 'PATCH':
            for row in found:
                row.update(body)
        if method == 'DELETE':
            self.tables[table] = [r for r in rows if not matches(r)]
        return httpx.Response(200, json=copy.deepcopy(found))


class LocalWorkspaceTests(unittest.TestCase):
    cloud_mode = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        vectorstore.close_client()
        settings = patch.multiple(config, CLOUD_MODE=self.cloud_mode, DATA_DIR=root/'data',
            PDF_DIR=root/'pdfs', CACHE_DIR=root/'cache', REGISTRY_FILE=root/'registry.json',
            QDRANT_PATH=root/'vectors', QDRANT_URL='', SUPABASE_URL='https://supabase.test',
            SUPABASE_SERVICE_ROLE_KEY='test-key', CORS_ORIGINS=['http://localhost:3000'])
        settings.start()
        self.addCleanup(settings.stop)
        self.addCleanup(vectorstore.close_client)
        # Exercise real collections without native SQLite file handles on Windows.
        vectorstore._client = QdrantClient(location=":memory:")
        fake_embedder = Mock(dim=2)
        fake_embedder.embed_query.return_value = np.array([1., 0.])
        fake_embedder.embed_passages.side_effect = lambda texts: np.tile([1., 0.], (len(texts), 1))
        for target, kwargs in [(config, {'configuration_errors': Mock(return_value=[])}),
                               (embed, {'get_embedder': Mock(return_value=fake_embedder)})]:
            patched = patch.multiple(target, **kwargs)
            patched.start()
            self.addCleanup(patched.stop)
        self.storage = SupabaseMemory()
        transport = patch.object(cloud._client, 'request', side_effect=self.storage.request)
        transport.start()
        self.addCleanup(transport.stop)
        async def authenticate(request):
            identity = {'admin': ADMIN, 'alice': ALICE, 'bob': BOB}.get(request.cookies.get('test_identity'))
            if identity is None:
                raise HTTPException(401, 'Sign in.')
            request.state.user = identity
        auth = patch.object(api_server.user_auth, 'authenticate', side_effect=authenticate)
        auth.start()
        self.addCleanup(auth.stop)
        configured = patch.object(api_server.user_auth, 'configured', return_value=True)
        configured.start()
        self.addCleanup(configured.stop)
        self.client = TestClient(api_server.app)
        self.addCleanup(self.client.close)
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 80), 'Private account document about mango orchards and harvests.')
            self.pdf = pdf.tobytes()

    def req(self, identity, method, path, **kwargs):
        return self.client.request(method, '/api' + path, headers={**CSRF, 'Cookie': 'test_identity=' + identity}, **kwargs)

    def upload(self, identity, key='customer_support', name='private.pdf', content=None):
        response = self.req(identity, 'POST', f'/domains/{key}/ingest', files={'file': (name, self.pdf if content is None else content)}, data={'use_llm': 'false'})
        self.assertEqual(response.status_code, 200, response.text)
        if self.cloud_mode:
            for job in response.json()['jobs']:
                response = self.req(identity, 'POST', f'/domains/{key}/documents/{job["file_hash"]}/step')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['status'], 'ready')
        else:
            self.assertNotIn('"type": "error"', response.text)
        result = self.req(identity, 'GET', f'/domains/{key}/status')
        self.assertEqual(result.status_code, 200, result.text)
        return next(iter(result.json()['files']))

    def test_legacy_admin_data_is_preserved_and_new_accounts_start_empty(self):
        # Seed through legacy local entrypoints to prove the old paths stay valid.
        with as_user(ADMIN):
            original_namespace = config.embedding_namespace()
            custom = create_domain('Admin private base')
            original_collection = get_domain('customer_support').collection
        digest = self.upload('admin')
        self.assertEqual(self.req('admin', 'GET', f'/domains/customer_support/documents/{digest}/pdf').content, self.pdf)
        for identity, user in [('alice', ALICE), ('bob', BOB)]:
            response = self.req(identity, 'GET', '/domains')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(custom.key, [d['key'] for d in response.json()])
            status = self.req(identity, 'GET', '/domains/customer_support/status').json()
            self.assertEqual(status['files'], {})
            self.assertEqual(status['chunks'], 0)
            with as_user(user):
                self.assertNotEqual(config.embedding_namespace(), original_namespace)
                self.assertNotEqual(get_domain('customer_support').collection, original_collection)
        with as_user(ADMIN):
            self.assertEqual(get_domain(custom.key).label, 'Admin private base')
            self.assertEqual(config.embedding_namespace(), original_namespace)

    def test_direct_urls_cannot_read_mutate_or_search_another_accounts_base(self):
        custom = self.req('alice', 'POST', '/domains', json={'name': 'Private notes'}).json()
        key = custom['key']
        digest = self.upload('alice', key)
        for identity in ('bob', 'admin'):
            for method, suffix, body in [('GET', '/status', None), ('GET', f'/documents/{digest}/pdf', None),
                ('GET', f'/documents/{digest}/text', None), ('DELETE', f'/documents/{digest}', None),
                ('POST', '/ask', {'question': 'mango'}), ('POST', '/search', {'query': 'mango'}),
                ('POST', '/reset', {}), ('PATCH', '', {'name': 'stolen'}), ('DELETE', '', None),
                ('POST', '/ingest', None)]:
                response = self.req(identity, method, f'/domains/{key}' + suffix, **({'json': body} if body is not None else {}))
                self.assertEqual(response.status_code, 404, (identity, method, suffix, response.text))
        self.assertEqual(self.req('alice', 'GET', f'/domains/{key}/documents/{digest}/pdf').content, self.pdf)

    def test_builtin_ids_and_identical_files_are_scoped_including_vectors(self):
        digest = self.upload('alice')
        root = f'/domains/customer_support/documents/{digest}'
        for method, suffix in [('GET', '/pdf'), ('GET', '/text'), ('DELETE', '')]:
            response = self.req('bob', method, root + suffix)
            self.assertEqual(response.status_code, 404, response.text)
        step = self.req('bob', 'POST', root + '/step')
        self.assertIn(step.status_code, (400, 404))
        search = self.req('bob', 'POST', '/domains/customer_support/search', json={'query': 'mango'})
        self.assertEqual(search.json()['hits'], [])
        ask = self.req('bob', 'POST', '/domains/customer_support/ask', json={'question': 'mango'})
        self.assertEqual(ask.json()['sources'], [])
        self.assertEqual(self.upload('bob'), digest)
        self.assertTrue(self.req('alice', 'POST', '/domains/customer_support/search', json={'query': 'mango'}).json()['hits'])
        self.assertEqual(self.req('alice', 'DELETE', root).status_code, 200)
        self.assertEqual(self.req('bob', 'GET', root + '/pdf').content, self.pdf)
        self.assertTrue(self.req('bob', 'POST', '/domains/customer_support/search', json={'query': 'mango'}).json()['hits'])
        self.assertEqual(self.req('alice', 'GET', '/domains/customer_support/status').json()['files'], {})
        if self.cloud_mode:
            namespaces = {r['namespace'] for r in self.storage.tables['qa_documents']}
            self.assertEqual(len(namespaces), 1)
            self.assertIn('user_' + BOB['id'].replace('-', ''), next(iter(namespaces)))

    def test_same_base_names_and_parallel_requests_do_not_share_registries(self):
        keys = {}
        for identity in ('admin', 'alice', 'bob'):
            response = self.req(identity, 'POST', '/domains', json={'name': 'Same name', 'owner_id': 'admin'})
            self.assertEqual(response.status_code, 201, response.text)
            keys[identity] = response.json()['key']
        def verify(identity):
            response = self.req(identity, 'GET', '/domains')
            own = [d['key'] for d in response.json() if d['custom']]
            self.assertEqual(own, [keys[identity]])
            return identity
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(len(list(pool.map(verify, ['admin', 'alice', 'bob'] * 5))), 15)
        self.assertEqual(workspace.owner_id(), 'admin')

    def test_image_ocr_previews_are_private(self):
        buffer = io.BytesIO()
        Image.new('RGB', (300, 200), 'white').save(buffer, format='PNG')
        with patch('rag.extract.ocr_image', return_value='Private image transcript.'), patch.object(cloud, '_ocr_page', return_value='Private image transcript.'):
            digest = self.upload('alice', name='private.png', content=buffer.getvalue())
        path = f'/domains/customer_support/documents/{digest}/text'
        response = self.req('alice', 'GET', path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['text'], 'Private image transcript.')
        for identity in ('admin', 'bob'):
            self.assertEqual(self.req(identity, 'GET', path).status_code, 404)

    def test_owner_can_rename_and_delete_only_own_base(self):
        bases = {identity: self.req(identity, 'POST', '/domains', json={'name': 'Own base'}).json()['key']
                 for identity in ('admin', 'alice', 'bob')}
        key = bases['alice']
        self.upload('alice', key)
        self.assertEqual(self.req('alice', 'PATCH', f'/domains/{key}', json={'name': 'Renamed'}).status_code, 200)
        self.assertEqual(self.req('alice', 'DELETE', f'/domains/{key}').status_code, 200)
        for identity in ('admin', 'bob'):
            domains = self.req(identity, 'GET', '/domains').json()
            self.assertIn(bases[identity], [d['key'] for d in domains])
        self.assertEqual(self.req('alice', 'GET', f'/domains/{key}/status').status_code, 404)

    def test_invalid_identity_cannot_fall_back_to_admin(self):
        async def broken(request):
            request.state.user = {'id': '../../admin', 'role': 'user'}
        with patch.object(api_server.user_auth, 'authenticate', side_effect=broken):
            self.assertEqual(self.req('alice', 'GET', '/domains').status_code, 401)

    def test_legacy_entrypoint_has_no_authentication_bypass(self):
        import api
        self.assertIs(api.app, api_server.app)
        with TestClient(api.app) as client:
            self.assertEqual(client.get('/api/domains').status_code, 401)
            self.assertEqual(client.post('/api/ask', headers=CSRF, json={'domain': 'customer_support', 'question': 'private'}).status_code, 401)

    def test_storage_and_vector_helpers_reject_foreign_workspace(self):
        with as_user(ADMIN):
            path = config.embedding_namespace() + '/customer_support/' + 'a' * 64 + '.pdf'
            collection = get_domain('customer_support').collection
        with as_user(ALICE):
            with self.assertRaises(ValueError):
                cloud.read_document(path)
            with self.assertRaises(ValueError):
                vectorstore.search(collection, np.array([1., 0.]), 5)
            with self.assertRaises(ValueError):
                vectorstore.reset_collection(collection)


class CloudWorkspaceTests(LocalWorkspaceTests):
    cloud_mode = True

    def test_missing_migration_fails_closed(self):
        with patch.object(cloud._client, 'request', return_value=httpx.Response(400, json={'message': 'owner_id does not exist'})):
            response = self.req('alice', 'GET', '/domains')
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('Admin private', response.text)


if __name__ == '__main__':
    unittest.main()
