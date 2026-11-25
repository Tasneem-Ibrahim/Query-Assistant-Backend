import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_server
from rag import config, mfa, user_auth
from auth_helpers import complete_mfa

HEADERS = {"X-CSRF-Protection": "1"}

class LocalAdminTests(unittest.TestCase):
    def test_unified_login_uses_original_admin_credentials_without_supabase(self):
        with patch.object(user_auth, 'provider', side_effect=AssertionError('Admin must not require Supabase')):
            response = self.client.post('/api/auth/login', headers=HEADERS,
                json={'identity': config.ADMIN_USERNAME, 'password': config.ADMIN_PASSWORD, 'role': 'user'})
            response = complete_mfa(self.client, response, 'local-admin', HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['user']['role'], 'admin')
        self.assertEqual(self.client.get('/api/auth/session').json()['user']['id'], 'local-admin')

    def test_unified_bad_admin_credentials_never_fall_back_to_supabase(self):
        with patch.object(user_auth, 'provider', side_effect=AssertionError('No provider fallback')):
            for identity in [config.ADMIN_USERNAME, 'unknown-user']:
                response = self.client.post('/api/auth/login', headers=HEADERS,
                    json={'identity': identity, 'password': 'incorrect'})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()['detail'], 'Email or username, or password is incorrect.')
                self.assertFalse(response.headers.get_list('set-cookie'))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        settings = patch.multiple(config, ADMIN_USERNAME='Admin', ADMIN_PASSWORD='original-password',
            AUTH_COOKIE_SECRET='test-cookie-secret', AUTH_COOKIE_SECURE=False, AUTH_COOKIE_SAMESITE='lax',
            DATA_DIR=Path(folder.name), CLOUD_MODE=False, SUPABASE_URL='', SUPABASE_ANON_KEY='', SUPABASE_SERVICE_ROLE_KEY='')
        settings.start()
        self.addCleanup(settings.stop)
        provider = patch.object(user_auth, 'provider', side_effect=AssertionError('Admin must not contact Supabase'))
        provider.start()
        self.addCleanup(provider.stop)
        self.client = TestClient(api_server.app)
        self.addCleanup(self.client.close)

    def login(self, **changes):
        response = self.client.post('/api/auth/admin/login', headers=HEADERS,
            json={'username': 'Admin', 'password': 'original-password', **changes})
        return complete_mfa(self.client, response, 'local-admin', HEADERS)

    def test_existing_admin_login_works_without_supabase(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['role'], 'admin')
        self.assertTrue(response.json()['user']['onboarding_seen'])
        self.assertEqual(self.client.post('/api/auth/onboarding/complete', headers=HEADERS).status_code, 200)
        self.assertEqual(response.json()['expires_in'], 900)
        self.assertNotIn('original-password', response.text)
        self.assertEqual(self.client.get('/api/auth/session').json()['user']['name'], 'Admin')
        with patch.object(api_server, 'list_domains', return_value=[]):
            self.assertEqual(self.client.get('/api/domains').status_code, 200)

    def test_incorrect_and_case_changed_credentials_fail(self):
        self.assertEqual(self.login(password='wrong').status_code, 401)
        self.assertEqual(self.login(username='admin').status_code, 401)
        self.assertEqual(self.client.get('/api/domains').status_code, 401)

    def test_refresh_rotates_and_revokes_previous_token(self):
        initial = self.login().json()
        old = dict(self.client.cookies)
        self.client.cookies.delete(user_auth.ACCESS)
        response = self.client.post('/api/auth/refresh', headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['session_expires_at'], initial['session_expires_at'])
        self.assertNotEqual(self.client.cookies.get(user_auth.REFRESH), old[user_auth.REFRESH])
        self.assertEqual(self.client.get('/api/auth/session').status_code, 200)
        with TestClient(api_server.app) as replay:
            replay.cookies.update(old)
            self.assertEqual(replay.post('/api/auth/refresh', headers=HEADERS).status_code, 401)

    def test_logout_revokes_copied_session(self):
        self.login()
        old = dict(self.client.cookies)
        self.assertEqual(self.client.post('/api/auth/logout', headers=HEADERS).status_code, 200)
        self.assertFalse(self.client.cookies)
        with TestClient(api_server.app) as replay:
            replay.cookies.update(old)
            self.assertEqual(replay.get('/api/auth/session').status_code, 401)
            self.assertEqual(replay.post('/api/auth/refresh', headers=HEADERS).status_code, 401)

    def test_tampered_access_and_changed_admin_password_fail(self):
        self.login()
        saved = dict(self.client.cookies)
        self.client.cookies.clear()
        self.client.cookies.update({**saved, user_auth.ACCESS: saved[user_auth.ACCESS] + 'x'})
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)
        self.client.cookies.clear()
        self.client.cookies.update(saved)
        with patch.object(config, 'ADMIN_PASSWORD', 'changed-password'):
            self.assertEqual(self.client.get('/api/auth/session').status_code, 401)
            self.assertEqual(self.client.post('/api/auth/refresh', headers=HEADERS).status_code, 401)

    def test_disabled_admin_fails_closed(self):
        with patch.object(config, 'ADMIN_PASSWORD', ''):
            self.assertEqual(self.login().status_code, 503)

    def test_setup_gates_api_and_refresh_and_rejects_a_replayed_code(self):
        body = {'identity': config.ADMIN_USERNAME, 'password': config.ADMIN_PASSWORD}
        endpoint = '/api/auth/mfa/verify'
        first = self.client.post('/api/auth/login', headers=HEADERS, json=body)
        self.assertEqual(first.json()['mfa_required'], 'setup')
        self.assertEqual(self.client.get('/api/domains').status_code, 401)
        self.assertEqual(self.client.post('/api/auth/refresh', headers=HEADERS).status_code, 401)
        key = base64.b32decode(first.json()['setup_key'])
        # Use the forward window so crossing a time boundary does not make the code reusable.
        code = mfa.code_for(key, int(time.time()) // 30 + 1)
        self.assertEqual(self.client.post(endpoint, json={'code': code}).status_code, 403)
        self.assertEqual(self.client.post(endpoint, headers=HEADERS, json={'code': code}).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/session').status_code, 200)

        second = self.client.post('/api/auth/login', headers=HEADERS, json=body)
        self.assertEqual(second.json(), {'mfa_required': 'verify'})
        self.assertEqual(self.client.post(endpoint, headers=HEADERS, json={'code': code}).status_code, 400)
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)
