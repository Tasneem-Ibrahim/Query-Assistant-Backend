"""Super Admin account administration. Only server-owned Auth metadata is trusted."""
import csv
import io
from contextlib import nullcontext
from functools import wraps
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from . import access_control, admin_panel, admin_sessions, audit_journal, cloud, config, revisions, saved_uploads, security_store, workspace
from .user_auth import SignupBody, sign_invite

router = APIRouter(prefix="/api/super-admin", tags=["Super Admin"])


def guard(request):
    if request.state.user["role"] != "super_admin":
        raise HTTPException(403, "Super Admin access required.")


def permission_guard(request):
    actor = request.state.user
    if actor["role"] == "super_admin":
        return actor
    if not access_control.allows(actor, "permissions.view"):
        raise HTTPException(403, "Permission management access required.")
    return actor


def delegated_transaction(handler):
    @wraps(handler)
    def wrapped(*args, request, **kwargs):
        actor = request.state.user
        if not any(access_control.allows(actor, key) for key in (
            "permissions.add", "permissions.edit", "permissions.delete", "roles.edit", "roles.delete", "accounts.add",
            "accounts.edit", "accounts.delete", "roles.add", "logs.add", "logs.edit", "logs.delete",
            "revisions.add", "revisions.edit", "revisions.delete",
        )):
            raise HTTPException(403, "Delegated write access required.")
        if actor["role"] == "super_admin":
            return handler(*args, request=request, **kwargs)
        with (nullcontext() if config.CLOUD_MODE else revisions.database()) as db:
            if db is not None:
                db.execute("begin immediate")
                request.state.security_db = db

            def read(key):
                return security_store.read(key) if db is None else revisions.local_state(db, key)

            policies = {role: read("policy:" + role) for role in ("admin", "user")}
            actor_policy, _ = policies[actor["role"]]
            snapshot = {}
            if config.CLOUD_MODE:
                snapshot["state"] = {"policy:" + role: version for role, (_, version) in policies.items()}
            delegated_overrides = []
            if actor["id"] == "local-admin":
                overrides, version = read("account:local-admin")
                status, status_version = read("status:local-admin")
                if not status.get("active", True):
                    raise HTTPException(403, "Admin workspace access is paused.")
                if config.CLOUD_MODE:
                    snapshot["state"].update({"account:local-admin": version, "status:local-admin": status_version})
            else:
                current = auth("GET", "/" + actor["id"])
                person = account(current)
                if (person["role"] != actor["role"] or person["status"] != "active"
                        or person["role"] == "admin" and not access_control.managed_admin_active()):
                    raise HTTPException(403, "Account authority changed. Refresh and try again.")
                delegated_overrides = person["delegated_overrides"]
                overrides = person["permissions"]
                snapshot["account"] = access(current)
            request.state.user = {**actor, "account_scope": access_control.scope_entries(overrides),
                                  "permissions": access_control.effective(
                                      {**actor, "delegated_overrides": delegated_overrides},
                                      actor_policy, account_overrides=overrides)}
            snapshot["scope"] = request.state.user["account_scope"]
            snapshot["allowed"] = sorted(access_control.grant_scope(request.state.user))
            request.state.delegation_snapshot = snapshot
            request.state.delegation_policies = policies
            try:
                return handler(*args, request=request, **kwargs)
            except cloud.DelegationConflict as exc:
                raise HTTPException(409, str(exc)) from None
            finally:
                request.state.user = actor
    return wrapped


def audit_guard(request, key):
    if not access_control.allows(request.state.user, key):
        raise HTTPException(403, "Audit access required.")



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


def changed_keys(before, after):
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


