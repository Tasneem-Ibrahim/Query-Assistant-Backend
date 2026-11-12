"""Request-local ownership. HTTP callers bind only a verified server-side identity.

The default is the legacy admin workspace for the local CLI/Streamlit entrypoints.
Never bind an identity from request parameters or mutate global configuration per user.
"""
from contextvars import ContextVar
from uuid import UUID
import re

_owner = ContextVar("workspace_owner", default="admin")


def owner_id():
    return _owner.get()


def bind(user):
    if user.get("role") == "admin" and user.get("id") == "local-admin":
        owner = "admin"
    else:
        owner = str(UUID(user["id"]))
    return _owner.set(owner)


def reset(token):
    _owner.reset(token)


def suffix():
    return "" if owner_id() == "admin" else "user_" + UUID(owner_id()).hex


def data_dir():
    from . import config
    return config.DATA_DIR / "workspaces" / suffix() if suffix() else config.DATA_DIR


def pdf_root():
    from . import config
    return config.PDF_DIR / "workspaces" / suffix() if suffix() else config.PDF_DIR


def cache_dir():
    from . import config
    return data_dir() / "cache" if suffix() else config.CACHE_DIR


def registry_file():
    from . import config
    return data_dir() / "registry.json" if suffix() else config.REGISTRY_FILE


def local_collection(key):
    return f"{suffix()}_{key}" if suffix() else key


def owns_namespace(namespace):
    return bool(re.fullmatch(r"api_[0-9a-f]{16}" + ("_" + suffix() if suffix() else ""), namespace))
