"""Admin directory and approvals for private saved uploads.

Service-role tables are never exposed directly to the browser; workspace
identity always comes from authentication. Browser requests resume saved uploads.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, File, Form, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import cloud, config, workspace

router = APIRouter(prefix="/api", tags=["Administration"])
POLICIES = {"approval_required", "allowed", "blocked"}


def admin(request):
    if request.state.user.get("role") not in {"admin", "super_admin"}:
        raise HTTPException(403, "Administrator access required.")


def uid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid user ID.") from None


def policy(owner):
    rows = cloud.rows("qa_upload_permissions", user_id="eq." + owner)
    return rows[0]["policy"] if rows else "approval_required"


def upload_mode(owner, direct_upload=False):
    mode = policy(owner)
    return "allowed" if direct_upload and mode == "approval_required" else mode


def require_upload(key, filename, content, *, direct_upload=False):
    if workspace.owner_id() == "admin" or workspace.privileged_actor():
        return
    owner = workspace.owner_id()
    managed = cloud.rows('qa_upload_requests',user_id='eq.'+owner,domain='eq.'+key,
        file_hash='eq.'+hashlib.sha256(content).hexdigest())
    if any(r.get('file_ready') and r.get('job_state','none') != 'none' for r in managed):
        raise HTTPException(409, 'This saved upload resumes from your Dashboard. Check its status below.')
    mode = upload_mode(owner, direct_upload)
    if mode == "allowed":
        return
    if mode == "blocked":
        raise HTTPException(403, "Uploads are blocked for your account. Contact the administrator.")
    found = cloud.rows("qa_upload_requests", user_id="eq." + owner, domain="eq." + key,
                       file_hash="eq." + hashlib.sha256(content).hexdigest(), status="eq.approved",
                       filename="eq." + filename, size="eq." + str(len(content)))
    if not found:
        raise HTTPException(403, "Request admin approval for this file and knowledge base before uploading.")


@router.get("/admin/users")
def users(request: Request):
    from .access_control import allows
    from . import saved_uploads
    actor = request.state.user
    can_view_accounts = allows(actor, "accounts.view")
    can_view_approvals = allows(actor, "approvals.view")
    if not (can_view_accounts or can_view_approvals):
        raise HTTPException(403, "User details or upload approval access required.")
    if can_view_approvals:
        try:
            saved_uploads.cleanup()
        except cloud.CloudStorageError:
            # Retention is best-effort on admin access, never a scheduler dependency.
            pass
    people = cloud.rows("users", select="id,name,email,created_at", order="created_at.desc,id.asc") if can_view_accounts else []
    permissions = {r["user_id"]: r["policy"] for r in cloud.rows("qa_upload_permissions")} if can_view_accounts and can_view_approvals else {}
    from .saved_uploads import public_row
    requests = [public_row(r) for r in cloud.rows("qa_upload_requests", order="created_at.desc,id.asc")
                if not r.get("admin_deleted_at")] if can_view_approvals else []
    for person in people:
        person["policy"] = permissions.get(person["id"], "approval_required")
        person["pending"] = sum(r["user_id"] == person["id"] and r["status"] == "pending" for r in requests)
    return {"users": people, "requests": requests}


@router.get("/admin/users/{owner}/activity")
def user_activity(request: Request, owner: str):
    admin(request)
    owner = uid(owner)
    from . import domains, ingest
    from .extract import IMAGE_EXTENSIONS

    uploads = cloud.rows(
        "qa_upload_requests", user_id="eq." + owner,
        select="domain,file_hash,filename,status,file_ready,job_state", order="id.asc",
    )
    uploads_by_domain = defaultdict(list)
    for upload in uploads:
        uploads_by_domain[upload["domain"]].append(upload)
    token = workspace.bind({"id": owner})
    try:
        documents_by_domain = defaultdict(dict)
        if config.CLOUD_MODE:
            for row in cloud.rows(
                "qa_documents", namespace="eq." + config.embedding_namespace(),
                select="domain,file_hash,name,status", order="domain.asc,file_hash.asc",
            ):
                documents_by_domain[row["domain"]][row["file_hash"]] = (row["name"], row["status"])
        bases = []
        for domain in domains.list_domains():
            if config.CLOUD_MODE:
                files = documents_by_domain[domain.key]
            else:
                files = {
                    digest: (row["name"], "ready")
                    for digest, row in ingest.list_ingested(domain.key).items()
                }
            approvals = Counter()
            for upload in uploads_by_domain[domain.key]:
                approvals[upload["status"]] += 1
                digest = upload["file_hash"]
                job = upload.get("job_state")
                current = files.get(digest)
                if upload["status"] not in {"pending", "approved"} or not upload.get("file_ready"):
                    continue
                if current and current[1] == "ready":
                    continue
                if job in {"queued", "processing", "retrying", "failed"}:
                    files[digest] = (upload["filename"], job)
                elif not current and job != "ready":
                    files[digest] = (upload["filename"], "saved")
            types = Counter(Path(name).suffix.lower() for name, _ in files.values())
            bases.append({
                "name": domain.label,
                "pdfs": types[".pdf"],
                "images": sum(types[extension] for extension in IMAGE_EXTENSIONS),
                "approvals": dict(approvals),
                "processing": dict(Counter(status for _, status in files.values() if status != "saved")),
            })
    finally:
        workspace.reset(token)
    return {"knowledge_bases": bases}


class PermissionBody(BaseModel):
    policy: str


@router.put("/admin/users/{user_id}/permissions")
def permissions(user_id: str, body: PermissionBody, request: Request):
    admin(request)
    from .revisions import actor_header
    user_id = uid(user_id)
    if body.policy not in POLICIES:
        raise HTTPException(400, "Invalid upload policy.")
    if not cloud.rows("users", id="eq." + user_id):
        raise HTTPException(404, "User not found.")
    cloud.write("qa_upload_permissions", {"user_id": user_id, "policy": body.policy}, "user_id", headers=actor_header(request))
    return {"policy": body.policy}


@router.get("/upload-requests")
def own_requests(request: Request):
    from .saved_uploads import public_row
    from .access_control import allows_direct_upload
    if workspace.owner_id() == "admin":
        return {"policy": "allowed", "requests": []}
    rows = [r for r in cloud.rows('qa_upload_requests',user_id='eq.'+workspace.owner_id(),order='created_at.desc,id.asc')
            if not r.get('admin_deleted_at')]
    return {'policy':upload_mode(workspace.owner_id(), allows_direct_upload(request.state.user)),'requests':[public_row(r) for r in rows if not r.get('user_deleted_at')],
            'ready_documents':[{'domain':r['domain'],'file_hash':r['file_hash'],'filename':r['filename']}
                for r in rows if r['status']=='approved' and r.get('job_state')=='ready'],
            'processing':[{'id':r['id'],'domain':r['domain'],'file_hash':r['file_hash'],'filename':r['filename']}
                for r in rows if r['status']=='approved' and r.get('file_ready')
                and r.get('job_state') not in {'ready','failed'}]}


@router.delete('/upload-requests/{request_id}')
def remove_own_card(request_id: str, request: Request):
    from datetime import datetime, timezone
    owner = workspace.owner_id()
    if owner == 'admin':
        raise HTTPException(403,'Only users can remove their own upload cards.')
    request_id = uid(request_id)
    rows = cloud.rows('qa_upload_requests',id='eq.'+request_id,user_id='eq.'+owner)
    if not rows:
        raise HTTPException(404,'Upload request not found.')
    if not rows[0].get('user_deleted_at'):
        cloud._request('PATCH','/rest/v1/qa_upload_requests',
            params={'id':'eq.'+request_id,'user_id':'eq.'+owner,'user_deleted_at':'is.null'},
            json={'user_deleted_at':datetime.now(timezone.utc).isoformat()})
    return {'ok':True}


@router.post('/uploads',status_code=201)
async def saved_upload(request: Request, domain: str = Form(...), file: UploadFile = File(...),
                       message: str = Form(default='',max_length=500), use_llm: bool = Form(default=True)):
    from .domains import get_domain
    from .saved_uploads import save
    from .access_control import allows_direct_upload
    try:
        selected = get_domain(domain)
    except ValueError:
        raise HTTPException(404,'Knowledge base not found.') from None
    content = await file.read(config.MAX_UPLOAD_BYTES+1)
    return await run_in_threadpool(save,selected,file.filename or '',content,message,use_llm,
                                   direct_upload=allows_direct_upload(request.state.user))


class UploadRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=255)
    file_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0, le=config.MAX_UPLOAD_BYTES)
    message: str = Field(default="", max_length=500)


@router.post("/upload-requests")
def create_request(body: UploadRequest, request: Request):
    from .domains import get_domain
    from .extract import SUPPORTED_EXTENSIONS
    from pathlib import Path
    if workspace.owner_id() == "admin":
        raise HTTPException(400, "Admin uploads do not require approval.")
    if policy(workspace.owner_id()) == "blocked":
        raise HTTPException(403, "Uploads are blocked for your account.")
    try:
        domain = get_domain(body.domain)
    except ValueError:
        raise HTTPException(404, "Knowledge base not found.") from None
    if Path(body.filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, "Unsupported file type.")
    data = body.model_dump()
    data.update(user_id=workspace.owner_id(), domain_label=domain.label)
    # Ignore duplicates rather than resetting an approved/rejected decision.
    result = cloud._request("POST", "/rest/v1/qa_upload_requests",
        params={"on_conflict": "user_id,domain,file_hash"},
        headers={"Prefer": "resolution=ignore-duplicates,return=representation"}, json=data).json()
    if result:
        return result[0]
    existing = cloud.rows("qa_upload_requests", user_id="eq." + workspace.owner_id(),
                          domain="eq." + body.domain, file_hash="eq." + body.file_hash)[0]
    if existing.get("admin_deleted_at"):
        raise HTTPException(409, "An approval request for this file was removed.")
    return existing


class Decision(BaseModel):
    status: str
    reason: str = Field(default="", max_length=500)


@contextmanager
def approval_workspace(owner):
    from . import super_admin
    person = super_admin.account(super_admin.auth("GET", "/" + uid(owner)))
    if person["role"] != "user" or person["status"] != "active":
        raise HTTPException(403, "Select an active User account.")
    if not workspace.begin_write(person["id"]):
        raise HTTPException(409, "This workspace is being deleted.")
    token = workspace.bind(person)
    try:
        yield
    finally:
        workspace.reset(token)
        workspace.end_write(person["id"])


@router.get("/admin/upload-requests/for/{owner}")
def approval_bases(owner: str, request: Request):
    admin(request)
    from .domains import list_domains
    with approval_workspace(owner):
        return [{"key": domain.key, "label": domain.label} for domain in list_domains()]


@router.post("/admin/upload-requests/for/{owner}", status_code=201)
async def add_approval_request(owner: str, request: Request, domain: str = Form(...),
                               file: UploadFile = File(...), message: str = Form(default="", max_length=500)):
    admin(request)
    from .domains import get_domain
    from .saved_uploads import save
    content = await file.read(config.MAX_UPLOAD_BYTES + 1)

    def add():
        with approval_workspace(owner):
            try:
                selected = get_domain(domain)
            except ValueError:
                raise HTTPException(404, "Knowledge base not found.") from None
            return save(selected, file.filename or "", content, message, request_approval=True)

    return await run_in_threadpool(add)


@router.delete("/admin/upload-requests/{request_id}")
def delete_approval_request(request_id: str, request: Request):
    admin(request)
    request_id = uid(request_id)
    now = datetime.now(timezone.utc).isoformat()
    changed = cloud._request("PATCH", "/rest/v1/qa_upload_requests", params={
        "id": "eq." + request_id, "admin_deleted_at": "is.null",
        "or": f"(lease_until.is.null,lease_until.lte.{now})",
    }, headers={"Prefer": "return=representation"},
       json={"status": "expired", "admin_deleted_at": now, "updated_at": now}).json()
    if not changed:
        rows = cloud.rows("qa_upload_requests", id="eq." + request_id)
        if not rows:
            raise HTTPException(404, "Upload request not found.")
        if not rows[0].get("admin_deleted_at"):
            raise HTTPException(409, "This request is processing. Try again when processing finishes.")
    return {"ok": True}


@router.put("/admin/upload-requests/{request_id}")
def decide(request_id: str, body: Decision, request: Request):
    request_id = uid(request_id)
    if body.status not in {"approved", "rejected"}:
        raise HTTPException(400, "Choose approved or rejected.")
    from .access_control import allows
    actor = request.state.user
    if not allows(actor, "approvals.edit"):
        raise HTTPException(403, "Upload approval access required.")
    if actor["role"] == "user" and cloud.rows(
        "qa_upload_requests", id="eq." + request_id, user_id="eq." + actor["id"], select="id",
    ):
        raise HTTPException(403, "You cannot review your own upload request.")
    changed = cloud._request('POST','/rest/v1/rpc/qa_decide_saved_upload',
        json={'p_id':request_id,'p_status':body.status,'p_reason':body.reason.strip()}).json()
    if not changed:
        raise HTTPException(409, "This request was already decided or no longer exists. Refresh the list.")
    from .saved_uploads import public_row
    return public_row(changed[0])