def provenance(actor, previous, before, after, *, track_grants=False):
    changed = changed_keys(before, after)
    retained = set(previous) - changed
    if actor["role"] != "super_admin":
        retained.update(key for key in changed if key in after and (track_grants or after[key] is False))
    return sorted(retained)


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
        "delegated_overrides": settings.get("delegated_overrides", []),
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
    delegated = revision_request is not None and getattr(revision_request.state, "delegation_snapshot", None) is not None
    function = ("qa_update_delegated_account" if delegated else "qa_update_permission_account"
                if config.CLOUD_MODE and "permissions" in changes else "qa_update_account_access")
    changed = cloud._request("POST", "/rest/v1/rpc/" + function, json={
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


def require_actions(actor, *keys):
    if not all(access_control.allows(actor, key) for key in keys):
        raise HTTPException(403, "This administrative action is disabled for your account.")


def require_target(actor, person):
    if not access_control.scoped_to(actor, person):
        raise HTTPException(403, "This account is outside your delegated scope.")


@router.get("/workspace-accounts")
def workspace_accounts(request: Request):
    actor = request.state.user
    require_actions(actor, "user_uploads.view")
    return {"accounts": [{key: person[key] for key in ("id", "name", "email", "role", "status")}
                         for person in list_accounts()
                         if person["status"] == "active" and access_control.scoped_to(actor, person)]}


@router.get("/role-accounts")
def role_accounts(request: Request):
    actor = request.state.user
    require_actions(actor, "roles.view")
    people = [person for person in list_accounts() if access_control.scoped_to(actor, person)]
    policies = {role: access_control.policy(role) for role in ("admin", "user")}
    active = access_control.managed_admin_active()
    if actor["role"] == "super_admin":
        people += [original_admin_account(), {
            "id": admin_sessions.SUPER_ID, "name": config.SUPER_ADMIN_USERNAME, "role": "super_admin",
            "status": "active", "protected": True,
        }]
    else:
        for person in people:
            person["role_choices"] = []
            next_role = "user" if person["role"] == "admin" else "admin"
            if person["status"] == "deleting" or next_role == "admin" and not active:
                continue
            if access_control.allows(actor, "roles.edit") or (
                next_role == "user" and access_control.allows(actor, "roles.delete")
            ):
                try:
                    check_role_change(actor, person, next_role, policies)
                except HTTPException:
                    continue
                person["role_choices"].append(next_role)
    policy, _ = policies["admin"]
    can_create_admin = False
    if active and "role:admin" in actor.get("account_scope", ()) and access_control.allows(actor, "roles.edit"):
        try:
            require_bounded_target(actor, {"id": "", "role": "admin", "permissions": {}}, policy)
        except HTTPException:
            pass
        else:
            can_create_admin = True
    return {"accounts": people, "admin_role_active": active, "can_create_admin": can_create_admin,
            "admin_policy": policy, "resources": access_control.RESOURCES,
            "defaults": {role: access_control.defaults(role) for role in ("admin", "user")},
            "assignable": {role: sorted(access_control.grant_scope({"role": "super_admin"})) for role in ("admin", "user")}}


@router.get("/permission-settings")
def permission_settings(request: Request):
    actor = permission_guard(request)
    is_super = actor["role"] == "super_admin"
    roles = ("super_admin", "admin", "user") if is_super else ("admin", "user")
    policies = {}
    for role in roles:
        if role == "super_admin":
            continue
        values, revision = access_control.policy(role)
        policies[role] = {"modules": {}, "actions": {}, **values, "revision": revision}
    people = list_accounts()
    if is_super:
        people.append(original_admin_account())
    else:
        people = [person for person in people if access_control.scoped_to(actor, person)]
    return {"accounts": people, "editable": is_super or any(access_control.allows(actor, "permissions." + action)
                                         for action in ("add", "edit", "delete")),
            "editable_roles": [role for role in ("admin", "user") if can_edit_policy(actor, role)],
            "delegation_scope": None if is_super else sorted(access_control.grant_scope(actor)), "resources": access_control.RESOURCES, "policies": policies,
            "opt_in_modules": {"user": list(access_control.USER_ROLE_MODULES)},
            "opt_in_actions": {"user": sorted(set().union(*access_control.USER_ROLE_MODULES.values()))},
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


def can_edit_policy(actor, role):
    return actor["role"] == "super_admin" or (
        role != actor["role"] and "policy:" + role in actor.get("account_scope", ())
        and any(access_control.allows(actor, "permissions." + action) for action in ("add", "edit", "delete"))
    )


def require_permission_changes(actor, before, after):
    require_actions(actor, "permissions.view")
    for key in changed_keys(before, after):
        action = "add" if key not in before else "delete" if key not in after else "edit"
        require_actions(actor, "permissions." + action)


def check_policy_delegation(actor, before, after):
    if actor["role"] == "super_admin":
        return
    for group in ("modules", "actions"):
        old, new = before.get(group, {}), after.get(group, {})
        keys = changed_keys(old, new)
        if group == "actions":
            require_permission_changes(actor, old, new)
        elif keys:
            require_actions(actor, "permissions.edit")
        if any(key.partition(".")[0] not in {"bases", "documents"} for key in keys):
            raise HTTPException(403, "Shared administrative defaults are controlled by Super Admin.")
        access_control.check_delegation(actor, old, new, modules=group == "modules",
                                        delegated=before.get("delegated_" + group, []))


@router.put("/permission-settings/{role}")
@delegated_transaction
def save_policy(role: Literal["admin", "user"], body: PermissionPolicy, request: Request):
    actor = permission_guard(request)
    if not can_edit_policy(actor, role):
        raise HTTPException(403, "This role policy is outside your delegated scope.")
    previous, revision = (access_control.policy(role) if actor["role"] == "super_admin"
                          else request.state.delegation_policies[role])
    check_policy_delegation(actor, previous, body.model_dump(exclude={"revision"}))
    if body.revision != revision:
        raise HTTPException(409, "Permissions changed. Refresh and try again.")
    validate_permissions(role, body.actions, account_overrides=False)
    if any(not access_control.module_available(role, key) for key in body.modules):
        raise HTTPException(400, "One or more modules are unavailable to this role.")
    state = body.model_dump(exclude={"revision"})
    for group in ("modules", "actions"):
        field = "delegated_" + group
        delegated = provenance(actor, previous.get(field, []), previous.get(group, {}), state[group])
        if delegated:
            state[field] = delegated
    if not revisions.write_setting(request, "policy:" + role, state, body.revision):
        raise HTTPException(409, "Permissions changed. Refresh and try again.")
    return {"ok": True}


@router.get("/login-logs")
def login_logs(request: Request, before: int | None = Query(default=None, ge=1)):
    audit_guard(request, "logs.view")
    rows = security_store.events(51, before)
    return {"events": rows[:50], "next": rows[49]["id"] if len(rows) > 50 else None}


@router.get("/login-logs.csv")
def export_login_logs(request: Request):
    audit_guard(request, "logs.view")
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
    audit_guard(request, "logs.view")
    if category not in security_store.ACTIVITY_CATEGORIES:
        raise HTTPException(404, "Unknown activity log category.")
    rows = security_store.activity_events(category, 51, before)
    return {"events": rows[:50], "next": rows[49]["id"] if len(rows) > 50 else None}


AuditResource = Literal["logs", "revisions"]


class AuditEntry(BaseModel):
    note: str = Field(min_length=1, max_length=2000)
    subject: str | None = Field(default=None, max_length=36)


class AuditAnnotations(BaseModel):
    references: list[str] = Field(max_length=250)


@router.get("/audit-subjects/{resource}")
def audit_subjects(resource: AuditResource, request: Request):
    actor = request.state.user
    require_actions(actor, resource + ".view")
    return {"accounts": [{key: person[key] for key in ("id", "name", "email")}
                         for person in list_accounts() if access_control.scoped_to(actor, person)]}


@router.get("/audit-journal/{resource}")
def audit_history(resource: AuditResource, request: Request, before: int | None = Query(default=None, ge=1)):
    require_actions(request.state.user, resource + ".view")
    return audit_journal.history(resource, before)


@router.post("/audit-journal/{resource}/annotations")
def audit_annotations(resource: AuditResource, body: AuditAnnotations, request: Request):
    require_actions(request.state.user, resource + ".view")
    return audit_journal.states(resource, body.references)


@router.post("/audit-journal/{resource}", status_code=201)
@delegated_transaction
def add_audit_entry(resource: AuditResource, body: AuditEntry, request: Request):
    return write_audit_entry(resource, "add", None, body, request)


@router.patch("/audit-journal/{resource}/{reference}")
@delegated_transaction
def correct_audit_entry(resource: AuditResource, reference: str, body: AuditEntry, request: Request):
    if resource != "logs":
        raise HTTPException(405, "Use a manual revision entry to record a clarification.")
    return write_audit_entry(resource, "correct", reference, body, request)


@router.delete("/audit-journal/{resource}/{reference}")
@delegated_transaction
def archive_audit_entry(resource: AuditResource, reference: str, body: AuditEntry, request: Request):
    return write_audit_entry(resource, "archive", reference, body, request)


@router.post("/audit-journal/{resource}/{reference}/restore")
@delegated_transaction
def restore_audit_entry(resource: AuditResource, reference: str, body: AuditEntry, request: Request):
    return write_audit_entry(resource, "restore", reference, body, request)


def write_audit_entry(resource, operation, reference, body, request):
    actor = request.state.user
    action = {"add": "add", "correct": "edit", "archive": "delete", "restore": "delete"}[operation]
    require_actions(actor, resource + ".view", resource + "." + action)
    note = body.note.strip()
    if not note:
        raise HTTPException(400, "Enter a note or reason.")
    subject = body.subject
    if reference is not None:
        if not any(pattern.fullmatch(reference) for pattern in audit_journal.REFERENCE[resource]):
            raise HTTPException(404, "Audit record not found.")
        db = getattr(request.state, "security_db", None)
        if config.CLOUD_MODE or db is not None:
            row = audit_journal.lookup(reference, resource, db)
        else:
            with revisions.database() as db:
                row = audit_journal.lookup(reference, resource, db)
        if row is None:
            raise HTTPException(404, "Audit record not found.")
        subject = row["subject"]
    if operation == "add" and subject is not None:
        subject = target_id(subject)
    if actor["role"] != "super_admin":
        try:
            owner = target_id((subject or "").split(" / ", 1)[0])
        except HTTPException:
            raise HTTPException(403, "This record has no account in your delegated scope.") from None
        require_target(actor, account(auth("GET", "/" + owner)))
    identifier = audit_journal.append(request, resource, operation, reference, subject, note)
    return {"id": identifier}


def check_revision_scope(actor, item, policies, targets):
    if item["actor_id"] != actor["id"]:
        raise HTTPException(403, "You can reverse only your own changes.")
    before, after = item["before_state"], item["after_state"]
    if item["kind"] == "security" and item["target"] in {"policy:admin", "policy:user"}:
        role = item["target"].partition(":")[2]
        if not can_edit_policy(actor, role):
            raise HTTPException(403, "This role policy is outside your delegated scope.")
        require_actions(actor, "permissions.view")
        check_policy_delegation(actor, after, before)
    elif config.CLOUD_MODE and item["kind"] == "account" and item["target"] in targets:
        person = targets[item["target"]]
        require_target(actor, person)
        policy, _ = policies[person["role"]]
        if before["role"] != after["role"]:
            require_actions(actor, "roles.view", "roles.edit")
            if before["permissions"] != after["permissions"]:
                raise HTTPException(403, "Combined role and permission changes require Super Admin.")
            check_role_change(actor, person, before["role"], policies)
        elif before["status"] != after["status"]:
            require_actions(actor, "accounts.view", "accounts.edit")
            require_bounded_target(actor, person, policy)
        else:
            require_actions(actor, "permissions.view")
            check_account_delegation(actor, policy, after["permissions"], before["permissions"],
                                     after.get("delegated_overrides", []))
            if access_control.scope_entries(person["permissions"]):
                require_bounded_target(actor, person, policy)
    else:
        raise HTTPException(403, "This revision is outside your delegated scope.")


@router.get("/revisions")
def revision_history(request: Request, local_before: int | None = Query(default=None, ge=1),
                     cloud_before: int | None = Query(default=None, ge=1)):
    audit_guard(request, "revisions.view")
    result = revisions.history(local_before, cloud_before)
    actor = request.state.user
    if actor["role"] != "super_admin" and access_control.allows(actor, "revisions.edit"):
        policies = {role: access_control.policy(role) for role in ("admin", "user")}
        targets = {}
        if config.CLOUD_MODE and any(item["kind"] == "account" and item["actor_id"] == actor["id"]
                                     for item in result["revisions"]):
            targets = {person["id"]: person for person in list_accounts()
                       if access_control.scoped_to(actor, person)}
        for item in result["revisions"]:
            try:
                check_revision_scope(actor, item, policies, targets)
            except HTTPException:
                continue
            item["can_reverse"] = True
    return result


@router.post("/revisions/{revision_id}/reverse")
@delegated_transaction
def reverse_revision(revision_id: str, request: Request):
    actor = request.state.user
    require_actions(actor, "revisions.view", "revisions.edit")
    match = re.fullmatch(r"RV-([CL])([0-9]{1,18})", revision_id)
    if not match or not int(match[2]):
        raise HTTPException(404, "Revision not found.")
    source, value = match[1], int(match[2])
    if actor["role"] == "super_admin":
        if source == "C":
            item = revisions.lookup(source, value)
            if item is not None and (item["kind"] == "account-created"
                                     or item["after_state"].get("status") == "deleting"):
                raise HTTPException(409, "Account creation and permanent deletion cannot be reversed.")
        return {"revision_id": revisions.reverse(source, value, actor["id"])}
    if not config.CLOUD_MODE and source == "C":
        raise HTTPException(403, "Cloud account revisions require cloud permission storage for delegated reversal.")
    item = revisions.lookup(source, value, getattr(request.state, "security_db", None))
    if item is None:
        raise HTTPException(404, "Revision not found.")
    targets = {}
    if item["kind"] == "account" and item["actor_id"] == actor["id"]:
        owner = target_id(item["target"])
        targets[owner] = account(auth("GET", "/" + owner))
    check_revision_scope(actor, item, request.state.delegation_policies, targets)
    return {"revision_id": revisions.reverse(source, value, actor["id"], request)}


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


def valid_scope(key):
    if key in {"role:user", "role:admin", "policy:user", "policy:admin"}:
        return True
    try:
        return target_id(key) == key
    except HTTPException:
        return False


def validate_permissions(role, permissions, *, account_overrides=True):
    if any(not (access_control.module_available(role, key.removeprefix("module:"))
                if account_overrides and key.startswith("module:")
                else valid_scope(key.removeprefix("scope:")) if account_overrides and key.startswith("scope:")
                else access_control.available(role, key))
           for key in permissions):
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
@delegated_transaction
def create(body: NewAccount, request: Request):
    actor = request.state.user
    require_actions(actor, "roles.view", "roles.add")
    return create_account(body, request)


class NewUser(NewAccount):
    role: Literal["user"] = "user"


@router.post("/user-details", status_code=201)
@delegated_transaction
def add_user_detail(body: NewUser, request: Request):
    actor = request.state.user
    require_actions(actor, "accounts.view", "accounts.add")
    if body.permissions:
        raise HTTPException(400, "User Details adds User accounts with role defaults.")
    if actor["role"] != "super_admin" and "role:user" not in actor.get("account_scope", ()):
        raise HTTPException(403, "Adding Users requires scope for User accounts.")
    return create_account(body, request)


def create_account(body, request):
    actor = request.state.user
    if actor["role"] != "super_admin":
        if body.permissions:
            raise HTTPException(403, "Create the account with role defaults before assigning individual overrides.")
        if body.role == "admin":
            require_actions(actor, "roles.edit")
            if "role:admin" not in actor.get("account_scope", ()):
                raise HTTPException(403, "Creating an Admin requires scope for managed Admin accounts.")
        policy, _ = request.state.delegation_policies[body.role]
        require_bounded_target(actor, {"id": "", "role": body.role, "permissions": {}}, policy)
    validate_permissions(body.role, body.permissions)
    require_active_role(body.role)
    cloud._request("GET", "/rest/v1/qa_admin_audit", params={"select": "id", "limit": 1})
    settings = {"role": body.role, "permissions": body.permissions}
    if body.method == "invite":
        invitation = auth("POST", "", endpoint="/admin/generate_link", json={
            "type": "invite", "email": body.email, "data": {"name": body.name},
        })
        result = update(invitation["id"], invitation, "Invited " + body.role.title(),
                        revision_request=request, invited=True, **settings)
        request.state.activity_target = result["id"]
        return {**result, "invite_token": sign_invite(invitation["hashed_token"])}
    result = account(auth("POST", "", json={
        "email": body.email, "password": body.password, "email_confirm": True,
        "user_metadata": {"name": body.name},
        "app_metadata": {"qa_access": {**settings, "created_by": actor["id"]}},
    }))
    audit("Added " + body.role.title(), result["id"])
    request.state.activity_target = result["id"]
    return result


class AccountEdits(BaseModel):
    role: Literal["admin", "user"] | None = None
    status: Literal["active", "inactive"] | None = None
    permissions: dict[str, bool] | None = None
    expected_permissions: dict[str, bool] | None = None


def check_account_delegation(actor, policy, before, after, delegated):
    actions_before = {key: value for key, value in before.items() if not key.startswith("module:")}
    actions_after = {key: value for key, value in after.items() if not key.startswith("module:")}
    require_permission_changes(actor, actions_before, actions_after)
    if any(key.startswith("module:") for key in changed_keys(before, after)):
        require_actions(actor, "permissions.edit")
    access_control.check_delegation(actor, before, after, delegated=delegated)
    for key in changed_keys(before, after):
        if (policy.get("actions", {}).get(key) is False and key not in policy.get("delegated_actions", [])
                and after.get(key) is not False):
            raise HTTPException(403, "This restriction is controlled by Super Admin.")


def require_bounded_target(actor, person, policy):
    effective = access_control.effective(person, policy)
    if {key for key, enabled in effective.items() if enabled} - access_control.grant_scope(actor):
        raise HTTPException(403, "The target account has authority outside your delegation.")


def check_role_change(actor, previous, next_role, policies):
    old_policy, _ = policies[previous["role"]]
    new_policy, _ = policies[next_role]
    require_bounded_target(actor, previous, old_policy)
    candidate = {**previous, "role": next_role}
    require_bounded_target(actor, candidate, new_policy)
    old = access_control.effective(previous, old_policy)
    new = access_control.effective(candidate, new_policy)
    for key, allowed in new.items():
        resource = key.partition(".")[0]
        if allowed and not old[key] and (
            old_policy.get("modules", {}).get(resource) is False
            and resource not in old_policy.get("delegated_modules", ())
            or old_policy.get("actions", {}).get(key) is False
            and key not in old_policy.get("delegated_actions", ())
        ):
            raise HTTPException(403, "The role change would bypass a Super Admin restriction.")


@router.patch("/accounts/{owner}")
@delegated_transaction
def edit(owner: str, body: AccountEdits, request: Request):
    actor = request.state.user
    if body.role is not None:
        require_actions(actor, "roles.view")
        if not (access_control.allows(actor, "roles.edit") or
                body.role == "user" and access_control.allows(actor, "roles.delete")):
            raise HTTPException(403, "Role assignment access required.")
    if body.status is not None:
        require_actions(actor, "accounts.view", "accounts.edit")
    if body.permissions is not None:
        permission_guard(request)
    if body.role is None and body.status is None and body.permissions is None:
        raise HTTPException(400, "Provide an account setting to update.")
    owner = target_id(owner)
    current = auth("GET", "/" + owner)
    previous = account(current)
    if previous["status"] == "deleting":
        raise HTTPException(409, "Permanent deletion is in progress. Retry deletion to finish.")
    if body.expected_permissions is not None and body.expected_permissions != previous["permissions"]:
        raise HTTPException(409, "Permissions changed. Refresh and try again.")
    changes = body.model_dump(exclude={"expected_permissions"}, exclude_none=True)
    role = changes.get("role", previous["role"])
    if role != previous["role"]:
        require_active_role(role)
    if actor["role"] != "super_admin":
        require_target(actor, previous)
        if len(changes) != 1:
            raise HTTPException(403, "Make delegated account changes one at a time.")
        policies = request.state.delegation_policies
        policy, _ = policies[previous["role"]]
        if "status" in changes or access_control.scope_entries(previous["permissions"]):
            require_bounded_target(actor, previous, policy)
        if "role" in changes and role != previous["role"]:
            check_role_change(actor, previous, role, policies)
        if "permissions" in changes:
            check_account_delegation(actor, policy, previous["permissions"], changes["permissions"],
                                     previous["delegated_overrides"])
            if access_control.scope_entries(previous["permissions"]):
                require_bounded_target(actor, {**previous, "permissions": changes["permissions"]}, policy)
    if "permissions" in changes:
        validate_permissions(role, changes["permissions"])
    if not changes:
        return previous
    if "permissions" in changes:
        delegated = provenance(actor, previous["delegated_overrides"], previous["permissions"], changes["permissions"],
                               track_grants=True)
        if delegated or previous["delegated_overrides"]:
            changes["delegated_overrides"] = delegated
    result = update(owner, current, "Updated " + ", ".join(changes), revision_request=request, **changes)
    events = []
    if not config.CLOUD_MODE and "status" in changes and changes["status"] != previous["status"]:
        events.append(("user-details", "Activated account" if changes["status"] == "active" else "Deactivated account"))
    if not config.CLOUD_MODE and "role" in changes and role != previous["role"]:
        events.append(("role-management", "Changed account role"))
    if not config.CLOUD_MODE and "permissions" in changes and changes["permissions"] != previous["permissions"]:
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
@delegated_transaction
def delete(owner: str, request: Request, confirmation: str):
    actor = request.state.user
    require_actions(actor, "accounts.view", "accounts.delete")
    owner = target_id(owner)
    if confirmation != "DELETE":
        raise HTTPException(400, "Confirm permanent deletion.")
    current = auth("GET", "/" + owner)
    person = account(current)
    require_target(actor, person)
    if actor["role"] != "super_admin":
        policy, _ = request.state.delegation_policies[person["role"]]
        require_bounded_target(actor, person, policy)
    if not workspace.begin_delete(owner):
        raise HTTPException(409, "Workspace activity is still finishing. Retry permanent deletion shortly.")
    try:
        return delete_account(owner, current, request)
    finally:
        workspace.end_delete(owner)


def delete_account(owner, current, request=None):
    if account(current)["status"] not in {"inactive", "deleting"}:
        raise HTTPException(409, "Deactivate this account first.")
    if account(current)["status"] != "deleting":
        update(owner, current, "Started permanent deletion", revision_request=request, status="deleting")
    db = getattr(request.state, "security_db", None) if request is not None else None
    if db is not None:
        db.commit()
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
        require_target(user, person)
        if person["status"] != "active":
            raise HTTPException(403, "Select an active account.")
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
