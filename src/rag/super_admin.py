"""Super Admin account administration. Only server-owned Auth metadata is trusted."""
import csv
import io
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from . import access_control, admin_panel, admin_sessions, cloud, config, revisions, saved_uploads, security_store, workspace
from .user_auth import SignupBody, sign_invite

router = APIRouter(prefix="/api/super-admin", tags=["Super Admin"])


def guard(request):
    if request.state.user["role"] != "super_admin":
        raise HTTPException(403, "Super Admin access required.")


def auth(method, path, *, endpoint="/admin/users", **kwargs):
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(503, "Account management requires a Supabase service-role configuration.")
    response = cloud._request(method, "/auth/v1" + endpoint + path, **kwargs)
    return response.json() if response.content else {}


def target_id(owner):
    if owner == "local-admin":
        raise HTTPException(403, "The original Admin sign-in is protected.")
    owner = admin_panel.uid(owner)
    if owner == admin_sessions.SUPER_ID:
        raise HTTPException(403, "This account is protected.")
    return owner


def access(user):
    return (user.get("app_metadata") or {}).get("qa_access") or {}


def account(user):
    settings = access(user)
    role = "admin" if settings.get("role") == "admin" else "user"
    return {
        "id": user["id"], "name": (user.get("user_metadata") or {}).get("name") or user.get("email"),
        "email": user.get("email"), "role": role, "status": settings.get("status", "active"),
        "created_at": user.get("created_at"), "updated_at": settings.get("updated_at") or user.get("updated_at"),
        "permissions": settings.get("permissions", {}),
    }


def list_accounts():
    result = []
    page = 1
    while True:
        users = auth("GET", "", params={"page": page, "per_page": 200})["users"]
        result.extend(account(user) for user in users)
        if len(users) < 200:
            return result
        page += 1


def original_admin_account():
    permissions, revision = access_control.original_admin_permissions()
    status, status_revision = access_control.original_admin_status()
    return {"id": "local-admin", "name": config.ADMIN_USERNAME, "role": "admin",
            "status": "active" if status.get("active", True) else "inactive", "protected": True,
            "permissions": permissions, "permissions_revision": revision, "status_revision": status_revision}


def audit(action, owner):
    cloud._request("POST", "/rest/v1/qa_admin_audit", json={"action": action, "target": owner})


def update(owner, current, action, *, revision_request=None, **changes):
    settings = {**access(current), **changes, "updated_at": datetime.now(timezone.utc).isoformat()}
    changed = cloud._request("POST", "/rest/v1/rpc/qa_update_account_access", json={
        "p_user": owner, "p_previous": access(current), "p_next": settings, "p_action": action,
    }, **({"headers": revisions.actor_header(revision_request)} if revision_request else {})).json()
    if not changed:
        raise HTTPException(409, "This account changed. Refresh and try again.")
    return account({**current, "app_metadata": {"qa_access": settings}})


@router.get("/overview")
def overview(request: Request):
    guard(request)
    admin_active = access_control.managed_admin_active()
    admin_policy, _ = access_control.policy("admin")
    people = list_accounts()
    people.extend([
        original_admin_account(),
        {"id": admin_sessions.SUPER_ID, "name": config.SUPER_ADMIN_USERNAME, "role": "super_admin",
         "status": "active", "protected": True},
    ])
    uploads = cloud.rows("qa_upload_requests", select="status,job_state", order="id.asc")
    from . import domains, ingest
    bases = pdfs = 0
    for person in people:
        token = workspace.bind(person)
        try:
            for domain in domains.list_domains():
                bases += 1
                files = ingest.list_ingested(domain.key).values()
                pdfs += sum(row["name"].lower().endswith(".pdf") for row in files)
        finally:
            workspace.reset(token)
    history = cloud._request("GET", "/rest/v1/qa_admin_audit", params={
        "select": "created_at,action,target", "order": "id.desc", "limit": 30,
    }).json()
    return {"accounts": people, "admin_role_active": admin_active, "admin_policy": admin_policy,
            "defaults": {role: access_control.defaults(role)
             for role in ("super_admin", "admin", "user")}, "resources": access_control.RESOURCES,
            "assignable": {role: [key for key in access_control.defaults(role) if access_control.available(role, key)]
                           for role in ("user", "admin")},
            "pending": sum(row["status"] == "pending" for row in uploads),
            "processing": dict(Counter(row["job_state"] for row in uploads if row.get("job_state") not in {None, "none"})),
            "bases": bases, "pdfs": pdfs, "audit": history}


