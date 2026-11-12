"""Authentication contracts: Supabase password flow, cookies, expiry, CSRF and RAG gate."""
import base64
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_server
from rag import config, user_auth

USER = {"id": "50c0eb32-2055-42ca-98d4-1dbe793f1ef8", "email": "person@example.com", "user_metadata": {"name": "Test Person"}}
CSRF = {"X-CSRF-Protection": "1", "Origin": "http://localhost:3000"}


def token(age=0, lifetime=900):
    now = int(time.time()) - age
    payload = base64.urlsafe_b64encode(json.dumps({"iat": now, "exp": now + lifetime, "sub": USER["id"]}).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


class UserAuthTests(unittest.TestCase):
    def setUp(self):
        settings = patch.multiple(config, SUPABASE_URL='https://supabase.example', SUPABASE_ANON_KEY='public-key',
            AUTH_COOKIE_SECRET='test-secret', AUTH_COOKIE_SECURE=False, AUTH_COOKIE_SAMESITE='lax',
            CORS_ORIGINS=['http://localhost:3000'])
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
                if method == 'PUT':
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
        return self.client.post('/api/auth/login', headers=CSRF, json={'email': USER['email'], 'password': 'test-password'})

    def test_signup_login_and_profile_session(self):
        response = self.client.post('/api/auth/signup', headers=CSRF,
            json={'name': 'Test Person', 'email': USER['email'], 'password': 'test-password'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['user']['name'], 'Test Person')
        self.assertNotIn('access_token', response.text)
        self.assertNotIn('refresh_token', response.text)
        signup = self.provider_calls[0]
        self.assertEqual(signup[2]['json']['data'], {'name': 'Test Person'})
        self.assertEqual(self.client.get('/api/auth/session').json()['user']['id'], USER['id'])

    def test_cookies_http_only_and_access_lasts_15_minutes(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(response.json()['expires_in'], 900)
        for cookie in response.headers.get_list('set-cookie'):
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=lax', cookie)
            self.assertIn('Path=/api', cookie)
        self.assertIn('Max-Age=900', response.headers.get_list('set-cookie')[0])
        self.assertIn('Max-Age=604800', response.headers.get_list('set-cookie')[1])
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
