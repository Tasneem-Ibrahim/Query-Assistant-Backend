"""Account permissions and workspace selection for the HTTP entry points."""
import re

from fastapi import HTTPException

from . import cloud, security_store

ACTIONS = ("view", "add", "edit", "delete")
RESOURCES = {
    "accounts": "User details",
    "bases": "Knowledge bases",
    "documents": "PDFs and images",
    "approvals": "Upload approvals",
    "user_uploads": "User uploads",
    "roles": "Role management",
    "permissions": "Permission matrix",
    "logs": "Login logs",
}
ADMIN_ONLY = {("accounts", "view"), ("approvals", "view"), ("approvals", "edit")}
USER_ROLE_MODULES = {
    "accounts": {"accounts.view"},
    "approvals": {"approvals.view", "approvals.edit"},
    "user_uploads": {"user_uploads.add"},
}
APPROVAL_MANAGEMENT = {("approvals", "add"), ("approvals", "delete")}
DELEGATED_UPLOADS = {("user_uploads", action) for action in ("view", "add", "delete")}


def managed_admin_active():
    rows = cloud.rows("qa_role_settings", role="eq.admin", select="active")
    return not rows or rows[0]["active"] is True


def defaults(role):
    return {
        f"{resource}.{action}": (
            (role == "super_admin" and (resource != "logs" or action == "view")) or resource in {"bases", "documents"}
            or (role == "admin" and (resource, action) in ADMIN_ONLY)
        )
        for resource in RESOURCES for action in ACTIONS
    }


def available(role, key):
    resource, _, action = key.partition(".")
    return action in ACTIONS and (resource in {"bases", "documents"} or
                                  (role == "admin" and (resource, action) in ADMIN_ONLY | APPROVAL_MANAGEMENT | DELEGATED_UPLOADS))


def module_available(role, resource):
    return (role == "user" and resource in USER_ROLE_MODULES) or any(
        available(role, f"{resource}.{action}") for action in ACTIONS
    )


def policy(role):
    return security_store.read("policy:" + role)


def original_admin_permissions():
    return security_store.read("account:local-admin")


def original_admin_status():
    return security_store.read("status:local-admin")


def effective(user, settings=None):
    role = user["role"]
    values = defaults(role)
    if role == "super_admin":
        return values
    if user["id"] == "local-admin":
        overrides, _ = original_admin_permissions()
        return {**values, **overrides}
    if settings is None:
        settings, _ = policy(role)
    overrides = user.get("permissions", {})
    modules = settings.get("modules", {})
    permissions = {key: available(role, key) and modules.get(key.split(".")[0], True)
            and overrides.get(key, settings.get("actions", {}).get(key, enabled))
            for key, enabled in values.items()}
    if role == "user":
        for module, keys in USER_ROLE_MODULES.items():
            for key in keys:
                permissions[key] = modules.get(module) is True
    return permissions


def allows(user, key):
    if user["role"] == "super_admin":
        return True
    resource, _, action = key.partition(".")
    if user["role"] == "user" and key in USER_ROLE_MODULES.get(resource, ()):
        return user.get("permissions", {}).get(key) is True
    enabled = resource in {"bases", "documents"} or (resource, action) in ADMIN_ONLY
    return available(user["role"], key) and user.get("permissions", {}).get(key, enabled) is True


def authorize_user_uploads(user, path, method):
    action = None
    if method == "GET" and (path == "/api/domains" or re.fullmatch(r"/api/domains/[^/]+/status", path)):
        action = "view"
    elif method == "POST" and re.fullmatch(r"/api/domains/[^/]+/(ingest|documents/[^/]+/step)", path):
        action = "add"
    elif method == "DELETE" and re.fullmatch(r"/api/domains/[^/]+/documents/[^/]+", path):
        action = "delete"
    if user["role"] != "admin" or action is None or not all(
        allows(user, key) for key in ("accounts.view", "user_uploads.view", f"user_uploads.{action}")
    ):
        raise HTTPException(403, "You do not have access to this user upload action.")


def allows_direct_upload(user):
    return user["role"] == "user" and allows(user, "user_uploads.add")


def authorize(user, path, method):
    if user["role"] == "super_admin":
        return
    if user.get("access_suspended") and path not in {"/api/auth/session", "/api/health"}:
        raise HTTPException(403, "Admin workspace access is paused. Contact Super Admin.")
    action = {"GET": "view", "HEAD": "view", "POST": "add",
              "PATCH": "edit", "PUT": "edit", "DELETE": "delete"}.get(method)
    required = []
    if path == "/api/domains" or re.fullmatch(r"/api/domains/[^/]+", path):
        required = [f"bases.{action}"]
        if action == "delete":
            required.append("documents.delete")
    elif path.startswith("/api/domains/"):
        if path.endswith(("/search", "/ask", "/status")):
            required = ["bases.view", "documents.view"]
        elif path.endswith("/reset"):
            required = ["bases.delete", "documents.delete"]
        elif path.endswith(("/ingest", "/step")):
            required = ["documents.add"]
        elif "/documents/" in path:
            required = [f"documents.{action}"]
    elif path == "/api/uploads" or path.startswith("/api/upload-requests"):
        required = [f"documents.{action}"]
    elif path.startswith("/api/admin/users"):
        required = ["approvals.edit" if method == "PUT" else "accounts.view"]
        if path == "/api/admin/users":
            if not any(allows(user, key) for key in ("accounts.view", "approvals.view")):
                raise HTTPException(403, "This action is disabled for your account.")
            required = []
        elif path.endswith("/activity"):
            required.extend(("approvals.view", "bases.view", "documents.view"))
    elif path.startswith("/api/admin/upload-requests"):
        if path.startswith("/api/admin/upload-requests/for/"):
            required = ["accounts.view", "approvals.view", "approvals.add"]
        elif method == "DELETE":
            required = ["approvals.view", "approvals.delete"]
        else:
            required = ["approvals.edit"]

    if any(not allows(user, key) for key in required):
        raise HTTPException(403, "This action is disabled for your account.")
