"""Compatibility entrypoint: always use the authenticated, account-scoped API.

Run ``uvicorn api:app`` or ``uvicorn api_server:app``; both expose the current
API contract. The obsolete unauthenticated shared-workspace routes are removed.
"""
from api_server import app  # noqa: F401
