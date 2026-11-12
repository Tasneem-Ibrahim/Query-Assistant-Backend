from contextlib import contextmanager
from unittest.mock import patch
from fastapi import HTTPException

@contextmanager
def authenticated_api():
    async def authenticate(request):
        if request.cookies.get('qa_access') != 'test-access':
            raise HTTPException(401, 'Please sign in.')
        request.state.user = {'id': 'test-user', 'name': 'Test User', 'email': 'test@example.com'}
        return request.state.user
    with patch('api_server.user_auth.configured', return_value=True), patch('api_server.user_auth.authenticate', side_effect=authenticate):
        yield
