import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException

@contextmanager
def authenticated_api():
    async def authenticate(request):
        if request.cookies.get('qa_access') != 'test-access':
            raise HTTPException(401, 'Please sign in.')
        request.state.user = {'id': 'local-admin', 'role': 'admin', 'name': 'Test Admin', 'email': ''}
        return request.state.user
    with patch('api_server.user_auth.configured', return_value=True), patch('api_server.user_auth.authenticate', side_effect=authenticate):
        yield
def complete_mfa(client, response, owner, headers):
    from rag import mfa, security_store

    if response.status_code in (200, 201) and response.json().get("mfa_required"):
        state, _ = security_store.read("factor:" + owner)
        step = max(int(time.time()) // 30, state.get("last_step", -1) + 1)
        code = mfa.code_for(mfa.secret(owner, state["nonce"]), step)
        with patch.object(mfa, "time", SimpleNamespace(time=lambda: step * 30)):
            return client.post("/api/auth/mfa/verify", headers=headers, json={"code": code})
    return response
