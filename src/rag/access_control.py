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
    "logs": "Logs Management",
    "revisions": "Revision Management",
}
ADMIN_ONLY = {("accounts", "view"), ("approvals", "view"), ("approvals", "edit")}
USER_ROLE_MODULES = {
    "accounts": {"accounts.view"},
    "approvals": {"approvals.view", "approvals.edit"},
    "user_uploads": {"user_uploads.add"},
}
APPROVAL_MANAGEMENT = {("approvals", "add"), ("approvals", "delete")}
DELEGATED_UPLOADS = {("user_uploads", action) for action in ACTIONS}


def managed_admin_active():
    rows = cloud.rows("qa_role_settings", role="eq.admin", select="active")
    return not rows or rows[0]["active"] is True


def defaults(role):
    return {
        f"{resource}.{action}": (
            role == "super_admin" and (resource, action) in SUPER_ACTIONS
            or resource in {"bases", "documents"}
            or role == "admin" and (resource, action) in ADMIN_ONLY | {("accounts", "edit"), ("user_uploads", "add")}
        )
        for resource in RESOURCES for action in ACTIONS
    }


SUPPORTED = ADMIN_ONLY | APPROVAL_MANAGEMENT | DELEGATED_UPLOADS | {
    ("accounts", "add"), ("accounts", "edit"), ("accounts", "delete"),
    *((resource, action) for resource in ("permissions", "logs", "revisions") for action in ACTIONS),
} | {(resource, action) for resource in ("bases", "documents", "roles") for action in ACTIONS}
SUPER_ACTIONS = SUPPORTED


def available(role, key):
    resource, _, action = key.partition(".")
    return role in {"admin", "user"} and (resource, action) in SUPPORTED


def module_available(role, resource):
    return any(available(role, f"{resource}.{action}") for action in ACTIONS)


def module_enabled(role, resource, modules, override=None):
    default = role != "user" or resource not in USER_ROLE_MODULES
    return modules.get(resource) is not False and (
        override if override is not None else modules.get(resource, default)
    )


def scope_entries(permissions):
    return [key.removeprefix("scope:") for key, value in permissions.items()
            if key.startswith("scope:") and value is True]


def scoped_to(actor, target):
    return actor["role"] == "super_admin" or (
        target["id"] not in {actor["id"], "local-admin"}
        and target.get("role") in {"admin", "user"}
        and (target["id"] in actor.get("account_scope", ())
             or "role:" + target["role"] in actor.get("account_scope", ()))
    )


def policy(role):
    return security_store.read("policy:" + role)


def original_admin_permissions():
    return security_store.read("account:local-admin")


def original_admin_status():
    return security_store.read("status:local-admin")


def effective(user, settings=None, *, account_overrides=None):
    role = user["role"]
    values = defaults(role)
    if role == "super_admin":
        return values
    if settings is None:
        settings, _ = policy(role)
    overrides = user.get("permissions", {})
    if account_overrides is not None:
        overrides = account_overrides
    elif user["id"] == "local-admin":
        overrides, _ = original_admin_permissions()
    modules = settings.get("modules", {})
    actions = settings.get("actions", {})
    delegated = set(user.get("delegated_overrides", ()))
    blocked_overrides = {key for key in delegated
                         if actions.get(key) is False and key not in settings.get("delegated_actions", ())}
    if "accounts.edit" not in actions and "accounts.edit" not in overrides and role == "admin":
        actions = {**actions, "accounts.edit": overrides.get("approvals.edit", actions.get("approvals.edit", True))}
    return {
        key: available(role, key)
        and module_enabled(role, resource, modules, overrides.get("module:" + resource))
        and key not in blocked_overrides
        and overrides.get(key, actions.get(key, enabled or
                          role == "user" and key in USER_ROLE_MODULES.get(resource, ())))
        for key, enabled in values.items()
        for resource in (key.partition(".")[0],)
    }


def allows(user, key):
    if user["role"] == "super_admin":
        return True
    value = user.get("permissions", {}).get(key)
    return available(user["role"], key) and (
        value is True if value is not None else defaults(user["role"]).get(key, False)
    )


def grant_scope(user):
    return {f"{resource}.{action}" for resource, action in SUPPORTED if allows(user, f"{resource}.{action}")}


def check_delegation(user, before, after, *, modules=False, delegated=()):
    if user["role"] == "super_admin":
        return
    scope = grant_scope(user)
    for key in before.keys() | after.keys():
        if before.get(key) == after.get(key):
            continue
        if before.get(key) is False and key not in delegated:
            raise HTTPException(403, "This restriction is controlled by Super Admin.")
        if modules or key.startswith("module:"):
            resource = key.removeprefix("module:")
            required = {action for action in defaults("user") if available("user", action)
                        and action.startswith(resource + ".")}
        else:
            required = {key}
        if not required or not required <= scope:
            raise HTTPException(403, "This change is outside your delegated permission scope.")


def authorize_user_uploads(user, path, method):
    action = None
    if method == "GET" and (path == "/api/domains" or re.fullmatch(
        r"/api/domains/[^/]+/(status|documents/[^/]+/(chunks|pdf|text))", path
    )) or method == "POST" and re.fullmatch(r"/api/domains/[^/]+/(ask|search)", path):
        action = "view"
    elif method == "POST" and re.fullmatch(r"/api/domains/[^/]+/(ingest|documents/[^/]+/step)", path):
        action = "add"
    elif method == "PUT" and re.fullmatch(r"/api/domains/[^/]+/documents/[^/]+/chunks", path):
        action = "edit"
    elif method == "DELETE" and re.fullmatch(r"/api/domains/[^/]+/documents/[^/]+", path):
        action = "delete"
    if action is None or not all(
        allows(user, key) for key in ("user_uploads.view", f"user_uploads.{action}")
    ):
        raise HTTPException(403, "You do not have access to this user upload action.")


def allows_direct_upload(user):
    if user.get("direct_upload_restricted"):
        return False
    if allows(user, "user_uploads.add"):
        return True
    return None


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
            required = ["bases.view", "documents.add"]
        elif "/documents/" in path:
            required = ["bases.view", f"documents.{action}"]
    elif path == "/api/uploads" or path.startswith("/api/upload-requests"):
        required = [f"documents.{action}"]
    elif path.startswith("/api/admin/users"):
        required = ["accounts.view", "accounts.edit"] if method == "PUT" else ["accounts.view"]
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
            required = ["approvals.view", "approvals.edit"]

    if any(not allows(user, key) for key in required):
        raise HTTPException(403, "This action is disabled for your account.")