@router.get("/permission-settings")
def permission_settings(request: Request):
    guard(request)
    roles = ("super_admin", "admin", "user")
    policies = {}
    for role in ("admin", "user"):
        values, revision = access_control.policy(role)
        policies[role] = {"modules": {}, "actions": {}, **values, "revision": revision}
    people = list_accounts()
    people.append(original_admin_account())
    return {"accounts": people, "resources": access_control.RESOURCES, "policies": policies,
            "opt_in_modules": {"user": list(access_control.USER_ROLE_MODULES)},
            "defaults": {role: access_control.defaults(role) for role in roles},
            "assignable": {role: [key for key in access_control.defaults(role) if access_control.available(role, key)]
                           for role in roles}}


class PermissionPolicy(BaseModel):
    modules: dict[str, bool]
    actions: dict[str, bool]
    revision: int = Field(ge=0)


class OriginalAdminPermissions(BaseModel):
    permissions: dict[str, bool]
    revision: int = Field(ge=0)


@router.put("/original-admin/permissions")
def save_original_admin_permissions(body: OriginalAdminPermissions, request: Request):
    guard(request)
    validate_permissions("admin", body.permissions)
    if not revisions.write_setting(request, "account:local-admin", body.permissions, body.revision):
        raise HTTPException(409, "Permissions changed. Refresh and try again.")
    return {"ok": True}


@router.put("/permission-settings/{role}")
def save_policy(role: Literal["admin", "user"], body: PermissionPolicy, request: Request):
    guard(request)
    validate_permissions(role, body.actions)
    if any(not access_control.module_available(role, key) for key in body.modules):
        raise HTTPException(400, "One or more modules are unavailable to this role.")
    if not revisions.write_setting(request, "policy:" + role, body.model_dump(exclude={"revision"}), body.revision):
        raise HTTPException(409, "Permissions changed. Refresh and try again.")
    return {"ok": True}


@router.get("/login-logs")
def login_logs(request: Request, before: int | None = Query(default=None, ge=1)):
    guard(request)
    rows = security_store.events(51, before)
    return {"events": rows[:50], "next": rows[49]["id"] if len(rows) > 50 else None}


