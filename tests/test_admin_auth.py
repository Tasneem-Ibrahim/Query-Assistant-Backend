"""Authentication contracts: Supabase password flow, cookies, expiry, CSRF and RAG gate."""
import base64
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_server
from rag import access_control, config, security_store, user_auth
from auth_helpers import complete_mfa

USER = {"id": "50c0eb32-2055-42ca-98d4-1dbe793f1ef8", "email": "person@example.com", "user_metadata": {"name": "Test Person"}}
CSRF = {"X-CSRF-Protection": "1", "Origin": "http://localhost:3000"}


def token(age=0, lifetime=900):
    now = int(time.time()) - age
    payload = base64.urlsafe_b64encode(json.dumps({"iat": now, "exp": now + lifetime, "sub": USER["id"]}).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


class UserAuthTests(unittest.TestCase):
    def test_unified_email_login_role_is_not_client_controlled(self):
        self.user['user_metadata'].update({'role': 'admin', '_local_admin': True})
        response = self.client.post('/api/auth/login', headers=CSRF,
            json={'identity': '  ' + USER['email'] + '  ', 'password': 'test-password', 'role': 'admin'})
        response = complete_mfa(self.client, response, self.user['id'], CSRF)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['user']['role'], 'user')
        self.assertEqual(self.provider_calls[0][2]['json'], {'email': USER['email'], 'password': 'test-password'})
        self.assertEqual(self.client.get('/api/admin/users').status_code, 403)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        settings = patch.multiple(config, SUPABASE_URL='https://supabase.example', SUPABASE_ANON_KEY='public-key',
            AUTH_COOKIE_SECRET='test-secret', AUTH_COOKIE_SECURE=False, AUTH_COOKIE_SAMESITE='lax',
            CORS_ORIGINS=['http://localhost:3000'], DATA_DIR=Path(folder.name), CLOUD_MODE=False)
        settings.start()
        self.addCleanup(settings.stop)
        self.provider_calls = []
        self.user = copy.deepcopy(USER)
        self.counter = 0
        self.confirmation = False
        self.old_token = False
        self.fail_login = False
        self.fail_network = False
        async def provider(method, url, **kwargs):
            self.provider_calls.append((method, url, kwargs))
            if self.fail_network:
                raise httpx.ConnectError('private diagnostic')
            if url.endswith('/user'):
                if method == 'PUT' and 'data' in kwargs['json']:
                    self.user['user_metadata'].update(kwargs['json']['data'])
                return httpx.Response(200, json=self.user)
            if '/logout?' in url:
                return httpx.Response(204)
            if self.fail_login:
                return httpx.Response(400, json={'error_code': 'invalid_credentials'})
            if url.endswith('/signup') and self.confirmation:
                return httpx.Response(200, json=self.user)
            self.counter += 1
            return httpx.Response(200, json={'access_token': token(age=901 if self.old_token else 0, lifetime=3600),
                'refresh_token': 'refresh-' + str(self.counter), 'user': self.user, 'expires_in': 3600})
        transport = patch.object(user_auth._client, 'request', side_effect=provider)
        transport.start()
        self.addCleanup(transport.stop)
        self.client = TestClient(api_server.app)
        self.addCleanup(self.client.close)

    def login(self):
        response = self.client.post('/api/auth/login', headers=CSRF, json={'email': USER['email'], 'password': 'test-password'})
        return complete_mfa(self.client, response, self.user['id'], CSRF)

    def test_signup_login_and_profile_session(self):
        response = self.client.post('/api/auth/signup', headers=CSRF,
            json={'name': 'Test Person', 'email': USER['email'], 'password': 'test-password'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['mfa_required'], 'setup')
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)
        response = complete_mfa(self.client, response, self.user['id'], CSRF)
        self.assertEqual(response.json()['user']['name'], 'Test Person')
        self.assertNotIn('access_token', response.text)
        self.assertNotIn('refresh_token', response.text)
        signup = self.provider_calls[0]
        self.assertEqual(signup[2]['json']['data'], {'name': 'Test Person'})
        self.assertEqual(self.client.get('/api/auth/session').json()['user']['id'], USER['id'])

    def test_self_service_account_is_authenticated_and_updates_only_the_signed_in_user(self):
        self.assertEqual(self.client.get('/api/auth/account').status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get('/api/auth/account').json(), {'mfa_enabled': True})
        self.assertEqual(self.client.patch('/api/auth/account', json={'name': 'New Name'}).status_code, 403)
        response = self.client.patch('/api/auth/account', headers=CSRF, json={
            'name': ' New Name ', 'id': 'other-account', 'role': 'admin',
        })
        self.assertEqual(response.json(), {'name': 'New Name'})
        self.assertEqual(self.provider_calls[-1][2]['json'], {'data': {'name': 'New Name'}})
        self.assertEqual(self.client.get('/api/auth/session').json()['user']['role'], 'user')
        self.user['app_metadata'] = {'qa_access': {'role': 'admin'}}
        with patch.object(access_control, 'managed_admin_active', return_value=True):
            self.assertEqual(self.client.get('/api/auth/account').status_code, 403)

    def test_account_password_change_rechecks_password_and_revokes_verification_session(self):
        self.assertEqual(self.login().status_code, 200)
        path = '/api/auth/account/password'
        body = {'current_password': 'test-password', 'new_password': 'new-test-password'}
        invalid = self.client.post(path, headers=CSRF, json={**body, 'new_password': 'short'})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(body['current_password'], invalid.text)
        self.fail_login = True
        refused = self.client.post(path, headers=CSRF, json=body)
        self.assertEqual(refused.status_code, 400)
        self.assertIn('Current password is incorrect', refused.text)
        self.fail_login = False
        response = self.client.post(path, headers=CSRF, json=body)
        self.assertEqual(response.json(), {'ok': True})
        verify, update, revoke = self.provider_calls[-3:]
        self.assertEqual(verify[2]['json'], {'email': USER['email'], 'password': body['current_password']})
        self.assertEqual(update[2]['json'], {'password': body['new_password']})
        self.assertTrue(revoke[1].endswith('/logout?scope=local'))
        self.assertEqual(update[2]['headers']['Authorization'], revoke[2]['headers']['Authorization'])
        self.assertFalse(response.headers.get_list('set-cookie'))

    def test_cookies_http_only_and_access_lasts_15_minutes(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(response.json()['expires_in'], 900)
        for cookie in response.headers.get_list('set-cookie'):
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=lax', cookie)
            self.assertIn('Path=/api', cookie)
        cookies = {cookie.split('=', 1)[0]: cookie for cookie in response.headers.get_list('set-cookie')}
        self.assertIn('Max-Age=900', cookies[user_auth.ACCESS])
        self.assertIn('Max-Age=604800', cookies[user_auth.REFRESH])
        with patch.object(config, 'AUTH_COOKIE_SECURE', True):
            secure = self.login()
            self.assertTrue(all('Secure' in c for c in secure.headers.get_list('set-cookie')))

    def test_email_confirmation_does_not_create_browser_session(self):
        self.confirmation = True
        response = self.client.post('/api/auth/signup', headers=CSRF,
            json={'name': 'Person', 'email': USER['email'], 'password': 'test-password'})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['confirmation_required'])
        self.assertFalse(response.headers.get_list('set-cookie'))

    def test_invalid_credentials_and_provider_failure_are_safe(self):
        self.fail_login = True
        response = self.login()
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.client.cookies)
        self.fail_network = True
        response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private diagnostic', response.text)
        self.assertNotIn('test-secret', response.text)

    def test_input_validation(self):
        for body in [{'name': ' ', 'email': USER['email'], 'password': '12345678'},
                     {'name': 'Person', 'email': 'invalid', 'password': '12345678'},
                     {'name': 'Person', 'email': USER['email'], 'password': 'short'}]:
            self.assertEqual(self.client.post('/api/auth/signup', headers=CSRF, json=body).status_code, 422)
        self.assertEqual(self.provider_calls, [])

    def test_refresh_rotates_token_without_extending_absolute_session(self):
        login = self.login().json()
        before = self.client.cookies.get(user_auth.REFRESH)
        self.client.cookies.delete(user_auth.ACCESS)
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)
        response = self.client.post('/api/auth/refresh', headers=CSRF)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.client.cookies.get(user_auth.REFRESH), before)
        self.assertEqual(response.json()['session_expires_at'], login['session_expires_at'])
        self.assertEqual(self.client.get('/api/auth/session').status_code, 200)

    def test_expired_access_token_rejected_even_if_provider_accepts_it(self):
        self.login()
        self.client.cookies.set(user_auth.ACCESS, token(age=901, lifetime=3600), domain='testserver.local', path='/api')
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)

    def test_verified_session_is_bound_to_account(self):
        self.login()
        self.user['id'] = '55555555-5555-4555-8555-555555555555'
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)
        self.assertEqual(self.client.post('/api/auth/refresh', headers=CSRF).status_code, 401)

    def test_role_module_disable_applies_to_active_session_and_account_overrides(self):
        self.user['app_metadata'] = {'qa_access': {'permissions': {'bases.view': True}}}
        self.login()
        self.assertTrue(self.client.get('/api/auth/session').json()['user']['permissions']['bases.view'])
        self.assertTrue(security_store.write('policy:user', {'modules': {'bases': False}}, 0))
        self.assertEqual(self.client.get('/api/domains').status_code, 403)
        self.assertFalse(self.client.get('/api/auth/session').json()['user']['permissions']['bases.view'])
        _, revision = access_control.policy('user')
        self.assertTrue(security_store.write('policy:user', {'modules': {'bases': True}}, revision))
        self.assertNotEqual(self.client.get('/api/domains').status_code, 403)

    def test_session_expiry_and_tampering_rejected(self):
        for value in ['9999999999.forged', user_auth.sign_deadline(int(time.time()) - 1, 'refresh-1')]:
            self.client.cookies.clear()
            self.client.cookies.set(user_auth.REFRESH, 'refresh-1')
            self.client.cookies.set(user_auth.DEADLINE, value)
            self.assertEqual(self.client.post('/api/auth/refresh', headers=CSRF).status_code, 401)
        self.assertEqual(self.provider_calls, [])

    def test_rag_routes_and_documentation_are_protected(self):
        for method, path in [('GET', '/api/domains'), ('GET', '/api/health'), ('GET', '/docs'),
            ('GET', '/openapi.json'), ('POST', '/api/domains/example/ask'),
            ('POST', '/api/domains/example/ingest'), ('DELETE', '/api/domains/custom_example')]:
            self.assertEqual(self.client.request(method, path, headers=CSRF).status_code, 401)
        # Old shared-admin credentials cannot bypass Supabase authentication.
        self.assertEqual(self.client.get('/api/domains', headers={'Authorization': 'Basic QWRtaW46cGFzc3dvcmQ='}).status_code, 401)

    def test_csrf_and_credentialed_cors(self):
        self.assertEqual(self.client.post('/api/auth/login', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/auth/login', headers={**CSRF, 'Origin': 'https://evil.example'}, json={}).status_code, 403)
        response = self.client.options('/api/auth/login', headers={'Origin': CSRF['Origin'],
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,x-csrf-protection'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['access-control-allow-credentials'], 'true')
        self.assertEqual(response.headers['access-control-allow-origin'], CSRF['Origin'])

    def test_logout_revokes_session_and_clears_all_cookies(self):
        self.login()
        self.client.cookies.delete(user_auth.ACCESS)
        response = self.client.post('/api/auth/logout', headers=CSRF)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any('/logout?scope=local' in c[1] for c in self.provider_calls))
        self.assertFalse(self.client.cookies)
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)

    def test_missing_configuration_fails_closed(self):
        with patch.object(config, 'AUTH_COOKIE_SECRET', ''):
            self.assertEqual(self.login().status_code, 503)

    def test_welcome_completion_persists_on_account_and_preserves_profile(self):
        self.assertFalse(self.login().json()['user']['onboarding_seen'])
        self.assertFalse(self.client.get('/api/auth/session').json()['user']['onboarding_seen'])
        response = self.client.post('/api/auth/onboarding/complete', headers=CSRF,
                                    json={'owner_id': 'someone-else'})
        self.assertEqual(response.status_code, 200, response.text)
        update = next(c for c in self.provider_calls if c[0] == 'PUT')
        self.assertEqual(update[2]['json'], {'data': {'workspace_guide_seen': True}})
        self.assertEqual(update[2]['headers']['Authorization'], 'Bearer ' + self.client.cookies.get(user_auth.ACCESS))
        profile = self.client.get('/api/auth/session').json()['user']
        self.assertTrue(profile['onboarding_seen'])
        self.assertEqual(profile['name'], USER['user_metadata']['name'])
        self.assertEqual(profile['role'], 'user')
        self.assertTrue(self.login().json()['user']['onboarding_seen'])
        # A different account has independent metadata, even in the same browser.
        self.user = {**copy.deepcopy(USER), 'id': '22222222-2222-4222-8222-222222222222'}
        self.assertFalse(self.login().json()['user']['onboarding_seen'])

    def test_welcome_completion_requires_session_and_csrf(self):
        self.assertEqual(self.client.post('/api/auth/onboarding/complete', headers=CSRF).status_code, 401)
        self.assertFalse(self.provider_calls)
        self.login()
        self.assertEqual(self.client.post('/api/auth/onboarding/complete').status_code, 403)
        self.assertFalse(self.user['user_metadata'].get('workspace_guide_seen'))


if __name__ == '__main__':
    unittest.main()
