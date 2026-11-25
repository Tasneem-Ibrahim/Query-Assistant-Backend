"""Request-local ownership. HTTP callers bind only a verified server-side identity.

The default is the legacy admin workspace for the local CLI/Streamlit entrypoints.
Never bind an identity from request parameters or mutate global configuration per user.
"""
from collections import Counter
from contextvars import ContextVar
from threading import Lock
from uuid import UUID
import re

_actor = ContextVar("workspace_actor", default="")

def bind_actor(user):
    return _actor.set(user["role"])


def reset_actor(token):
    _actor.reset(token)


def privileged_actor():
    return _actor.get() in {"admin", "super_admin"}


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


# Keep local ingestion and HTTP mutations from racing a workspace purge in this process.
_activity_lock = Lock()
_writers = Counter()
_deleting = set()


def begin_write(owner):
    with _activity_lock:
        if owner in _deleting:
            return False
        _writers[owner] += 1
        return True


def end_write(owner):
    with _activity_lock:
        _writers[owner] -= 1
        if not _writers[owner]:
            del _writers[owner]


def begin_delete(owner):
    with _activity_lock:
        if owner in _deleting or _writers[owner]:
            return False
        _deleting.add(owner)
        return True


def end_delete(owner):
    with _activity_lock:
        _deleting.remove(owner)