@router.get("/login-logs.csv")
def export_login_logs(request: Request):
    guard(request)
    first = security_store.events(500)
    columns = ("id", "account_id", "created_at", "role", "outcome")

    def export():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        rows = first
        while True:
            for row in rows:
                writer.writerow([row[key] for key in columns])
            yield buffer.getvalue()
            if len(rows) < 500:
                break
            buffer.seek(0)
            buffer.truncate()
            rows = security_store.events(500, rows[-1]["id"])

    return StreamingResponse(export(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="login-logs.csv"'})


@router.get("/activity-logs/{category}")
def activity_logs(category: str, request: Request, before: int | None = Query(default=None, ge=1)):
    guard(request)
    if category not in security_store.ACTIVITY_CATEGORIES:
        raise HTTPException(404, "Unknown activity log category.")
    rows = security_store.activity_events(category, 51, before)
    return {"events": rows[:50], "next": rows[49]["id"] if len(rows) > 50 else None}


@router.get("/revisions")
def revision_history(request: Request, local_before: int | None = Query(default=None, ge=1),
                     cloud_before: int | None = Query(default=None, ge=1)):
    guard(request)
    return revisions.history(local_before, cloud_before)


@router.post("/revisions/{revision_id}/reverse")
def reverse_revision(revision_id: str, request: Request):
    guard(request)
    match = re.fullmatch(r"RV-([CL])([0-9]{1,18})", revision_id)
    if not match or not int(match[2]):
        raise HTTPException(404, "Revision not found.")
    return {"revision_id": revisions.reverse(match[1], int(match[2]), request.state.user["id"])}


class NewAccount(SignupBody):
    role: Literal["admin", "user"] = "admin"
    method: Literal["create", "invite"] = "create"
    password: str | None = Field(default=None, min_length=8, max_length=256)
    permissions: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_password_for_create(self):
        if self.method == "create" and self.password is None:
            raise ValueError("Enter an initial password.")
        return self


def validate_permissions(role, permissions):
    if any(not access_control.available(role, key) for key in permissions):
        raise HTTPException(400, "One or more permissions are unavailable to this role.")


def require_active_role(role):
    if role == "admin" and not access_control.managed_admin_active():
        raise HTTPException(409, "Reactivate the Admin role before assigning it.")


class RoleStatus(BaseModel):
    active: bool


class OriginalAdminStatus(RoleStatus):
    revision: int = Field(ge=0)


@router.put("/original-admin/status")
def set_original_admin_status(body: OriginalAdminStatus, request: Request):
    guard(request)
    if not revisions.write_setting(request, "status:local-admin", {"active": body.active}, body.revision):
        raise HTTPException(409, "Admin status changed. Refresh and try again.")
    return {"active": body.active}


@router.put("/accounts/{owner}/upload-approval")
def set_upload_approval(owner: str, body: RoleStatus, request: Request):
    guard(request)
    permissions = {"approvals.view": body.active, "approvals.edit": body.active}
    if owner == "local-admin":
        current, revision = access_control.original_admin_permissions()
        if not revisions.write_setting(request, "account:local-admin", {**current, **permissions}, revision):
            raise HTTPException(409, "Permissions changed. Refresh and try again.")
        return {"active": body.active}
    owner = target_id(owner)
    current = auth("GET", "/" + owner)
    previous = account(current)
    if previous["role"] != "admin" or previous["status"] == "deleting":
        raise HTTPException(409, "Select an Admin account to change upload approval access.")
    action = "Granted upload approval" if body.active else "Revoked upload approval"
    return update(owner, current, action, revision_request=request, permissions={**previous["permissions"], **permissions})


@router.put("/roles/admin")
def set_admin_role(body: RoleStatus, request: Request):
    guard(request)
    cloud._request("POST", "/rest/v1/rpc/qa_set_admin_role_active", headers=revisions.actor_header(request), json={"p_active": body.active})
    return {"active": body.active}


@router.post("/accounts", status_code=201)
def create(body: NewAccount, request: Request):
    guard(request)
    validate_permissions(body.role, body.permissions)
    require_active_role(body.role)
    cloud._request("GET", "/rest/v1/qa_admin_audit", params={"select": "id", "limit": 1})
    settings = {"role": body.role, "permissions": body.permissions}
    if body.method == "invite":
        invitation = auth("POST", "", endpoint="/admin/generate_link", json={
            "type": "invite", "email": body.email, "data": {"name": body.name},
        })
        result = update(invitation["id"], invitation, "Invited " + body.role.title(), invited=True, **settings)
        request.state.activity_target = result["id"]
        return {**result, "invite_token": sign_invite(invitation["hashed_token"])}
    result = account(auth("POST", "", json={
        "email": body.email, "password": body.password, "email_confirm": True,
        "user_metadata": {"name": body.name}, "app_metadata": {"qa_access": settings},
    }))
    audit("Added " + body.role.title(), result["id"])
    request.state.activity_target = result["id"]
    return result


class AccountEdits(BaseModel):
    role: Literal["admin", "user"] | None = None
    status: Literal["active", "inactive"] | None = None
    permissions: dict[str, bool] | None = None


@router.patch("/accounts/{owner}")
def edit(owner: str, body: AccountEdits, request: Request):
    guard(request)
    owner = target_id(owner)
    current = auth("GET", "/" + owner)
    previous = account(current)
    if previous["status"] == "deleting":
        raise HTTPException(409, "Permanent deletion is in progress. Retry deletion to finish.")
    changes = body.model_dump(exclude_none=True)
    role = changes.get("role", previous["role"])
    if role != previous["role"]:
        require_active_role(role)
    if "permissions" in changes:
        validate_permissions(role, changes["permissions"])
    elif role != previous["role"]:
        changes["permissions"] = {key: value for key, value in previous["permissions"].items()
                                  if access_control.available(role, key)}
    if not changes:
        return previous
    result = update(owner, current, "Updated " + ", ".join(changes), revision_request=request, **changes)
    events = []
    if "status" in changes and changes["status"] != previous["status"]:
        events.append(("user-details", "Activated account" if changes["status"] == "active" else "Deactivated account"))
    if "role" in changes and role != previous["role"]:
        events.append(("role-management", "Changed account role"))
    if "permissions" in changes and changes["permissions"] != previous["permissions"]:
        events.append(("role-management", "Changed account permissions"))
    request.state.activity_events = events
    return result


def delete_files(bucket, files):
    for offset in range(0, len(files), 100):
        cloud._request("DELETE", "/storage/v1/object/" + quote(bucket, safe=""),
                       json={"prefixes": files[offset:offset + 100]})


def purge_workspace(owner):
    from . import vectorstore
    token = workspace.bind({"id": owner})
    try:
        suffix = workspace.suffix()
        namespace_filter = "like.api_*_" + suffix
        documents = [row for row in cloud.rows("qa_documents", namespace=namespace_filter,
                                                order="namespace.asc,domain.asc,file_hash.asc")
                     if workspace.owns_namespace(row["namespace"])]
        uploads = cloud.rows("qa_upload_requests", user_id="eq." + owner, order="id.asc")
        now = datetime.now(timezone.utc)
        if any(row.get("lease_until") and datetime.fromisoformat(row["lease_until"]) > now
               for row in documents + uploads):
            raise HTTPException(409, "Processing is finishing. Retry permanent deletion shortly.")
        paths = []
        for row in documents:
            path = row["object_path"]
            if not path.startswith(row["namespace"] + "/"):
                raise HTTPException(409, "A stored file has an invalid workspace path.")
            paths.extend([path, path + ".ocr.pdf", path + ".ocr.txt"])
        pending = []
        for row in uploads:
            if row.get("object_path"):
                path = row["object_path"]
                if path != owner + "/" + row["id"]:
                    raise HTTPException(409, "A saved upload has an invalid workspace path.")
                pending.append(path)
        offset = 0
        while True:
            inventory = cloud._request("POST", "/rest/v1/rpc/qa_account_storage_objects",
                params={"limit": 1000, "offset": offset}, json={
                    "p_user": owner, "p_document_bucket": config.SUPABASE_BUCKET,
                    "p_upload_bucket": saved_uploads.BUCKET,
                }).json()
            for row in inventory:
                if row["bucket"] == config.SUPABASE_BUCKET and workspace.owns_namespace(row["name"].split("/")[0]):
                    paths.append(row["name"])
                elif row["bucket"] == saved_uploads.BUCKET and row["name"].startswith(owner + "/"):
                    pending.append(row["name"])
            if len(inventory) < 1000:
                break
            offset += len(inventory)
        delete_files(config.SUPABASE_BUCKET, sorted(set(paths)))
        delete_files(saved_uploads.BUCKET, sorted(set(pending)))
        client = vectorstore.get_client()
        for collection in client.get_collections().collections:
            name = collection.name
            if name.startswith(suffix + "_") or re.match(r"^api_[0-9a-f]{16}_" + suffix + "_", name):
                client.delete_collection(name)
        for root, folder in ((config.DATA_DIR, workspace.data_dir()), (config.PDF_DIR, workspace.pdf_root())):
            if folder.resolve() != root.resolve() / "workspaces" / suffix:
                raise HTTPException(409, "Invalid local workspace path.")
            if folder.is_dir():
                shutil.rmtree(folder)
        for namespace in {row["namespace"] for row in documents}:
            cloud.remove("qa_documents", namespace="eq." + namespace)
        cloud.remove("qa_domains", owner_id="eq." + owner)
    finally:
        workspace.reset(token)


@router.delete("/accounts/{owner}")
def delete(owner: str, request: Request, confirmation: str):
    guard(request)
    owner = target_id(owner)
    if confirmation != "DELETE":
        raise HTTPException(400, "Confirm permanent deletion.")
    current = auth("GET", "/" + owner)
    if not workspace.begin_delete(owner):
        raise HTTPException(409, "Workspace activity is still finishing. Retry permanent deletion shortly.")
    try:
        return delete_account(owner, current)
    finally:
        workspace.end_delete(owner)


def delete_account(owner, current):
    if account(current)["status"] not in {"inactive", "deleting"}:
        raise HTTPException(409, "Deactivate this account first.")
    if account(current)["status"] != "deleting":
        update(owner, current, "Started permanent deletion", status="deleting")
    purge_workspace(owner)
    security_store.remove_factor(owner)
    auth("DELETE", "/" + owner)
    audit("Permanently deleted account", owner)
    return {"ok": True}


def selected_workspace(request):
    user = request.state.user
    selected = request.headers.get("x-workspace-account")
    if not selected:
        return user
    if user["role"] != "super_admin":
        access_control.authorize_user_uploads(user, request.url.path, request.method)
        person = account(auth("GET", "/" + target_id(selected)))
        if person["role"] != "user" or person["status"] != "active":
            raise HTTPException(403, "Select an active User account.")
        return person
    if selected == "local-admin":
        return {"id": "local-admin", "role": "admin"}
    if selected == admin_sessions.SUPER_ID:
        return user
    selected = target_id(selected)
    person = account(auth("GET", "/" + selected))
    if person["status"] == "deleting":
        raise HTTPException(409, "This workspace is being deleted.")
    return person
