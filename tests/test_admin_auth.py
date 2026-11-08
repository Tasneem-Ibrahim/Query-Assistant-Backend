"""Authentication must guard reads, paid operations and destructive operations."""
import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_server
from rag import config


def credentials(name='Admin', password='test-password'):
    return {'Authorization': 'Basic ' + base64.b64encode(f'{name}:{password}'.encode()).decode()}


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.multiple(config, ADMIN_USERNAME='Admin', ADMIN_PASSWORD='test-password')
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.client = TestClient(api_server.app)

    def test_missing_credentials_block_every_route_before_work(self):
        for method, path in [('GET', '/api/domains'), ('GET', '/api/health'),
                             ('GET', '/docs'), ('GET', '/openapi.json'),
                             ('POST', '/api/domains/customer_support/ask'),
                             ('POST', '/api/domains/customer_support/ingest'),
                             ('DELETE', '/api/domains/custom_example'),
                             ('DELETE', '/api/domains/customer_support/documents/example'),
                             ('POST', '/api/domains/customer_support/documents/example/step')]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path).status_code, 401)

    def test_correct_login_and_logout_credentials(self):
        response = self.client.get('/api/auth/session', headers=credentials())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'name': 'Admin'})
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)

    def test_wrong_or_malformed_credentials(self):
        for header in ['Basic !!!', 'Bearer token', credentials(password='wrong')['Authorization'], credentials(name='admin')['Authorization']]:
            self.assertEqual(self.client.get('/api/auth/session', headers={'Authorization': header}).status_code, 401)

    def test_unconfigured_password_fails_closed(self):
        with patch.object(config, 'ADMIN_PASSWORD', ''):
            self.assertEqual(self.client.get('/api/domains', headers=credentials()).status_code, 503)

    def test_preflight_and_unauthorized_responses_have_cors(self):
        origin = config.CORS_ORIGINS[0]
        response = self.client.options('/api/domains', headers={'Origin': origin,
            'Access-Control-Request-Method': 'DELETE', 'Access-Control-Request-Headers': 'authorization'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['access-control-allow-origin'], origin)
        response = self.client.get('/api/domains', headers={'Origin': origin})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['access-control-allow-origin'], origin)


if __name__ == '__main__':
    unittest.main()
