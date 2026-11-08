"""Storage failures must remain readable by a cross-origin frontend."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_server
from rag import cloud, config


class StorageErrorTests(unittest.TestCase):
    def setUp(self):
        self.auth = patch('api_server.admin_auth.authorized', return_value=True)
        self.auth.start()
        self.configured = patch('api_server.admin_auth.configured', return_value=True)
        self.configured.start()
        self.addCleanup(self.auth.stop)
        self.addCleanup(self.configured.stop)

    def test_connection_failure_is_safe_json_with_cors(self):
        origin = config.CORS_ORIGINS[0]
        with patch.object(config, 'SUPABASE_URL', 'https://storage.example'), \
             patch.object(config, 'SUPABASE_SERVICE_ROLE_KEY', 'private-key'), \
             patch.object(cloud._client, 'request', side_effect=httpx.ConnectError('private diagnostic')), \
             patch.object(api_server, 'list_domains', side_effect=lambda: cloud.rows('qa_domains')):
            response = TestClient(api_server.app).get('/api/domains', headers={'Origin': origin})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.headers['access-control-allow-origin'], origin)
        self.assertIn('Cannot reach Supabase', response.json()['detail'])
        self.assertNotIn('private', response.text)

    def test_provider_failure_does_not_expose_body(self):
        with patch.object(config, 'SUPABASE_URL', 'https://storage.example'), \
             patch.object(config, 'SUPABASE_SERVICE_ROLE_KEY', 'private-key'), \
             patch.object(cloud._client, 'request', return_value=httpx.Response(401, text='private diagnostic')):
            with self.assertRaisesRegex(cloud.CloudStorageError, 'HTTP 401') as error:
                cloud.rows('qa_domains')
        self.assertNotIn('private diagnostic', str(error.exception))
        self.assertNotIn('private-key', str(error.exception))


if __name__ == '__main__':
    unittest.main()
